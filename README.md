# TA Gyaani

A live technical-analysis dashboard for Indian equities that runs on your own computer. It watches 86 instruments — 82 NSE stocks and four indices (Nifty, Bank Nifty, Midcap Nifty, Sensex) — fetches their prices from your Dhan account every 5 seconds, runs nine calculators on every one of them about every 30 seconds, and shows the results on one page in your browser at `http://localhost:8054`.

Each calculator compares what a stock is doing now with what the same stock usually does at the same time of day, so the page can tell you not just that a number moved but whether that move is unusual for that stock.

It runs entirely on your machine, in Docker, behind a login. Nothing is sent anywhere except the price requests to Dhan: the page's stylesheet and fonts are part of the app, not fetched from the internet.

---

## Contents

- [What you need](#what-you-need)
- [Setup](#setup)
- [Day-to-day use](#day-to-day-use)
- [Two files you keep up to date](#two-files-you-keep-up-to-date)
- [Navigating the dashboard](#navigating-the-dashboard)
- [Understanding the analytics](#understanding-the-analytics)
- [Understanding the charts](#understanding-the-charts)
- [Building up history](#building-up-history)
- [What runs automatically](#what-runs-automatically)
- [Renewing your Dhan token](#renewing-your-dhan-token)
- [Where your data lives](#where-your-data-lives)
- [Running without Docker, and the tests](#running-without-docker-and-the-tests)
- [Troubleshooting](#troubleshooting)
- [Licence](#licence)

---

## What you need

- **Docker Desktop** (Mac or Windows), or Docker with Docker Compose on Linux — https://www.docker.com/products/docker-desktop/
- **A Dhan trading account with API access**: you need its client ID and an access token.
- An internet connection: the first start downloads the Python base image and the Python packages, and the app reaches Dhan for prices.

---

## Setup

### 1. Fill in the settings file

In the project folder, copy `.env.example` to a new file named `.env`, open it in a text editor and fill in the four required lines:

```env
DHAN_CLIENT_ID=your Dhan client ID
DHAN_ACCESS_TOKEN=your Dhan access token
DASHBOARD_USER=any username you choose
DASHBOARD_PASSWORD=any password you choose
```

The two Dhan values come from your Dhan account's API settings. The username and password are your own choice: the browser asks for them when you open the dashboard, and the app will not start without them. `.env.example` also lists three optional settings with their defaults; you can leave those alone.

`.env` stays on your computer. It is listed in `.gitignore` and is not built into the Docker image. The app rewrites the `DHAN_ACCESS_TOKEN` line itself when it renews your token (see [Renewing your Dhan token](#renewing-your-dhan-token)).

**Is this safe?** The dashboard only listens on your own computer (`127.0.0.1`) — it isn't reachable from your home network, your router or the internet, even by other devices on your Wi-Fi. The login is a second layer on top of that.

### 2. Start it

- **Mac:** double-click `Start.command`. If macOS will not open it the first time, right-click it and choose Open.
- **Windows:** double-click `Start.bat`.
- **Linux, or any terminal:** in the project folder, run `docker compose up -d --build`.

The Start file checks that Docker is installed and running (and starts Docker Desktop if it is not), checks that `.env` exists and has the four required values — and says which are missing if not — then builds and starts the app and opens the dashboard in your browser. The first start takes a few minutes; later starts take seconds unless the code changed.

### 3. Open the dashboard

Go to `http://localhost:8054` and log in with the username and password from `.env`.

Outside market hours (before 09:15 or after 15:31 IST, weekends and holidays) the dashboard keeps showing the last trading session's values rather than going blank: the header reads `⚠ STALE` with how long ago they were computed, or `⚠ STALE · data from …` if the app was started out of hours. On the very first start, before any session has been recorded, it reads `○ NO DATA`.

### 4. Stop it

Double-click `Stop.command` (Mac) or `Stop.bat` (Windows), or run `docker compose down`. Your data in `data/` is kept.

### Optional: `config.json`

A handful of settings you rarely need to touch: the ATR period and the three EMA spans used on the price chart, how many daily rows the beta calculation reads, the starting value of the "unusual" threshold (which you then adjust live from the ⚙ Settings panel), and the market hours the app collects in. Collection runs 09:15–15:31 IST on trading days — one minute past the 15:30 close on purpose, so the closing auction's result, which can arrive a few seconds after 15:30, is still caught. Leave the end at 15:31: block 25's cut-off (`calculators/time_blocks.py`) is the same minute, and a test fails if the two differ.

After editing `config.json`, run `docker compose restart` (no rebuild needed).

---

## Day-to-day use

| What you want to do | How |
|---|---|
| Start the app | `Start.command` / `Start.bat`, or `docker compose up -d --build` |
| Stop the app | `Stop.command` / `Stop.bat`, or `docker compose down` |
| Watch what it's doing | `docker compose logs -f` |
| Pick up an edit to `config.json`, `data/holidays.txt` or `assets/assets_select.csv` | `docker compose restart` |
| Pick up a code update | `docker compose up -d --build` (the Start file does this) |

Once started, the app keeps itself running: it restarts automatically if it crashes or if one of its parts stops, and comes back after a reboot once Docker Desktop is running.

**Start it before 09:15 and leave it running until about 15:35.** Two things depend on it: the opening window (09:15–09:30) that Momentum and Relative Strength measure, and the end-of-day housekeeping at 15:34 (see [What runs automatically](#what-runs-automatically)).

---

## Two files you keep up to date

### `data/holidays.txt` — market holidays

On a date listed here the app does nothing at all: no price fetching, no calculations, none of the end-of-day housekeeping. One date per line as `dd/mm/yyyy`; lines starting with `#` are notes.

**The file ships with the NSE trading holidays for 2026 only. Add each new year's dates** as soon as NSE publishes its trading-holiday list for that year — it does so late in the year, on nseindia.com. If a year has no dates in the file, the app logs `HOLIDAY CALENDAR … lists no holiday in <year>` and, on every holiday of that year, collects and archives prices as if the market had been open: a false day in every history the analytics compare against, for up to a year. A missing file, or a line that is not a date, is also logged as an error, and the app carries on with whatever dates it could read.

Restart the app after editing the file (it is read once when the app starts).

### `assets/assets_select.csv` — the 86 instruments

One row per instrument. The columns the app uses:

| Column | What it is for |
|---|---|
| `asset_id`, `segment_code` | Dhan's security ID and segment (`NSE_EQ` for stocks, `IDX_I` for indices). Together they identify the instrument — the same ID can mean different things in different segments (25 is Bank Nifty in `IDX_I` and Adani Enterprises in `NSE_EQ`). |
| `asset_name`, `exchange` | The name shown on the page; the exchange. |
| `local_high`, `global_high`, `local_low`, `global_low` | **Your own price levels** for the Price Levels calculator and the Breach column — see below. |
| `index` | Which of the four indices the stock belongs to, as a comma-separated list using exactly these spellings: `NIFTY`, `BankNifty`, `Midcap`, `SENSEX`. It decides which benchmarks Relative Strength compares the stock with, and which index cards count it. |
| `weight_nifty`, `weight_bank`, `weight_midcap`, `weight_sensex` | The stock's weight in each index, in percent. The index cards' weighted figures and top contributors use them. |

`number_expiries`, `strike_size`, `lot_size` and `heavy` are informational; nothing reads them.

**The price levels ship empty.** Fill in any of the four for a stock — for example a recent swing high as `local_high` and a 52-week high as `global_high` — and the app tracks distance to each level, warns as price approaches, and records every crossing for the day. Until at least one stock has a level, the Price Levels calculator has nothing to report: its entry in the header's status strip stays at `Levels ·` and the Breach column stays empty. That is expected, not a fault.

**Keep the list current.** Index membership and weights change, and companies merge, demerge and change symbols. The app does not update this file; edit it when the indices change. Restart the app after editing it.

---

## Navigating the dashboard

One main screen with a table, plus panels that slide in when you need them.

**The main table** lists all 86 instruments, one row each, refreshing every 30 seconds. Click a column header to sort; type in the filter boxes under the headers to narrow the list (the Δ column is the only numeric one, so `>2` works there; the others filter by text). A row is highlighted when one of its signals is unusual — the same rules that light the glyphs in its Signals column (see below).

**Click any row** to open the detail panel on the right — every metric for that stock, with a "why" list at the top naming each signal that is lighting the row: price-level crossings and breakouts first, then the unusual readings, largest first. The small arrow tab on the right edge of the screen opens and closes this panel.

**Two drawers along the bottom**, each opened and closed by its tab:

- **▸ INDICES** — pick Nifty, Bank Nifty, Midcap or Sensex with the buttons at the top (the card's border is green when the index is up on the day, red when down). The card shows **breadth** — how many constituents are advancing or declining, above or below VWAP, outside their opening range upward or downward, showing unusually high or low block volume, moving unusually far up or down for the time of day, beating or lagging the index on relative strength, unchanged, or past one of their price levels; **weighted** figures for the index — return, return without its five heaviest stocks, volume velocity, volume z-score, distance from VWAP; the five biggest **contributors and detractors**; and six small charts through the day (breadth, dispersion of returns across constituents, weighted volume velocity, weighted return, weighted return against the same without the five heaviest, weighted VWAP distance).
- **▸ CHARTS** — pick any stock or index and see its price chart and eight signal charts in three tabs: **Price**, **Signals** and **Relative**.

**The ⚙ gear** (top right) opens the settings panel:

- **The "unusual" threshold** — a slider from 1.5 to 3.0 (default 2.0). A reading counts as unusual when its z-score is further from zero than this, in either direction. Everything on the page uses the same number.
- **The chart window** — 1h, 2h, 4h, 8h or 1d (the whole trading day shown). A chart never mixes two days.
- **The comparison window** — 22d, 90d, 180d or 252d: what "normal" is measured against — roughly the last month, quarter, three quarters or year of the same time slot. The table, the highlights, the glyphs, the "why" list and the index cards all switch together.

A stock can be busy compared with the last month and ordinary compared with the last year — switching between 22d and 252d shows the difference. A window without enough history yet shows a hyphen; all four buttons stay available, so the hyphens tell you which signals are still accumulating. The app opens on the longest window it has data for. All three settings are remembered in your browser.

**The header.** On the left, a status strip shows how long ago each of the nine calculators last stored a result (`Mom`, `Bkout`, `VWAP`, `RS`, `VolDyn`, `PVDiv`, `VolBlk`, `Levels`, `RollMom`); an entry turns red when it is overdue, and a grey dot means it has not reported since the app started. Below the header, `Daily history: N trading days` counts the days of daily prices on record (it disappears at 252). On the right:

| Shows | Means |
|---|---|
| `● LIVE` | Calculations are current and prices are arriving. |
| `⚠ PRICES STALE (age)` | Calculations are running, but no new price has arrived from Dhan for over 30 seconds — an expired token or a dropped connection — so every number is being computed from old prices. |
| `⚠ NO PRICES` | Calculations ran but no price has arrived since the app started. |
| `⚠ STALE (age)` | The last calculation cycle is more than a minute old. |
| `⚠ STALE · data from …` | Outside market hours: the last session's values, read back from disk. |
| `○ NO DATA` | Nothing recorded yet (a fresh install). |

---

## Understanding the analytics

Every stock gets nine reads, refreshed about every 30 seconds.

### z-scores and comparison windows

Many numbers come with a **z-score**, shown as `z 2.4` or `(z+1.8)`. It answers one question: *is this reading unusual for this stock, at this time of day, compared with how it normally behaves?*

- **Around 0** — normal.
- **Beyond ±2** — roughly the most extreme 1 in 20 readings.
- **Beyond ±3** — rare; roughly 1 in 300 or less.

The z-score is `(today's value − the average) ÷ the spread` of the same quantity at the same 15-minute slot on earlier trading days. It is computed against **four windows** — exactly the last 22, 90, 180 and 252 earlier sessions — all of them, all the time. The comparison-window buttons choose which one the table, highlights and glyphs show; the detail panel always shows all four side by side, with each window's average (μ), spread (σ) and history count (n). The spread is the population standard deviation (dividing by n). Because the average, spread and z come from the same calculation, `(value − μ) ÷ σ` in the detail panel reproduces the z exactly.

**A window needs exactly its own number of days.** 22 means 22: a window short of its history shows a hyphen, and the detail panel's `n` shows how far along it is (for example `n 47` against the 252d window). A hyphen says "not enough history to judge", which is different from a small number saying "nothing unusual". If every earlier value was identical the spread is zero and the z is undefined, so it is shown as a hyphen too, never as 0.

Each comparison slot fills at one sample per trading day, so the 22d window starts working about a month after you begin, 90d after about four and a half months, and 252d after about a year — per stock and per 15-minute slot.

**Δ (the last table column)** is how much the windows disagree: for each of the row's signals, the 22d z minus the 252d z (or the 180d z while 252d is still empty), and the column shows the largest of these, as a size. Sort by it to find stocks whose recent behaviour differs from their long-run normal. Each signal's own Δ is in the detail panel, with its sign: positive means more unusual recently than historically.

**Every calculator also reports a status.** `valid` means it computed normally. Anything else names why some values are empty — for example `window_not_closed` (before 09:30), `window_missed` (the app was not running 09:15–09:30), `no_previous_close`, `insufficient_data` (no price yet), `insufficient_beta_history`, `incomplete_block`, `interrupted`, `invalid_range`, `no_price_variation`, `no_volume_variation`. A missing value is always shown as a hyphen, never as a placeholder 0, because a 0 would claim something that was not measured.

**Signs matter.** Almost everything here can be negative and negative is meaningful — a quiet block, an underperforming stock, fading volume. Only volumes, spreads, sample counts and distances from a range are never negative.

### Momentum (Mom column)

A read on **the open**, and only the open: it answers "was the open unusual?", then stays fixed for the day.

| Value | How it is computed |
|---|---|
| Gap | `ln(open ÷ previous close)` — the overnight move, as a log return |
| Opening move | `ln(price at 09:30 ÷ open)` — the 09:15→09:30 move, excluding the gap |
| Continuation | gap direction (+1 / −1) × opening move — positive when the opening move followed the gap, negative when it fought it |
| Acceleration | today's opening move − yesterday's |
| z-scores | gap and opening move, each against the same quantity on earlier sessions |

Until 09:30 the column shows the gap and its z; from 09:30 the opening move and its z. Example: a stock gaps up 2% and gains another 1% by 09:30 — continuation +0.01, "the gap is holding". The same gap followed by a fall of 0.5% gives continuation −0.005, "the gap is fading". If the app was not running between 09:15 and 09:30 there is no opening move to measure, so the column stays blank for the day (status `window_missed`) rather than measuring some other window under the same name.

### Rolling Momentum (R.Mom column)

The stock's move since yesterday's close **right now**, in percent — `(price ÷ previous close − 1) × 100` — with a z-score against how far it had moved by the same 15-minute slot on earlier days. It keeps updating all day. The same +2% can be striking at 09:45 and ordinary at 15:00; the z-score accounts for the time of day.

### Breakout (Breakout column)

Structure, not a signal: is the price outside the range it traded in during 09:15–09:45?

| Value | Meaning |
|---|---|
| Range high / low / width | The 09:15–09:45 high and low; width is `ln(high ÷ low)` |
| Position | How far outside the range, in range-widths: above it, `ln(price ÷ high) ÷ width`; below it, `ln(price ÷ low) ÷ width` (negative); 0 inside |
| Now | +1 when this reading and the one before are both above the range, −1 when both are below, 0 otherwise — clears the moment price comes back |
| First breakout | The direction of the day's first move out of the range. Set once and never cleared |
| Accepted / failed | Whether the last two readings after that first breakout are still beyond the range (accepted), or the latest is back inside (failed) |

The column shows the position while price is outside the range right now, and a hyphen otherwise. The ▲ glyph and the row highlight follow "Now". Example: range 100–105, price 106 → position ≈ 0.19, a modest breakout; price 110 → ≈ 0.95, almost a full range-width out.

### VWAP (VWAP column)

VWAP is the day's volume-weighted average price — the exchange's own figure, as Dhan reports it. The column shows how far the price is from it, `ln(price ÷ VWAP)` in percent; the detail panel adds its z-score against the same slot on earlier days, and whether the distance is widening or closing since the previous cycle. A large z means the price is unusually far from its average for the time of day: either breaking away, or stretched. Indices have no traded volume and so no VWAP; their rows say `no_volume_variation`.

### Relative Strength (RelStr column)

Is the stock beating or lagging its index, after allowing for how strongly it normally moves with that index (its **beta**)?

- `beta = covariance(stock, index) ÷ variance(index)`, from daily log returns, refitted every trading morning, one beta per window (22/90/180/252 days).
- **Session RS** = `ln(stock open ÷ previous close) − beta × ln(index open ÷ previous close)`.
- **Intraday RS** = the same over 09:15→09:30.
- The z-scores compare each against earlier sessions rebuilt with **the same window's beta** — beta and the comparison window are one setting, so a 22d number is always measured and judged with the 22d beta.
- **Acceptance** = yesterday's opening-window RS × today's: positive if the stock outperformed (or underperformed) again, negative if it flipped.

The column shows intraday RS and its z for each benchmark the stock belongs to (a stock in both Nifty and Sensex shows two lines). Example: beta 1.2, the stock opens +1.5%, the index +1.0% → session RS ≈ +0.003, genuine outperformance; if the stock had opened only +0.5% it would read ≈ −0.007 — lagging its expected move even though it rose. Until beta has history the row reads `insufficient_beta_history`.

### Block Volume (Block Volume column)

The day is split into 25 blocks of 15 minutes, 09:15–15:30 (block 25 also takes the closing auction's result, up to 15:31). This shows how unusual the volume in the latest completed block is against the same block on earlier days — 11:00–11:15 today against 11:00–11:15 normally, so the busy open is not compared with lunchtime. The z is on `ln(1 + volume)`, so the detail panel labels its average and spread `μ log` / `σ log`. The panel also shows the session's running total so far, compared like for like with the same blocks on earlier days.

A block's volume is only trusted if the app had a reading within 30 seconds of both its start and its end. If not — a late start, a dropped connection — the block reads `incomplete_block` ("not fully observed") instead of a number, and it is left out of the long-term history rather than stored wrong.

### Volume Dynamics (the ↗ glyph and the detail panel)

How Block Volume's z is changing from block to block:

- **Velocity** = this block's z − the previous block's: is unusual participation building or fading?
- **Acceleration** = this velocity − the previous one: is that change speeding up?
- **Persistence** = the average z over the last three blocks.
- **State**: +2 expanding rapidly, +1 expanding, 0 steady, −1 contracting, −2 collapsing.

The ↗ glyph lights when velocity is beyond the threshold in either direction — green when participation is expanding, red when it is collapsing. A block not fully observed has no velocity, and neither does the one after it.

### Price-Volume Divergence (the ρ glyph and the detail panel)

Over the last 15 minutes of 5-second readings, are price changes and volume changes moving together (volume backing the move) or apart?

| Value | Meaning |
|---|---|
| Pearson r (ρ) | Correlation of reading-to-reading price changes with volume changes, −1 to +1 |
| Spearman r, winsorised r | Two cross-checks that ignore extreme single readings |
| Critical r | The 95%-confidence floor for this many readings: `tanh(1.96 ÷ √(n − 3))`. A correlation inside ±critical r is indistinguishable from noise |
| Reliable | Yes when `|r|` is beyond critical r |
| Agreement | The average of the three estimators' signs: +1 or −1 when all three agree; in between when one extreme reading is driving Pearson r |
| z-score | Of `atanh(r)` against the same block on earlier days |

Example: 179 quiet readings and one huge print can give Pearson r = −0.93 while the other two estimators read near zero — agreement ≈ 0.33, an outlier, not a real relationship. Statuses: `no_price_variation` (the price never moved — an illiquid stock, or one locked at a circuit limit), `no_volume_variation`, and `interrupted` — if the readings had a gap of more than 30 seconds inside the window, one reading would carry the whole gap's volume, so there is no value until that gap leaves the window. The day's first reading is always left out, because it carries everything traded before it.

### Breach column (Price Levels)

For each level you set in `assets/assets_select.csv`: the distance to it (percent), which side of it the price is on, whether the price is approaching it (within 0.2% and getting closer), whether it crossed on the latest reading, and — kept for the rest of the day — every level crossed today. The column shows the day's crossings: `1H` for one high level, `2L` for two low levels, and so on. See [`assets/assets_select.csv`](#assetsassets_selectcsv--the-86-instruments) for setting levels.

### The Signals column glyphs

Next to each name is a row of nine fixed slots, one per signal, always in the same order, so you can scan by pattern:

| Glyph | Lit when |
|---|---|
| ▲ | Price is outside its opening range right now (two readings in a row) |
| ■ | Block volume is unusual |
| ● | The opening move (09:15–09:30) was unusual |
| α | Relative strength is unusual against any of its benchmarks |
| ρ | The price-volume correlation is unusual |
| ✖ | A price level was crossed today |
| ↗ | Volume participation shifted sharply this block (green expanding, red contracting) |
| ‖ | Price is unusually far from VWAP for the time of day |
| ↻ | The move since yesterday's close is unusual for the time of day |

"Unusual" means beyond the threshold you set, at the comparison window you chose — the same rule that highlights rows. A row with several lit glyphs is worth opening.

### Momentum and Rolling Momentum, Volume Block and Volume Dynamics

They are pairs, not duplicates. Momentum separates the gap from the opening move and relates them; Rolling Momentum sees one blended number but covers the whole day. Volume Block is a level ("is volume unusual now?"); Volume Dynamics is its rate of change ("is that building or fading?") — a moderate Block Volume z with state +2 is an early sign; a very high one with state −2 may mean the burst is already over.

---

## Understanding the charts

Open **▸ CHARTS**, pick a stock or index, and use the three tabs.

**Price.** Five-minute candles (green: the five minutes ended higher than they started; red: lower) with volume bars underneath (stocks only), and:

- **VWAP** — the day's volume-weighted average price.
- **EMA-5m / EMA-15m / EMA-30m** — fast, medium and slow moving averages (spans in `config.json`).
- **Day high / low** — dashed lines at the session's high and low so far.
- **PrevClose** — the previous trading day's close.
- **LH / GH / LL / GL** — your price levels for the stock, if any.
- **A shaded box at the open** — the 09:15–09:45 opening range that Breakout measures against.
- **Show ATR Band** (checkbox above the chart) — PrevClose ± the average true range of the 14 trading days before the session shown: a rough gauge of a normal day's range.

**Signals.** Volume velocity and acceleration (one line per comparison window), VWAP distance in percent, Momentum's opening-move z, and Rolling Momentum's z.

**Relative.** Relative strength's intraday z for one benchmark at a time (pick it with the buttons above the chart if the stock has more than one), Block Volume's z, and the price-volume correlation.

On the z-score charts the selected comparison window's line is drawn heavier, and a shaded band marks the "unusual" zone from your threshold outward, so a line entering the band is a reading that lights its row. The price-volume chart shades its noise band (inside ± critical r) instead.

---

## Building up history

The z-scores need a track record. Until there is enough, they show hyphens — not wrong, just not yet meaningful. History builds up by itself from the days the app runs; there is no download step.

**Catching up a missed archive.** The app keeps its detailed 5-second readings for the last 5 trading days, and archives each day itself at 15:34. If it was stopped before 15:34 on a day, archive that day (or all five) afterwards:

```bash
docker compose exec trading_system python scripts/backfill.py --days 5
```

It asks before writing. It archives each day, writes each day's official close (taken from the next trading day's first quote), rebuilds those days' 15-minute snapshots, and refits beta at the end. Running it again over days already archived is safe. Other choices: `--all`, `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`, `--preview` (check without writing), `--allow-partial-coverage` (also store a day the app ran for only part of).

**What only live running can build.** The VWAP, Rolling Momentum and price-volume comparisons, and the opening-window comparisons of Momentum and Relative Strength, are built from the app's own 5-second readings; daily or 5-minute candles cannot supply them. Expect their 22d window roughly 22 trading days after you start running the app every day.

---

## What runs automatically

**Every trading day at 15:34 IST** (it takes about ten seconds — keep the app running until about 15:35):

1. The day's prices are archived into long-term storage, and the 5-second store is trimmed to the last 5 trading days.
2. The other stores are trimmed to their retention periods (see [Where your data lives](#where-your-data-lives)).
3. Your Dhan access token is renewed and written back into `.env` — always last, because Dhan invalidates the old token the moment the new one is issued.

**Every trading morning**, on the first price update, the app records the previous trading day's official close (it arrives in that morning's first quote — the day's own close is not known at 15:34) and refits every beta. Both happen before the first numbers are calculated.

**On a holiday listed in `data/holidays.txt`**, none of this runs, and nothing is fetched.

If the app is stopped at 15:34, that day's housekeeping does not run: archive the day later with the backfill command above, and renew the token by hand if needed.

---

## Renewing your Dhan token

The app renews the token every trading day at 15:34, as long as it is running. To renew it by hand — the app was stopped at 15:34, or you see an authentication error — double-click:

- **Mac:** `renew_token.command`
- **Windows:** `renew_token.bat`

**First stop the app**, then double-click; a window opens, renews the token, and shows `SUCCESS` or `FAILED` (press any key to close). **Then start the app again.** A copy of your previous `.env` is saved first as `data/.env.bak`.

---

## Where your data lives

Everything is under `data/`, which is kept when the app stops or is rebuilt:

| File | Holds | Kept for |
|---|---|---|
| `ohlc.db` | Every 5-second reading and the live 15-minute block volumes | the last 5 trading days |
| `metrics.db` | Every calculator's output, for the charts and for showing the last session out of hours | 7 days |
| `historical.db` | Daily prices and 15-minute block volumes (the long-term history) | indefinitely |
| | The per-block snapshots VWAP, Rolling Momentum, price-volume and the opening window compare against | about 420 calendar days |
| | The beta table | refitted daily |
| `holidays.txt` | Market holidays (see above) | — |
| `daily_scheduler_state.json` | Which day the 15:34 housekeeping last ran, so a restart does not repeat it | — |
| `logs/` | `ohlc_engine.log` (the app), `historical_extraction.log` (the 15:34 archive), `historical_backfill.log` | a new file each day, 30 kept |
| `.env.bak` | The previous `.env`, from the last token renewal | — |

The databases are created automatically on the first start. To start from nothing, stop the app and delete the `.db` files (and any `-wal` and `-shm` files beside them).

---

## Running without Docker, and the tests

Python 3.13 (the version the Docker image uses), in the project folder:

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.lock    # every package at the tested version
python main.py                      # the whole app, http://localhost:8054
```

> **Note: without Docker, the app is open to your network.** Started with `python main.py`, the dashboard (port 8054) and the app's internal price feed (port 5556) accept connections from any device that can reach your computer — for example, anyone on the same Wi-Fi — not just from your computer. The dashboard still asks for its username and password. The price feed has no login: anyone who connects to it receives the live prices and volumes the app is collecting (market data only — no token, password or account details). Started with Docker (the Start files, or `docker compose up`), both are reachable from your computer alone.

It reads `.env` from the project folder. Run the tests from the project folder with `pytest` (they need no `.env`, no market data and no network). `requirements.txt` lists the top-level packages; `requirements.lock` pins everything, and the Docker image installs the lock.

How it is built: one Python process (`main.py`) runs five parts — the price engine (`engine.py`: Dhan quotes every 5 seconds into `data/ohlc.db`, passed on over a local ZeroMQ socket), the metrics loop (`metrics.py`: the nine calculators in `calculators/` plus the index cards), the 15:34 scheduler, a watchdog that restarts the process if a part stops, and the dashboard (`dashboard.py`, `dashboard_signals.py`; Dash, Plotly, Bootstrap). Each calculator is a pure function (`*_calculator.py`, `volume_block.py`, `volume_dynamics.py`) wrapped by an adapter (`*_adapter.py`) that fetches its data.

---

## Troubleshooting

- **The Start file says the settings file is missing or incomplete** — see [Setup](#1-fill-in-the-settings-file).
- **`⚠ PRICES STALE` during market hours** — no new price for over 30 seconds. Usually an expired token (renew it — see above) or the network. `docker compose logs -f` shows the error.
- **Momentum and Relative Strength are blank all day** (`window_missed`) — the app was not running between 09:15 and 09:30. They return the next day.
- **`Levels ·` stays grey in the status strip, and Breach is empty** — no price levels are set; see [`assets/assets_select.csv`](#assetsassets_selectcsv--the-86-instruments).
- **Rate-limit errors in the log** — add `BASE_RATE_LIMIT_DELAY=3.0` to `.env` and restart.
- **One stock's charts are blank** — it may have no data yet in the chart window; try 1d in Settings.
- **The app will not start** — check `.env` (all four required values) and run `docker compose logs -f` for the error.
- **Port 8054 is in use** — another program has it. Stop that program, or change the first `8054` in `docker-compose.yml` (for example `"127.0.0.1:8055:8054"`) and open that address instead.

---

## Licence

MIT — see `LICENSE`. The bundled Bootstrap stylesheet is MIT licensed, and the Fraunces and JetBrains Mono typefaces are under the SIL Open Font License — see `THIRD_PARTY_LICENSES`. The Python packages are installed by pip from `requirements.lock` and keep their own licences.

---

From ChorBazaar — https://chorbazaar.co.in
