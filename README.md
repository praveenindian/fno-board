# fno-board

Live price charts for every NSE F&O stock and the major indices, all on one page.

Instead of opening each derivative chart one by one in a trading app, fno-board shows a grid of small 1-minute price charts that update as prices move: about 220 F&O stocks in one tab and 30 NSE/BSE indices in another.

## Features

- One chart tile per instrument with latest price and % change since the session's first price
- Two tabs: **F&O Stocks** and **Indices** (Nifty 50, Sensex, Bank Nifty, Fin Nifty and more)
- Click any tile for a detail view: a larger chart with a hover crosshair, the day's open/high/low, previous close, volume and 52-week range, plus the nearest futures contract's price, premium to spot, open interest, lot size and expiry
- **Archive**: every finished trading day is saved as one JSON file per stock and index, and can be reopened in the same grid from the Archive switch
- Filter by name, sort by top gainers or losers
- Under a gainers or losers sort the ranking refreshes every 15 seconds without tiles sliding around: a stock that comes into view gets a ring that fades, and the biggest movers already in view get a small ▲/▼ marker with the places moved. Changing the sort order glides tiles to their new places. Both respect the system's reduce-motion setting
- Small / Medium / Large presets, or set exact column and row counts
- Layout is remembered per tab and per screen, so a laptop and a monitor keep separate views
- Light and dark themes that follow your system
- Demo mode with simulated prices, so you can try it without any account
- Lightweight: one Python file and one HTML file, no build step

## Requirements

- Python 3.9 or newer
- For real prices: a [Groww](https://groww.in) account with an active Trade API subscription. Demo mode needs neither.

## Setup

```bash
git clone https://github.com/praveenindian/fno-board.git
cd fno-board
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## Run

Try it with simulated prices:

```bash
DATA_SOURCE=demo .venv/bin/python server.py
```

Then open http://127.0.0.1:8765.

For real prices, put your Groww API key and secret in `.env`, then:

```bash
.venv/bin/python server.py
```

Groww's login token lasts until the next morning, so restart the app each trading day. Groww may also ask you to approve the API key daily on its API Keys page.

## Archive

After the market closes, fno-board saves the day for every stock and index it lists, one plain JSON file each:

```
archive/
  2026-11-20/
    RELIANCE.json
    NIFTY.json
    ...
```

Each file holds the day's open, high, low, close, previous close and volume, plus every 1-minute candle as `[time, open, high, low, close, volume]`, where `time` is seconds since 1970 (UTC). Where the instrument has futures, `fut` holds the nearest contract's symbol, expiry, lot size and its figures for the day: open, high, low, close, volume and open interest, with the previous session's close and open interest. Only this summary is kept for futures, not their minute candles. When a day is saved late, after a contract has expired, the figures are for the contract that is nearest at the time of saving, as named in `fut`. Open, high and low are taken from 9:15 am onward; the pre-open candles stay in the `candles` list.

- **When it saves:** at 4:05 pm IST if the board is running, and otherwise the next time you start it. Each start also saves any of the last five sessions that are missing, so a few days without running it leave no gap. Skip more than five sessions and those days are not recovered.
- **Viewing a day:** switch from **Live** to **Archive** at the top of the page and pick a date. The tabs, filter, sort and sizes work as they do for live prices, and the detail view has a link to download that instrument's file.
- **Fetching a file directly:** `http://127.0.0.1:8765/archive/2026-11-20/RELIANCE.json`. `/api/archive` lists the saved days.
- **If candles cannot be fetched** at closing time, the prices the board collected that day are saved instead (`"kind": "prices"`, no volume) and replaced with full candles on a later start.

Demo mode saves its simulated days to `archive-demo/`, separate from real ones. Both folders are ignored by git.

### Open it on your phone

Start the server with `HOST=0.0.0.0` and open `http://<your-computer-ip>:8765` from a device on the same Wi-Fi. Anyone on that network can view the page while it runs this way.

## Settings

Set these in `.env` or the environment.

| Variable | Default | Purpose |
|---|---|---|
| `GROWW_API_KEY`, `GROWW_API_SECRET` | none | Groww Trade API credentials |
| `DATA_SOURCE` | `groww` | `demo` for simulated prices |
| `HOST` | `127.0.0.1` | `0.0.0.0` to allow other devices on the network |
| `PORT` | `8765` | Port to serve on |
| `ARCHIVE_DIR` | `archive` | Folder for saved days, relative to the project or an absolute path |

## Status

Early project. Demo mode, the Groww login and instrument list, quotes, 1-minute candle history and the archive of past sessions have been run against a subscribed account. The live Groww price feed and the 4:05 pm save have not yet been run during market hours, so expect fixes there. Groww returns no candles for a few indices (NIFTYMIDSELECT, BSEIPO, BSEMIDCAP, BSESMLCAP), so those tiles stay empty and are not archived.

## Disclaimer

This is a personal tool for viewing prices. It is not affiliated with Groww, NSE or BSE, it places no orders, and nothing in it is investment advice. Market data is subject to your broker's terms; use your own API credentials and keep them out of version control.

## License

[MIT](LICENSE)
