# fno-board

Live price charts for every NSE F&O stock and the major indices, all on one page.

Instead of opening each derivative chart one by one in a trading app, fno-board shows a grid of small 1-minute price charts that update as prices move: about 220 F&O stocks in one tab and 30 NSE/BSE indices in another.

## Features

- One chart tile per instrument with latest price and % change since the session's first price
- Two tabs: **F&O Stocks** and **Indices** (Nifty 50, Sensex, Bank Nifty, Fin Nifty and more)
- Filter by name, sort by top gainers or losers
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

## Status

Early project. Demo mode and the Groww login and instrument list are tested. The live Groww price feed and candle history are wired from the SDK documentation but have not yet been run against a subscribed account during market hours, so expect fixes there.

## Disclaimer

This is a personal tool for viewing prices. It is not affiliated with Groww, NSE or BSE, it places no orders, and nothing in it is investment advice. Market data is subject to your broker's terms; use your own API credentials and keep them out of version control.

## License

[MIT](LICENSE)
