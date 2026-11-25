"""
Real-time Price Data Manager

Subscribes to ZMQ price feed from engine and maintains in-memory current prices.
Provides fast lookups for calculators without database queries.

Architecture:
- Background thread subscribes to ZMQ (port 5556)
- Updates in-memory dictionary on every price update (every 5 seconds)
- Provides get_current_prices() method for instant memory lookup
- Thread-safe access with locks
"""

import zmq
import json
import logging
import threading
import time
import os
import pandas as pd
from typing import Dict, List, Optional
from datetime import datetime

from calculators.time_blocks import IST

logger = logging.getLogger(__name__)

# ZMQ configuration (same as engine.py publishes on)
# Use environment variable for Docker (connects to 'engine' container)
# Falls back to localhost for local development
ZMQ_HOST = os.environ.get('ZMQ_HOST', '127.0.0.1')
ZMQ_PORT = os.environ.get('ZMQ_PUBLISH_PORT', '5556')
ZMQ_SUBSCRIBE_ADDRESS = f"tcp://{ZMQ_HOST}:{ZMQ_PORT}"


class PriceDataManager:
    """
    Manages real-time price data from ZMQ feed.

    Provides fast in-memory access to current prices for calculators.
    Runs background thread to continuously update from ZMQ.
    """

    def __init__(self, zmq_address: str = ZMQ_SUBSCRIBE_ADDRESS):
        """
        Initialize price data manager.

        Args:
            zmq_address: ZMQ address to subscribe to (default: tcp://127.0.0.1:5556)
        """
        self.zmq_address = zmq_address

        # In-memory storage: {(asset_id, segment_code): price_data_dict}
        self._price_data = {}

        # Thread safety
        self._lock = threading.Lock()

        # Background thread control
        self._running = False
        self._thread = None

        # ZMQ objects
        self._zmq_context = None
        self._zmq_subscriber = None

        # Statistics
        self._updates_received = 0
        self._last_update_time = None

    def start(self) -> None:
        """Start the background ZMQ subscription thread."""
        if self._running:
            logger.warning("PriceDataManager already running")
            return

        logger.info("Starting PriceDataManager...")

        # Initialize ZMQ
        self._init_zmq()

        # Start background thread
        self._running = True
        self._thread = threading.Thread(target=self._subscription_loop, daemon=True)
        self._thread.start()

        logger.info(f"PriceDataManager started, subscribed to {self.zmq_address}")

    def stop(self) -> None:
        """Stop the background thread and cleanup."""
        if not self._running:
            return

        logger.info("Stopping PriceDataManager...")
        self._running = False

        # Wait for thread to finish
        if self._thread:
            self._thread.join(timeout=5)

        # Cleanup ZMQ
        if self._zmq_subscriber:
            self._zmq_subscriber.close()
        if self._zmq_context:
            self._zmq_context.term()

        logger.info("PriceDataManager stopped")

    def _init_zmq(self) -> None:
        """Initialize ZMQ subscriber."""
        try:
            self._zmq_context = zmq.Context()
            self._zmq_subscriber = self._zmq_context.socket(zmq.SUB)
            self._zmq_subscriber.connect(self.zmq_address)

            # Subscribe to all PRICE.* messages
            self._zmq_subscriber.setsockopt_string(zmq.SUBSCRIBE, "PRICE.")

            logger.info(f"ZMQ subscriber connected to {self.zmq_address}")
        except Exception as e:
            logger.error(f"Error initializing ZMQ: {e}")
            raise

    def _subscription_loop(self) -> None:
        """Background thread loop - receives and processes ZMQ messages."""
        logger.info("ZMQ subscription loop started")

        while self._running:
            try:
                # Poll with timeout to allow checking _running flag
                if self._zmq_subscriber.poll(timeout=1000):  # 1 second timeout
                    # Receive message: "PRICE.123 {json_data}"
                    message_raw = self._zmq_subscriber.recv_string()

                    # Parse message
                    topic, json_data = message_raw.split(' ', 1)
                    price_data = json.loads(json_data)

                    # Update in-memory storage
                    self._update_price_data(price_data)

            except Exception as e:
                logger.error(f"Error in subscription loop: {e}")
                time.sleep(1)  # Brief pause before retrying

        logger.info("ZMQ subscription loop ended")

    def _update_price_data(self, price_data: Dict) -> None:
        """
        Update in-memory price data (thread-safe).

        Args:
            price_data: Price data dictionary from ZMQ message
        """
        try:
            asset_id = price_data.get('asset_id')
            segment_code = price_data.get('segment_code')
            if not asset_id or not segment_code:
                return

            with self._lock:
                self._price_data[(asset_id, segment_code)] = price_data
                self._updates_received += 1
                self._last_update_time = datetime.now(IST)

        except Exception as e:
            logger.error(f"Error updating price data: {e}")

    def _drop_other_days_locked(self) -> None:
        """Drop every quote not dated today (IST). Caller holds self._lock.

        Nothing else ever expires this store. Without this, a container left
        running overnight serves yesterday's closing prices to the first
        cycles of the next session, before the engine's first fetch, and the
        calculators stamp them with today's time. Checked per quote, by the
        timestamp the engine puts on each message, so a stock that has had no
        quote yet today is dropped even after others have arrived.
        """
        today = datetime.now(IST).date().isoformat()
        stale = [k for k, v in self._price_data.items()
                 if (v.get('timestamp') or '')[:10] != today]
        for k in stale:
            del self._price_data[k]
        if stale:
            logger.info(f"PriceDataManager: dropped {len(stale)} quotes not dated {today}")

    def get_current_prices(self, asset_ids: List[int] = None, segment_codes: List[str] = None) -> pd.DataFrame:
        """
        Get current price data for specified assets.

        Fast in-memory lookup - no database queries.

        Args:
            asset_ids: Optional list of asset IDs to filter by (None = all assets)
            segment_codes: Required whenever asset_ids is provided — positionally
                paired list of segment codes. Some asset_ids are shared across
                segments (e.g. 25 is BankNifty in IDX_I and ADANIENT in NSE_EQ);
                filtering by asset_id alone would match both under one asset_id.

        Returns:
            DataFrame with columns: asset_id, symbol, segment_code, last_price, open_price,
                                    high_price, low_price, volume
        """
        try:
            with self._lock:
                self._drop_other_days_locked()
                # Get all price data or filtered by (asset_id, segment_code) pairs
                if asset_ids:
                    valid_keys = set(zip(asset_ids, segment_codes))
                    filtered_data = {k: v for k, v in self._price_data.items() if k in valid_keys}
                else:
                    filtered_data = self._price_data.copy()

                if not filtered_data:
                    logger.warning("No price data available in memory")
                    return pd.DataFrame()

                # Convert to DataFrame
                df = pd.DataFrame(filtered_data.values())

                # Ensure required columns exist - include ALL fields calculators need
                required_cols = ['asset_id', 'symbol', 'segment_code', 'last_price', 'open_price',
                                'high_price', 'low_price', 'volume', 'average_price', 'interval_volume']

                for col in required_cols:
                    if col not in df.columns:
                        df[col] = None

                logger.debug(f"Retrieved {len(df)} price records from memory")
                return df[required_cols]

        except Exception as e:
            logger.error(f"Error getting current prices: {e}")
            return pd.DataFrame()

    def get_status(self) -> Dict:
        """
        Get manager status and statistics.

        Returns:
            Dictionary with status information
        """
        with self._lock:
            return {
                'running': self._running,
                'assets_tracked': len(self._price_data),
                'updates_received': self._updates_received,
                'last_update_time': self._last_update_time.isoformat() if self._last_update_time else None
            }

    def is_ready(self) -> bool:
        """
        Check if manager has received data and is ready.

        Returns:
            True if price data is available, False otherwise
        """
        with self._lock:
            self._drop_other_days_locked()
            return len(self._price_data) > 0
