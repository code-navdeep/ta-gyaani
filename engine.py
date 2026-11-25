"""
Real-time OHLC Data Collection Engine

PURPOSE:
Fetch live quotes for all assets in assets_select.csv and store in ohlc.db

ARCHITECTURE:
- AssetLoader: Read assets_select.csv
- DhanQuoteFetcher: API interface for quotes
- OHLCDatabase: SQLite storage in ohlc.db
- Engine: Main orchestrator with configurable refresh rate

REFRESH RATE: 5 seconds (easily configurable via REFRESH_INTERVAL_SECONDS)
"""

import os
import sys
import time
import logging
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
import signal
import threading
import zmq
import json

# ================================
# CONFIGURATION SECTION
# ================================

# Seconds between quote fetches. Keep it well under 30: a 15-minute block counts
# as complete only if a reading lands within 30 s of each of its edges
# (time_blocks.BLOCK_EDGE_TOLERANCE), PV Divergence treats a gap of more than 30 s
# between readings as an interruption, and its window is sized for 5-second
# readings (price_volume_divergence_adapter.WINDOW_SAMPLES = 180, i.e. 15 minutes).
REFRESH_INTERVAL_SECONDS = 5

# Database configuration
# Indices are not tradeable and have no volume. The rule governs three sites in
# this file plus the adapters and the dashboard, so it lives in one place —
# calculators/segments.py, a zero-dependency leaf module (this is the only local
# import engine.py makes, and it cannot create a cycle).
from calculators.market_calendar import is_market_holiday
from calculators.segments import NON_VOLUME_SEGMENTS
from calculators.time_blocks import (
    IST, block_at_detection_window, block_complete, block_end_time, block_start_time,
    block_reading_end_time, reading_clock,
)

DATABASE_NAME = "ohlc.db"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATABASE_PATH = os.path.join(DATA_DIR, DATABASE_NAME)

# Database settings
DATABASE_TIMEOUT = float(os.getenv('DB_TIMEOUT', '30.0'))
DATABASE_CACHE_SIZE = int(os.getenv('DB_CACHE_SIZE', '-64000'))

# API configuration
API_RATE_LIMIT_SECONDS = float(os.getenv('BASE_RATE_LIMIT_DELAY', '1.0'))  # Dhan API limit: min 1.0 sec
API_TIMEOUT_SECONDS = 30

# Logging configuration
LOG_DIR = os.path.join(DATA_DIR, "logs")

# ZMQ configuration for price broadcasting
ZMQ_PUBLISH_PORT = int(os.getenv("ZMQ_PUBLISH_PORT", "5556"))
ZMQ_PUBLISH_ADDRESS = os.getenv("ZMQ_PUBLISH_ADDRESS", f"tcp://0.0.0.0:{ZMQ_PUBLISH_PORT}")

# ================================
# LOGGING SETUP
# ================================

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger(__name__)

# ================================
# ASSET LOADER
# ================================

class AssetLoader:
    """Load and manage assets from assets_select.csv"""

    def __init__(self, csv_path: str = "assets/assets_select.csv"):
        self.csv_path = csv_path
        self.assets_df = None
        self.grouped_assets = None

    def load_assets(self) -> bool:
        """Load assets from CSV file"""
        try:
            if not os.path.exists(self.csv_path):
                logger.error(f"Assets file not found: {self.csv_path}")
                return False

            self.assets_df = pd.read_csv(self.csv_path)

            # Validate required columns
            required_columns = ['asset_id', 'asset_name', 'exchange', 'segment_code']
            missing_columns = [col for col in required_columns if col not in self.assets_df.columns]

            if missing_columns:
                logger.error(f"Missing required columns in CSV: {missing_columns}")
                return False

            # Clean data - bulletproof approach for mixed data types
            try:
                # Clean column names safely
                self.assets_df.columns = self.assets_df.columns.str.strip()
            except Exception as e:
                logger.warning(f"Could not clean column names: {e}")

            # Clean string data safely - only apply str operations to string columns
            for col in self.assets_df.columns:
                try:
                    if self.assets_df[col].dtype == "object":
                        # Check if column actually contains string data before using .str accessor
                        non_null_data = self.assets_df[col].dropna()
                        if len(non_null_data) > 0 and isinstance(non_null_data.iloc[0], str):
                            self.assets_df[col] = self.assets_df[col].astype(str).str.strip()
                        else:
                            logger.debug(f"Skipping string operations on column '{col}' - contains non-string data")
                except Exception as e:
                    logger.warning(f"Could not clean column '{col}': {e}")

            # Group assets by exchange segment for API efficiency
            self._group_assets_by_exchange()

            logger.info(f"Loaded {len(self.assets_df)} assets from {self.csv_path}")
            logger.info(f"Exchange segments: {list(self.grouped_assets.keys())}")

            return True

        except Exception as e:
            logger.error(f"Failed to load assets: {e}")
            return False

    def _group_assets_by_exchange(self):
        """Group assets by exchange segment for API request format"""
        self.grouped_assets = {}

        for _, row in self.assets_df.iterrows():
            segment_code = row['segment_code']  # e.g., "NSE_EQ", "IDX_I"
            asset_id = int(row['asset_id'])

            if segment_code not in self.grouped_assets:
                self.grouped_assets[segment_code] = []

            self.grouped_assets[segment_code].append(asset_id)

        logger.debug(f"Grouped assets by segment: {self.grouped_assets}")

    def get_grouped_assets(self) -> Dict[str, List[int]]:
        """Get assets grouped by exchange segment (for API request)"""
        return self.grouped_assets or {}

    def get_asset_info(self, asset_id: int, segment_code: str) -> Optional[Dict]:
        """Get asset information by composite (asset_id, segment_code) key.

        Required because some asset_ids are shared across segments (e.g. 25 is
        BankNifty in IDX_I and ADANIENT in NSE_EQ); filtering on asset_id alone
        returns whichever row appears first in the CSV and mislabels the other.
        """
        if self.assets_df is None:
            return None

        asset_row = self.assets_df[
            (self.assets_df['asset_id'] == asset_id) &
            (self.assets_df['segment_code'] == segment_code)
        ]

        if asset_row.empty:
            return None

        return asset_row.iloc[0].to_dict()

    def get_index_constituents(self, index_name: str) -> List[Tuple[int, str, float]]:
        """
        Return [(asset_id, segment_code, weight_pct), ...] for stocks that are
        constituents of the given index.

        index_name: 'NIFTY' | 'BankNifty' | 'Midcap' | 'SENSEX'
            (matches the substring used in the CSV `index` column).

        Reads the constituent's `index` column (comma-separated) and the matching
        `weight_*` column. Skips rows where the weight is NaN or missing.
        """
        if self.assets_df is None:
            return []

        weight_col_map = {
            'NIFTY': 'weight_nifty',
            'BankNifty': 'weight_bank',
            'Midcap': 'weight_midcap',
            'SENSEX': 'weight_sensex',
        }
        weight_col = weight_col_map.get(index_name)
        if weight_col is None or weight_col not in self.assets_df.columns:
            return []

        constituents = []
        for _, row in self.assets_df.iterrows():
            index_field = str(row.get('index', '') or '')
            if index_name not in index_field:
                continue
            weight = row.get(weight_col)
            if pd.isna(weight):
                continue
            try:
                constituents.append((int(row['asset_id']), str(row['segment_code']), float(weight)))
            except (ValueError, TypeError):
                continue
        return constituents

# ================================
# DHAN QUOTE FETCHER
# ================================

class DhanQuoteFetcher:
    """Fetch quotes for every tracked asset from Dhan's market-quote API."""

    def __init__(self):
        self.access_token = None
        self.client_id = None
        self.last_api_call = 0
        self._load_credentials()

    def _load_credentials(self):
        """Load API credentials from environment"""
        load_dotenv(override=True)

        self.access_token = os.getenv('DHAN_ACCESS_TOKEN')
        self.client_id = os.getenv('DHAN_CLIENT_ID')

        if not self.access_token or not self.client_id:
            logger.error("Missing DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID in .env file")
            raise ValueError("API credentials not found")

        logger.info("API credentials loaded successfully")

    def _respect_rate_limit(self):
        """Ensure we don't exceed API rate limit"""
        time_since_last = time.time() - self.last_api_call
        if time_since_last < API_RATE_LIMIT_SECONDS:
            sleep_time = API_RATE_LIMIT_SECONDS - time_since_last
            logger.debug(f"Rate limiting: sleeping for {sleep_time:.2f} seconds")
            time.sleep(sleep_time)

    def _reload_token_if_changed(self) -> bool:
        """Re-read .env and adopt its token if it differs from the one in use.

        Dhan invalidates the old token the moment a renewal succeeds. The EOD
        scheduler hands the new token straight to this fetcher (main.py, Job 4),
        but a renewal done anywhere else — the standalone renew_token wrapper
        runs in a separate container — only rewrites .env. Without this, the
        engine would keep sending a dead token until the next restart.

        Keyed to "the token in .env changed", not to an HTTP status code: the
        status Dhan returns for an expired token has never been observed in
        this system's logs.
        """
        load_dotenv(override=True)
        token = os.getenv('DHAN_ACCESS_TOKEN')
        if token and token != self.access_token:
            self.access_token = token
            logger.info("DHAN_ACCESS_TOKEN changed in .env — using the new token")
            return True
        return False

    def fetch_quotes(self, grouped_assets: Dict[str, List[int]]) -> Optional[Dict]:
        """
        Fetch OHLC quotes for grouped assets.

        A failed request re-reads .env once; if the token there has changed, the
        request is retried once with it. Otherwise the failure stands.

        Args:
            grouped_assets: {"NSE_EQ": [asset_id1, asset_id2], "NSE_IDX": [asset_id3]}

        Returns:
            API response data or None if failed
        """
        api_data = self._request_quotes(grouped_assets)
        if api_data is None and self._reload_token_if_changed():
            api_data = self._request_quotes(grouped_assets)
        return api_data

    def _request_quotes(self, grouped_assets: Dict[str, List[int]]) -> Optional[Dict]:
        """One quote request with the current token. None if it failed."""
        try:
            self._respect_rate_limit()

            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'access-token': self.access_token,
                'client-id': self.client_id
            }

            url = "https://api.dhan.co/v2/marketfeed/quote"

            logger.debug(f"Making OHLC API request for {sum(len(assets) for assets in grouped_assets.values())} assets")

            response = requests.post(
                url,
                json=grouped_assets,
                headers=headers,
                timeout=API_TIMEOUT_SECONDS
            )

            self.last_api_call = time.time()

            if response.status_code != 200:
                logger.error(f"API request failed with status {response.status_code}: {response.text}")
                return None

            api_data = response.json()

            if api_data.get('status') != 'success':
                logger.error(f"API returned unsuccessful status: {api_data}")
                return None

            logger.debug(f"Successfully fetched quotes for {sum(len(assets) for assets in grouped_assets.values())} assets")
            return api_data.get('data', {})

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during API request: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during quote fetch: {e}")
            return None

# ================================
# OHLC DATABASE
# ================================

class OHLCDatabase:
    """SQLite database manager for OHLC data storage"""

    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._initialize_database()

    @contextmanager
    def get_connection(self):
        """Database connection context manager (same pattern as dashboard.py)"""
        conn = None
        try:
            # Ensure data directory exists
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

            # Connect with same settings as main dashboard
            conn = sqlite3.connect(self.db_path, timeout=DATABASE_TIMEOUT)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA cache_size = {DATABASE_CACHE_SIZE}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA temp_store = MEMORY")
            yield conn
        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()


    def _initialize_database(self):
        """Create database tables if they don't exist.

        `created_at` carries NO DEFAULT, deliberately. `DEFAULT
        CURRENT_TIMESTAMP` evaluates as UTC, space-separated, which sorts
        before every 'T'-separated IST cutoff as TEXT and would be invisible to
        a `>=` comparison forever. All three INSERTs below name the column and
        pass `datetime.now(IST).isoformat()`; an omission now writes a loud
        NULL instead of a silently unmatchable value.
        """
        try:
            with self.get_connection() as conn:
                # Create OHLC data table with extended quote data
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ohlc_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        asset_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        segment_code TEXT NOT NULL,
                        last_price REAL,
                        open_price REAL,
                        high_price REAL,
                        low_price REAL,
                        close_price REAL,
                        change_amount REAL,
                        change_percent REAL,
                        volume INTEGER DEFAULT 0,  -- Cumulative daily volume (running total from API)
                        interval_volume INTEGER DEFAULT 0,  -- Volume traded in this 5-second interval
                        created_at DATETIME,
                        -- New quote endpoint data fields
                        average_price REAL,
                        last_quantity INTEGER,
                        last_trade_time TEXT,
                        net_change REAL,
                        buy_quantity INTEGER,
                        sell_quantity INTEGER,
                        upper_circuit_limit REAL,
                        lower_circuit_limit REAL,
                        open_interest INTEGER DEFAULT 0,
                        oi_day_high INTEGER DEFAULT 0,
                        oi_day_low INTEGER DEFAULT 0
                    )
                """)

                # Create indexes for better performance
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ohlc_timestamp_asset
                    ON ohlc_data(timestamp, asset_id)
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ohlc_asset_timestamp
                    ON ohlc_data(asset_id, timestamp DESC)
                """)

                # Create metadata table for tracking
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fetch_metadata (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        assets_count INTEGER NOT NULL,
                        fetch_duration_ms INTEGER,
                        api_status TEXT,
                        created_at DATETIME
                    )
                """)

                # Create time block volumes table for volume trajectory analysis.
                # NOTE: time_block value range is 1..25 (15-min blocks
                # covering 09:15–15:30). No CHECK constraint, deliberately —
                # forward-compatible if block granularity ever changes again.
                #
                # cumulative_volume is EMPTY (NULL) for a block not fully
                # observed — time left unobserved at its start or end beyond
                # BLOCK_EDGE_TOLERANCE (see _store_time_block_volumes). Empty,
                # never 0: a 0 claims nothing traded, which Volume Block would
                # z-score as an extreme "unusually quiet" block. An empty row
                # lets it say "incomplete block" instead.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS time_block_volumes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL,
                        asset_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        segment_code TEXT NOT NULL,
                        time_block INTEGER NOT NULL,
                        cumulative_volume INTEGER,
                        timestamp TEXT NOT NULL,
                        created_at DATETIME,
                        UNIQUE(date, asset_id, segment_code, time_block)
                    )
                """)

                # Create indexes for time block volumes queries
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_time_block_asset_date
                    ON time_block_volumes(asset_id, date DESC, time_block)
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_time_block_date_block
                    ON time_block_volumes(date DESC, time_block)
                """)

                conn.commit()
                logger.info(f"Database initialized at {self.db_path}")

        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    def store_ohlc_data(self, ohlc_records: List[Dict]) -> bool:
        """Store OHLC data records in database"""
        try:
            if not ohlc_records:
                logger.warning("No OHLC records to store")
                return True

            with self.get_connection() as conn:
                conn.executemany("""
                    INSERT INTO ohlc_data (
                        timestamp, asset_id, symbol, segment_code,
                        last_price, open_price, high_price, low_price, close_price,
                        change_amount, change_percent, volume, interval_volume,
                        average_price, last_quantity, last_trade_time, net_change,
                        buy_quantity, sell_quantity, upper_circuit_limit, lower_circuit_limit,
                        open_interest, oi_day_high, oi_day_low, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (
                        record['timestamp'],
                        record['asset_id'],
                        record['symbol'],
                        record['segment_code'],
                        record['last_price'],
                        record['open_price'],
                        record['high_price'],
                        record['low_price'],
                        record['close_price'],
                        record['change_amount'],
                        record['change_percent'],
                        record.get('volume', 0),
                        record.get('interval_volume', 0),
                        # New quote endpoint fields
                        record.get('average_price'),
                        record.get('last_quantity'),
                        record.get('last_trade_time'),
                        record.get('net_change'),
                        record.get('buy_quantity'),
                        record.get('sell_quantity'),
                        record.get('upper_circuit_limit'),
                        record.get('lower_circuit_limit'),
                        record.get('open_interest', 0),
                        record.get('oi_day_high', 0),
                        record.get('oi_day_low', 0),
                        datetime.now(IST).isoformat()
                    ) for record in ohlc_records
                ])

                conn.commit()
                logger.debug(f"Stored {len(ohlc_records)} OHLC records in database")
                return True

        except Exception as e:
            logger.error(f"Failed to store OHLC data: {e}")
            return False

    def log_fetch_metadata(self, assets_count: int, duration_ms: int, api_status: str):
        """Log fetch operation metadata"""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO fetch_metadata (timestamp, assets_count, fetch_duration_ms, api_status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (datetime.now(IST).isoformat(), assets_count, duration_ms, api_status, datetime.now(IST).isoformat()))

                conn.commit()

        except Exception as e:
            logger.error(f"Failed to log fetch metadata: {e}")

# ================================
# SCHEDULE MANAGER
# ================================

class OHLCScheduleManager:
    """Manages scheduling logic for OHLC data collection.

    Reads the `schedule` section of config.json."""

    def __init__(self, config_path: str = "config.json"):
        self.config_file = config_path
        self.schedule_config = self.load_schedule_config()

    def load_schedule_config(self) -> dict:
        """Load the `schedule` section from config.json."""
        if not os.path.exists(self.config_file):
            raise FileNotFoundError(f"Config file not found: {self.config_file}")

        with open(self.config_file, 'r') as f:
            full_config = json.load(f)

        if 'schedule' not in full_config:
            raise KeyError(f"'schedule' section not found in {self.config_file}")

        return full_config['schedule']

    def is_within_collection_window(self) -> bool:
        """Check if current time is within collection window"""
        if not self.schedule_config['enabled']:
            return False

        now = datetime.now(IST)

        # Check if we're in the right day
        if not self._is_scheduled_day(now):
            return False

        # Check if we're within time window
        start_time = self._parse_time(self.schedule_config['start_time'])
        end_time = self._parse_time(self.schedule_config['end_time'])
        current_time = now.time()

        if start_time <= end_time:
            return start_time <= current_time <= end_time
        else:
            return current_time >= start_time or current_time <= end_time

    def _is_scheduled_day(self, check_datetime) -> bool:
        """Check if given datetime falls on a scheduled day.

        A market holiday listed in data/holidays.txt never is. This gates the
        engine's fetching AND the metrics loop (which calls it too), so on a
        holiday nothing is fetched, stored or computed."""
        if is_market_holiday(check_datetime.date()):
            return False
        if self.schedule_config['schedule_type'] == 'daily':
            return True
        elif self.schedule_config['schedule_type'] == 'weekdays':
            return check_datetime.weekday() < 5  # Monday to Friday
        return False

    def _parse_time(self, time_str: str):
        """Parse time string in HH:MM format"""
        try:
            return datetime.strptime(time_str, '%H:%M').time()
        except ValueError:
            logger.error(f"Invalid time format: {time_str}")
            return datetime.strptime('09:15', '%H:%M').time()


# ================================
# MAIN OHLC ENGINE
# ================================

class OHLCEngine:
    """Main orchestrator for OHLC data collection"""

    def __init__(self, csv_path: str = "assets/assets_select.csv", refresh_interval: int = 5,
                 config_path: str = "config.json"):
        self.asset_loader = AssetLoader(csv_path)
        self.quote_fetcher = DhanQuoteFetcher()
        self.database = OHLCDatabase()
        self.running = False
        self.fetch_thread = None
        self.fetch_count = 0
        self.success_count = 0
        self.error_count = 0
        self.last_fetch_time = None
        self.refresh_interval = refresh_interval  # Make refresh rate dynamic
        self.zmq_lock = threading.Lock()  # Thread safety for ZMQ operations
        self.schedule_manager = OHLCScheduleManager(config_path)  # Schedule support

        # Initialize ZMQ publisher
        self.zmq_context = zmq.Context()
        self.zmq_publisher = self.zmq_context.socket(zmq.PUB)
        self.zmq_publisher.bind(ZMQ_PUBLISH_ADDRESS)
        logger.info(f"ZMQ Publisher initialized on {ZMQ_PUBLISH_ADDRESS}")

        # Load the asset list during initialization
        self.asset_loader.load_assets()

    def start(self):
        """Start the OHLC data collection engine"""
        logger.info("=== OHLC Engine Starting ===")

        # Check if assets are loaded (they should be loaded during initialization)
        if self.asset_loader.assets_df is None or len(self.asset_loader.assets_df) == 0:
            logger.error("No assets loaded - cannot start engine")
            return False

        # Start fetching in background thread
        self.running = True
        self.fetch_thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self.fetch_thread.start()

        logger.info(f"OHLC Engine started - fetching every {self.refresh_interval} seconds")
        return True

    def _publish_price_updates(self, records: List[Dict]) -> None:
        """
        Publish price updates via ZMQ for calculators (PriceDataManager)

        Args:
            records: List of OHLC records from database store
        """
        # Early exit if stopping
        if not self.running:
            return

        try:
            with self.zmq_lock:
                # Double-check after acquiring lock
                if not self.running:
                    return

                timestamp = datetime.now(IST).isoformat()

                for record in records:
                    asset_id = record.get('asset_id')
                    segment_code = record.get('segment_code')
                    last_price = record.get('last_price')

                    # Skip if missing critical data
                    if not asset_id or not segment_code or last_price is None:
                        continue

                    # Create complete message with all fields required by PriceDataManager
                    # Must include: asset_id, symbol, segment_code, last_price, open_price,
                    # high_price, low_price, volume, average_price, interval_volume
                    message = {
                        'asset_id': asset_id,
                        'symbol': record.get('symbol'),
                        'segment_code': segment_code,
                        'last_price': last_price,
                        'open_price': record.get('open_price'),
                        'high_price': record.get('high_price'),
                        'low_price': record.get('low_price'),
                        'volume': record.get('volume'),
                        'average_price': record.get('average_price'),
                        'interval_volume': record.get('interval_volume'),
                        'timestamp': timestamp
                    }

                    # Publish
                    topic = f"PRICE.{asset_id}"
                    self.zmq_publisher.send_string(f"{topic} {json.dumps(message)}")

            logger.debug(f"Published {len(records)} price updates via ZMQ")

        except zmq.ZMQError as e:
            if self.running:  # Only log if not shutting down
                logger.error(f"ZMQ error publishing price updates: {e}")
        except Exception as e:
            logger.error(f"Error publishing price updates: {e}")

    def stop(self):
        """Stop the OHLC data collection engine"""
        logger.info("=== OHLC Engine Stopping ===")
        self.running = False

        if self.fetch_thread and self.fetch_thread.is_alive():
            self.fetch_thread.join(timeout=10)

        logger.info("OHLC Engine stopped (ZMQ remains active)")

    def _calculate_interval_volume(self, conn, asset_id: int, segment_code: str,
                                   current_volume: int, tick_timestamp: str
                                   ) -> Tuple[Optional[int], Optional[int]]:
        """
        This reading's running total to store, and the volume traded in this
        5-second interval: `(volume, interval_volume)`.

        A RUNNING TOTAL LOWER THAN THE LAST GOOD ONE IS A BAD READING. A day's
        running total cannot fall, so a lower one is not a measurement: both
        values are stored as NULL, and the next good reading is measured from
        the last good one. Real sequence, TCS 2026-09-07, at the close (the
        closing-auction period):

            15:15:03  2,163,939  good   interval 1,913
            15:15:08  2,163,939  good   interval 0   (quotes frozen; equal is not lower)
            15:20:05          0  BAD    NULL, NULL   (was stored as -2,163,939)
            15:29:57    132,757  BAD    NULL, NULL   (the auction's own count, alone)
            15:30:02  2,296,696  good   interval 132,757  (2,296,696 - 2,163,939)

        So a drop no longer becomes minus the whole day, and the recovery no
        longer lands as the whole day again.

        VOLUME SEMANTICS:
        - current_volume (param) = cumulative daily volume from API (running total,
          reset to ~0 by the exchange each morning)
        - previous_volume (from DB) = the previous cumulative from the last 5-sec
          snapshot **of the same session**
        - return = the difference = volume traded in this 5-second interval

        INDICES HAVE NO VOLUME. An index is not tradeable, so there is nothing to
        diff. Returns None (stored as NULL, never 0) so that "no volume exists" is
        distinguishable from "zero volume traded".

        THE SAME-DAY FILTER IS LOAD-BEARING — do not remove it.
        `ohlc.db` retains 5 trading days and the API's cumulative volume resets each
        morning. Without `substr(timestamp, 1, 10) = today` this query returns YESTERDAY'S
        CLOSING cumulative on the first tick of a session, so the subtraction below
        yields a large negative number:

            previous (yesterday's close) : 50,000,000
            current  (today, 09:15:05)   :  4,200,000
            difference                   : -45,800,000

        That is what the old `max(0, ...)` clamp was hiding. It zeroed the FIRST
        reading of every session — the only reading with no earlier same-day
        reading to subtract from — so block 1 lost whatever the exchange had
        already counted before that first poll, every day, for every asset.
        Measured on TCS 2026-08-28: ~66,818 of block 1's ~688,491 shares, about
        10%. Not the whole block; every later reading subtracted correctly.
        There is no clamp now: with this filter the subtraction cannot go
        negative, so a clamp could only mask a regression.

        On that ~66,818: it is volume the API attributes to before 09:15,
        recovered by differencing the intraday endpoint's running totals.
        NEITHER THE EXCHANGE NOR THE BROKER REPORTS IT SEPARATELY OR STATES
        WHERE IT CAME FROM. Do not describe it as an auction match, an odd-lot
        session or a block deal — the data carries no such label.

        Args:
            conn: An open ohlc.db connection, shared by every asset in one fetch
                cycle. Opening one per asset cost five PRAGMAs each time — about
                369,000 a day — inside a fetch loop that already has no time to
                spare.
            asset_id: Asset ID
            segment_code: Segment code (required — some asset_ids are shared across
                segments, e.g. 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ; filtering
                on asset_id alone would diff one entity's volume against the other's)
            current_volume: Current cumulative volume from the API
            tick_timestamp: ISO timestamp of this tick. Used for both the same-day
                filter and the market-open check — deliberately NOT datetime.now(),
                so the branch below is testable without freezing the clock.

        Returns:
            `(volume, interval_volume)`. Both None for a segment that has no
            volume, for a bad reading (above), and when the lookup fails — a
            failed lookup is handled like a bad reading, so the next reading
            is measured from the last good one and no interval is lost. Never
            a 0: a fabricated zero-volume interval would be summed into its
            block and archived as real.
        """
        if segment_code in NON_VOLUME_SEGMENTS:
            return None, None

        try:
            tick_dt = datetime.fromisoformat(tick_timestamp)
            session_date = tick_dt.date().isoformat()

            # substr(), NOT DATE(). SQLite's date functions PARSE the string
            # and, if it carries a UTC offset, convert to UTC before
            # extracting — so DATE('2026-08-31T00:30:00+05:30') is
            # '2026-08-30', and strftime('%H:%M', '...T09:15:00+05:30') is
            # '03:45', not '09:15'. substr() is a lexical slice: characters
            # 1-19 of an ISO timestamp are the same whether or not an offset
            # follows, so this reads IST wall-clock regardless of storage
            # format. Every date/time extraction in this codebase uses
            # substr for that reason. Do not "simplify" it back to DATE().
            # The last GOOD reading: bad readings are stored with volume NULL
            # and must not become the base the next reading is measured from.
            cursor = conn.execute("""
                SELECT volume FROM ohlc_data
                WHERE asset_id = ? AND segment_code = ? AND substr(timestamp, 1, 10) = ?
                  AND volume IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
            """, (asset_id, segment_code, session_date))

            result = cursor.fetchone()

            if result is None:
                # Genuinely the first tick of THIS session for this asset.
                #
                # Inside block 1 — [09:15, 09:30) — the whole running total
                # belongs to block 1: block 1 and the exchange's running total
                # both begin at 09:15, so a first reading at 09:25 carries
                # exactly block 1's volume so far. STRICTLY before 09:30: a
                # reading stamped 09:30:00 is block 2's, and handing it the
                # morning's total would file block 1's volume under block 2.
                # (The boundary was a bare 09:20, derived from nothing; a first
                # reading between 09:20 and 09:30 discarded the morning and
                # left block 1 short but looking complete.)
                if tick_dt.time() < block_end_time(1):
                    # Normal open: the whole running total so far IS this
                    # interval's volume — no earlier reading exists to have
                    # claimed any of it. That total includes whatever the
                    # exchange counted before 09:15; what that volume is,
                    # neither the exchange nor the broker says.
                    return current_volume, current_volume

                # Late start (e.g. engine first tick at 11:49). The morning's
                # accumulated volume is real but we cannot know how it was
                # distributed across the blocks we missed, so attributing all of
                # it to this one interval would be a fabrication. Discard it.
                return current_volume, 0

            previous_volume = result[0]
            if current_volume < previous_volume:
                # A bad reading — see the docstring. Nothing is stored for it,
                # so the next reading is measured from `previous_volume`.
                return None, None
            return current_volume, current_volume - previous_volume

        except Exception as e:
            # Treated exactly like a bad reading: nothing stored for this
            # reading's running total, so the next reading is measured from
            # the last good one and no volume goes missing from the block.
            # Keeping the total with an empty interval measured the next
            # reading from THIS one, dropping this reading's ~5 s of volume from
            # its block while the block still looked complete. If the lookup
            # keeps failing, every reading is stored empty and the blocks are
            # marked incomplete — visible, not silently short.
            logger.warning(f"Error calculating interval volume for asset {asset_id}: {e}")
            return None, None

    def _fetch_loop(self):
        """Main fetch loop running in background thread"""
        while self.running:
            try:
                # Check if we're within collection window
                config = self.schedule_manager.schedule_config
                if config['enabled'] and not self.schedule_manager.is_within_collection_window():
                    logger.debug("Outside collection window - skipping fetch")
                    time.sleep(60)  # Sleep for 1 minute when outside window
                    continue

                start_time = time.time()

                # Get grouped assets for API request
                grouped_assets = self.asset_loader.get_grouped_assets()

                if not grouped_assets:
                    logger.warning("No assets to fetch")
                    time.sleep(self.refresh_interval)
                    continue

                # Fetch quotes from API
                self.fetch_count += 1
                self.last_fetch_time = datetime.now(IST)

                # Immediate verification to catch corruption
                if not isinstance(self.last_fetch_time, datetime):
                    logger.error(f"CRITICAL: last_fetch_time immediately corrupted to {type(self.last_fetch_time)}")
                    self.last_fetch_time = datetime.now(IST)

                api_data = self.quote_fetcher.fetch_quotes(grouped_assets)

                if api_data:
                    # Process and store the data
                    ohlc_records = self._process_api_data(api_data)

                    if ohlc_records:
                        success = self.database.store_ohlc_data(ohlc_records)

                        # Log fetch metadata
                        duration_ms = int((time.time() - start_time) * 1000)
                        assets_count = sum(len(assets) for assets in grouped_assets.values())
                        status = "success" if success else "storage_failed"

                        self.database.log_fetch_metadata(assets_count, duration_ms, status)

                        if success:
                            self.success_count += 1
                            logger.debug(f"Fetch cycle completed: {len(ohlc_records)} records in {duration_ms}ms")

                            # Publish price updates via ZMQ
                            self._publish_price_updates(ohlc_records)

                            # Check for time block boundary and process volume analysis
                            # Bulletproof time handling - ensure we always have a datetime object
                            if isinstance(self.last_fetch_time, datetime):
                                current_time = self.last_fetch_time
                            else:
                                # Handle corrupted timestamp - log warning and use current time
                                if self.last_fetch_time is not None:
                                    logger.warning(f"last_fetch_time corrupted to {type(self.last_fetch_time)}: {self.last_fetch_time}")
                                current_time = datetime.now(IST)

                            time_block = self._is_time_block_boundary(current_time)
                            if time_block:
                                self._process_time_block_analysis(current_time, time_block)
                        else:
                            self.error_count += 1
                            logger.error("Failed to store OHLC data")
                    else:
                        self.error_count += 1
                        logger.warning("No OHLC records generated from API data")
                else:
                    self.error_count += 1
                    logger.error("Failed to fetch quotes from API")

                # Wait for next cycle
                elapsed = time.time() - start_time
                sleep_time = max(0, self.refresh_interval - elapsed)

                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in fetch loop: {e}")
                time.sleep(self.refresh_interval)

    def _process_api_data(self, api_data: Dict) -> List[Dict]:
        """Turn one quote-API response into OHLC records.

        The quote endpoint also returns each asset's order book (`depth`). It is
        deliberately ignored — never parsed, never stored. Nothing in the system
        uses it, and it used to be 1.2 GB of ohlc.db after four trading days.

        One connection serves the whole cycle; `_calculate_interval_volume`
        looks up each stock's previous reading on it.

        Each asset is processed inside its own `try`. A field that raises for one
        asset drops that asset, logged by name — not every asset after it in the
        same fetch.

        A missing or zero price (`last_price`, `open`, `high`, `low`, `close`) is
        stored as NULL, never 0. The archive takes the day's open and low with SQL
        MIN, which skips NULL but would pick a stored 0 as the day's open or low;
        NULL also stays visible to the archive's quality check. `change_amount` /
        `change_percent` are NULL when either price is missing, rather than
        `0 - close`.
        """
        ohlc_records = []
        timestamp = datetime.now(IST).isoformat()

        def _price(value):
            return round(value, 2) if value else None

        try:
            with self.database.get_connection() as conn:
                for segment_code, securities in api_data.items():
                    for asset_id_str, market_data in securities.items():
                        try:
                            asset_id = int(asset_id_str)

                            # Get asset info
                            asset_info = self.asset_loader.get_asset_info(asset_id, segment_code)
                            if not asset_info:
                                logger.warning(f"Asset info not found for ID {asset_id}")
                                continue

                            raw_last = market_data.get('last_price')
                            ohlc = market_data.get('ohlc', {})
                            raw_close = ohlc.get('close')

                            # Change against Dhan's `close`, which during the
                            # session is the PREVIOUS session's close.
                            if raw_last and raw_close:
                                change_amount = raw_last - raw_close
                                change_percent = (change_amount / raw_close) * 100
                            else:
                                change_amount = None
                                change_percent = None

                            # Volume. Indices are not tradeable and have none — both columns
                            # are stored as NULL for them, never 0, so "no volume exists" stays
                            # distinguishable from "zero traded". Whatever the quote endpoint
                            # returns in `volume` for an index is not a measurement.
                            if segment_code in NON_VOLUME_SEGMENTS:
                                current_volume = None
                                interval_volume = None
                            else:
                                # A bad reading comes back as (None, None): its
                                # running total is not stored either.
                                current_volume, interval_volume = self._calculate_interval_volume(
                                    conn, asset_id, segment_code, market_data.get('volume', 0), timestamp)

                            # Create OHLC record with all quote endpoint data
                            ohlc_record = {
                                'timestamp': timestamp,
                                'asset_id': asset_id,
                                'symbol': asset_info['asset_name'],
                                'segment_code': segment_code,
                                'last_price': _price(raw_last),
                                'open_price': _price(ohlc.get('open')),
                                'high_price': _price(ohlc.get('high')),
                                'low_price': _price(ohlc.get('low')),
                                'close_price': _price(raw_close),
                                'change_amount': round(change_amount, 2) if change_amount is not None else None,
                                'change_percent': round(change_percent, 2) if change_percent is not None else None,
                                'volume': current_volume,          # None for non-volume segments
                                'interval_volume': interval_volume,  # None for non-volume segments
                                # New quote endpoint fields
                                'average_price': round(market_data.get('average_price', 0), 2) if market_data.get('average_price') else None,
                                'last_quantity': market_data.get('last_quantity'),
                                'last_trade_time': market_data.get('last_trade_time'),
                                'net_change': round(market_data.get('net_change', 0), 2),
                                'buy_quantity': market_data.get('buy_quantity', 0),
                                'sell_quantity': market_data.get('sell_quantity', 0),
                                'upper_circuit_limit': round(market_data.get('upper_circuit_limit', 0), 2) if market_data.get('upper_circuit_limit') else None,
                                'lower_circuit_limit': round(market_data.get('lower_circuit_limit', 0), 2) if market_data.get('lower_circuit_limit') else None,
                                'open_interest': market_data.get('oi', 0),
                                'oi_day_high': market_data.get('oi_day_high', 0),
                                'oi_day_low': market_data.get('oi_day_low', 0)
                            }

                            ohlc_records.append(ohlc_record)

                        except Exception as e:
                            logger.error(f"Error processing quote for {segment_code} {asset_id_str}: {e}")

            logger.debug(f"Processed {len(ohlc_records)} OHLC records from API data")

        except Exception as e:
            logger.error(f"Error processing API data: {e}")

        return ohlc_records

    # ================================
    # TIME BLOCK VOLUME ANALYSIS
    # ================================

    def _is_time_block_boundary(self, current_time: datetime) -> Optional[int]:
        """
        Check if current time is at a time block boundary.

        Under the 15-min block grid, there are 25 blocks per session
        (09:15–15:30, 375 min ÷ 15 = 25 exact). Each boundary uses a 2-min
        detection window centered on the block's end time so the engine's 5-sec
        cycle reliably lands inside the window.

        Returns:
            Time block number (1-25) if at boundary, None otherwise.
        """
        # Extra safety check - ensure current_time is actually a datetime object
        if not isinstance(current_time, datetime):
            logger.error(f"Invalid time object in _is_time_block_boundary: {type(current_time)}")
            return None

        # The 25 detection brackets live in calculators/time_blocks.py. A
        # 25-entry dict used to be inlined here, and three more copies of the
        # same brackets existed elsewhere (metrics.py's comment said it
        # "mirrors the pattern in engine.py's boundary detector" — it was
        # written by copying this one). tests/test_time_blocks.py pins all 25
        # against the shared module, so this delegation is directly guarded.
        return block_at_detection_window(current_time)

    def _process_time_block_analysis(self, current_time: datetime, time_block: int) -> None:
        """
        Process time block volume analysis: store volumes for this block.

        Args:
            current_time: Current timestamp
            time_block: 15-min time block number (1-25)
        """
        try:
            logger.info(f"Processing time block {time_block} analysis at {current_time.isoformat()}")

            # Get all assets from asset loader
            if self.asset_loader.assets_df is None:
                logger.warning("No assets loaded for time block analysis")
                return

            current_date = current_time.date().isoformat()

            # Store volume data for all assets at this time block
            self._store_time_block_volumes(current_date, time_block, current_time)

            logger.info(f"Time block {time_block} analysis completed successfully")

        except Exception as e:
            logger.error(f"Error in time block analysis for block {time_block}: {e}")

    def _store_time_block_volumes(self, current_date: str, time_block: int, current_time: datetime) -> None:
        """Store current cumulative volumes for all assets at this time block."""
        # The block's [start, end) window, derived from the shared grid rather
        # than a local table. This defines the SQL window that sums
        # interval_volume into the block, so if it ever disagreed with the
        # detector that chose `time_block`, volume would be filed under the
        # wrong block with nothing raised. A 25-entry dict of 'HH:MM:SS' strings
        # used to live here — a fifth copy of the grid, in a third encoding.
        # The end is the block's READING end: block 25 also takes the session's
        # final readings, up to 15:31 (block_reading_end_time).
        try:
            start_time = block_start_time(time_block).strftime('%H:%M:%S')
            end_time = block_reading_end_time(time_block).strftime('%H:%M:%S')
        except ValueError:
            logger.error(f"Invalid time block number: {time_block}")
            return

        try:
            with self.database.get_connection() as conn:
                written = 0
                for _, asset_row in self.asset_loader.assets_df.iterrows():
                    asset_id = int(asset_row['asset_id'])
                    symbol = asset_row['asset_name']
                    segment_code = asset_row['segment_code']

                    # Indices are not tradeable and have no volume — write NO ROW,
                    # rather than a row of zeros. Their interval_volume is NULL, so
                    # SUM() below would return NULL and COALESCE would turn it into 0;
                    # Volume Block would then z-score a constant-zero series (sigma = 0)
                    # and emit a meaningless signal. Absence is the honest answer.
                    if segment_code in NON_VOLUME_SEGMENTS:
                        continue

                    # The block's volume (the sum of its intervals — "cumulative_volume"
                    # is a historical name for the block's TOTAL), and the three
                    # readings that decide whether it is complete: the last good
                    # reading before its start, and its first and last good ones.
                    # "Good" = a running total was stored (volume IS NOT NULL); a
                    # bad reading is no base for the next interval.
                    #
                    # substr(), NOT DATE()/TIME() — see the note in
                    # _calculate_interval_volume. TIME() on an offset-bearing
                    # timestamp returns UTC, so a 09:15-09:30 block would compare
                    # '03:45' against '09:15' and match nothing.
                    volume_query = """
                        SELECT SUM(CASE WHEN substr(timestamp, 12, 8) >= ? THEN interval_volume END),
                               MAX(CASE WHEN substr(timestamp, 12, 8) < ?
                                         AND volume IS NOT NULL THEN timestamp END),
                               MIN(CASE WHEN substr(timestamp, 12, 8) >= ?
                                         AND volume IS NOT NULL THEN timestamp END),
                               MAX(CASE WHEN substr(timestamp, 12, 8) >= ?
                                         AND volume IS NOT NULL THEN timestamp END)
                        FROM ohlc_data
                        WHERE asset_id = ?
                          AND segment_code = ?
                          AND substr(timestamp, 1, 10) = ?
                          AND substr(timestamp, 12, 8) < ?
                    """
                    block_sum, prev_ts, first_ts, last_ts = conn.execute(volume_query, [
                        start_time, start_time, start_time, start_time,
                        asset_id, segment_code, current_date, end_time]).fetchone()

                    # Complete only when both edges were observed within
                    # BLOCK_EDGE_TOLERANCE (time_blocks.block_complete). An
                    # incomplete block is stored with an EMPTY volume — not 0,
                    # which claims nothing traded, and not the partial sum, which
                    # reads as a quiet or busy block. 2026-09-02, TCS: block 5 had
                    # no readings and was stored as 0; block 8 took the whole of a
                    # 36-minute gap's volume.
                    #
                    # Judged against the REAL block end at every write in the
                    # bracket: a write more than the tolerance before the end
                    # stores "empty", the next write overwrites it, and the last
                    # write stands — so an outage across the boundary leaves the
                    # block marked incomplete instead of silently short.
                    complete = block_complete(time_block, reading_clock(prev_ts),
                                              reading_clock(first_ts), reading_clock(last_ts))
                    cumulative_volume = block_sum if complete else None

                    # A block cannot have negative volume. Refused, not written:
                    # Volume Block takes log(1 + volume), which fails below -1,
                    # and one such row failed it for every stock at the
                    # 2026-09-07 close.
                    if cumulative_volume is not None and cumulative_volume < 0:
                        logger.warning(f"Refusing negative volume {cumulative_volume} for "
                                       f"{symbol} ({asset_id}/{segment_code}) block {time_block}")
                        continue

                    if cumulative_volume is None:
                        logger.debug(f"Block {time_block} not fully observed for "
                                     f"{symbol} ({asset_id}/{segment_code}) — stored empty")

                    # Store time block volume (INSERT OR REPLACE for safety)
                    conn.execute("""
                        INSERT OR REPLACE INTO time_block_volumes
                        (date, asset_id, symbol, segment_code, time_block, cumulative_volume, timestamp, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, [current_date, asset_id, symbol, segment_code, time_block, cumulative_volume,
                          current_time.isoformat(), datetime.now(IST).isoformat()])
                    written += 1

                conn.commit()
                logger.debug(f"Stored {written} time block rows (block {time_block})")

        except Exception as e:
            logger.error(f"Error storing time block volumes: {e}")

# ================================
# MAIN EXECUTION
# ================================

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received signal {signum} - shutting down gracefully")
    if 'engine' in globals():
        engine.stop()
    sys.exit(0)

def main():
    """Main entry point"""
    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("=== OHLC Data Collection Engine ===")
        logger.info(f"Configuration:")
        logger.info(f"  - Refresh interval: {REFRESH_INTERVAL_SECONDS} seconds")
        logger.info(f"  - Database path: {DATABASE_PATH}")
        logger.info(f"  - API rate limit: {API_RATE_LIMIT_SECONDS} seconds")

        # Create and start engine
        global engine
        engine = OHLCEngine()

        # Log schedule configuration
        config = engine.schedule_manager.schedule_config
        logger.info("Schedule Configuration:")
        logger.info(f"  - Enabled: {config['enabled']}")
        logger.info(f"  - Schedule Type: {config['schedule_type']}")
        logger.info(f"  - Time Window: {config['start_time']} - {config['end_time']}")

        if not config['enabled']:
            logger.warning("Scheduling is DISABLED - engine will run continuously 24/7")

        if engine.start():
            logger.info("Engine started successfully - press Ctrl+C to stop")

            # Keep main thread alive
            try:
                while engine.running:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
        else:
            logger.error("Failed to start engine")
            return 1

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1
    finally:
        if 'engine' in globals():
            engine.stop()

    return 0

if __name__ == "__main__":
    sys.exit(main())