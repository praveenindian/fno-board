"""fno-board: live price charts for every NSE F&O stock on one page.

Run:  .venv/bin/python server.py      then open http://127.0.0.1:8765
Set DATA_SOURCE=demo in .env (or the environment) for simulated prices.
"""
import asyncio
import datetime as dt
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

SOURCE = os.getenv("DATA_SOURCE", "groww").lower()
PORT = int(os.getenv("PORT", "8765"))
HOST = os.getenv("HOST", "127.0.0.1")   # set HOST=0.0.0.0 to open it to phones on the same Wi-Fi
INSTRUMENT_CSV = "https://growwapi-assets.groww.in/instruments/instrument.csv"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ist_date(epoch):
    return dt.datetime.fromtimestamp(epoch, IST).date()


class Board:
    """In-memory store: one list of [minute_epoch, price] points per symbol."""

    def __init__(self):
        self.lock = threading.Lock()
        self.symbols = {}      # sym -> {"name": str, "points": [[t, p], ...]}
        self.ticks = {}        # sym -> [t, p]   changed since last broadcast
        self.rehist = set()    # symbols whose whole series was replaced
        self.status = {"source": SOURCE, "state": "starting", "detail": ""}

    def set_status(self, state, detail=""):
        self.status = {"source": SOURCE, "state": state, "detail": detail}

    def add(self, sym, name, group="stocks", exchange="NSE", fut=None):
        with self.lock:
            self.symbols.setdefault(sym, {"name": name, "group": group, "exchange": exchange,
                                          "fut": fut, "points": []})

    def tick(self, sym, price, ts=None):
        ts = ts or time.time()
        minute = int(ts) // 60 * 60
        with self.lock:
            pts = self.symbols[sym]["points"]
            if pts and ist_date(pts[-1][0]) != ist_date(minute):
                pts.clear()                      # new trading day
                self.rehist.add(sym)
            if pts and pts[-1][0] == minute:
                if pts[-1][1] == price:
                    return
                pts[-1][1] = price
            elif not pts or minute > pts[-1][0]:
                pts.append([minute, price])
            else:
                return                           # stale tick
            self.ticks[sym] = [minute, price]

    def set_history(self, sym, hist):
        """Put historical points in front of whatever live points exist."""
        if not hist:
            return
        with self.lock:
            pts = self.symbols[sym]["points"]
            if pts and ist_date(pts[0][0]) != ist_date(hist[-1][0]):
                return                           # live data is a newer day
            first_live = pts[0][0] if pts else float("inf")
            pts[:0] = [p for p in hist if p[0] < first_live]
            self.rehist.add(sym)

    def snapshot(self):
        with self.lock:
            return [{"s": s, "n": v["name"], "g": v["group"], "p": [list(p) for p in v["points"]]}
                    for s, v in self.symbols.items()]

    def drain(self):
        with self.lock:
            ticks, self.ticks = self.ticks, {}
            hist = {s: [list(p) for p in self.symbols[s]["points"]] for s in self.rehist}
            self.rehist = set()
        for s in hist:
            ticks.pop(s, None)
        return ticks, hist


board = Board()


def fno_stocks(df):
    """Cash-market rows for every stock that has an NSE futures contract."""
    fut = df[(df.exchange == "NSE") & (df.segment == "FNO") & (df.instrument_type == "FUT")]
    underlyings = set(fut.underlying_symbol.dropna())
    cash = df[(df.exchange == "NSE") & (df.segment == "CASH") & (df.instrument_type == "EQ")
              & df.trading_symbol.isin(underlyings)]
    return cash.sort_values("trading_symbol")


HEADLINE_INDICES = ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "NIFTYMIDSELECT", "NIFTYJR",
                    "BANKEX", "INDIAVIX"]


def index_rows(df):
    """NSE and BSE indices, headline ones first, the rest alphabetical."""
    idx = df[(df.instrument_type == "IDX") & df.groww_symbol.notna()].copy()
    rank = {s: i for i, s in enumerate(HEADLINE_INDICES)}
    idx["rank"] = idx.trading_symbol.map(lambda s: rank.get(s, len(rank)))
    return idx.sort_values(["rank", "trading_symbol"])


# Index symbol -> the name its futures contracts use as underlying.
FUT_ALIAS = {"NIFTYMIDSELECT": "MIDCPNIFTY", "NIFTYJR": "NIFTYNXT50"}


def near_futures(df):
    """Nearest-expiry futures contract for each underlying symbol."""
    fut = df[(df.segment == "FNO") & (df.instrument_type == "FUT")].copy()
    fut["expiry_date"] = fut.expiry_date.astype(str)
    today = dt.datetime.now(IST).strftime("%Y-%m-%d")
    out = {}
    for r in fut[fut.expiry_date >= today].sort_values("expiry_date").itertuples():
        out.setdefault(r.underlying_symbol, {"exchange": r.exchange, "symbol": r.trading_symbol,
                                             "expiry": r.expiry_date, "lot": int(float(r.lot_size))})
    return out


def fut_for(futs, sym):
    return futs.get(FUT_ALIAS.get(sym, sym))


groww_client = None


# ---------------------------------------------------------------- Groww source

def parse_candle_time(t):
    if isinstance(t, (int, float)):
        return int(t / 1000 if t > 1e11 else t)
    return int(dt.datetime.fromisoformat(str(t).replace(" ", "T")).replace(tzinfo=IST).timestamp())


def groww_backfill(g, stocks):
    """Load the latest session's 1-minute candles for each stock."""
    now = dt.datetime.now(IST)
    start = (now - dt.timedelta(days=5)).strftime("%Y-%m-%d 09:15:00")
    end = now.strftime("%Y-%m-%d %H:%M:%S")
    failures = 0
    for sym, exchange, groww_symbol in stocks:
        try:
            r = g.get_historical_candles(exchange=exchange, segment="CASH", groww_symbol=groww_symbol,
                                         start_time=start, end_time=end,
                                         candle_interval=g.CANDLE_INTERVAL_MIN_1)
            pts = [[parse_candle_time(c[0]), c[4]] for c in r.get("candles", []) if c[4] is not None]
            if pts:
                last_day = ist_date(pts[-1][0])
                board.set_history(sym, [p for p in pts if ist_date(p[0]) == last_day])
            failures = 0
        except Exception as e:
            failures += 1
            if failures >= 3:
                board.set_status(board.status["state"], f"History unavailable: {e}")
                return
        time.sleep(0.3)


def run_groww():
    global groww_client
    try:
        from growwapi import GrowwAPI, GrowwFeed
        token = GrowwAPI.get_access_token(api_key=os.environ["GROWW_API_KEY"],
                                          secret=os.environ["GROWW_API_SECRET"])
        g = GrowwAPI(token)
        df = g.get_all_instruments()
        cash, indices, futs = fno_stocks(df), index_rows(df), near_futures(df)
        groww_client = g
        stock_keys, index_keys = {}, {}          # (exchange, token) -> symbol
        for r in indices.itertuples():           # indices first so they backfill first
            board.add(r.trading_symbol, r.name, "indices", r.exchange, fut_for(futs, r.trading_symbol))
            index_keys[(r.exchange, str(r.exchange_token))] = r.trading_symbol
        for r in cash.itertuples():
            board.add(r.trading_symbol, r.name, "stocks", r.exchange, fut_for(futs, r.trading_symbol))
            stock_keys[(r.exchange, str(r.exchange_token))] = r.trading_symbol
        as_list = lambda keys: [{"exchange": e, "segment": "CASH", "exchange_token": t}
                                for e, t in keys]
        feed = GrowwFeed(g)
        feed.subscribe_ltp(as_list(stock_keys))
        feed.subscribe_index_value(as_list(index_keys))
        board.set_status("live")
        rows = pd.concat([indices, cash])
        threading.Thread(target=groww_backfill, daemon=True, args=(
            g, list(zip(rows.trading_symbol, rows.exchange, rows.groww_symbol)))).start()

        def ingest(data, keys, wrapper):
            data = (data or {}).get(wrapper, data or {})
            for exchange, segments in data.items():
                for tok, v in ((segments or {}).get("CASH") or {}).items():
                    sym = keys.get((exchange, str(tok)))
                    if not sym or v is None:
                        continue
                    if isinstance(v, dict):
                        price, ts = v.get("ltp", v.get("value")), v.get("tsInMillis")
                    else:
                        price, ts = v, None
                    if price:
                        board.tick(sym, float(price), ts / 1000 if ts else None)

        while True:
            time.sleep(1)
            ingest(feed.get_ltp(), stock_keys, "ltp")
            ingest(feed.get_index_value(), index_keys, "index_value")
    except Exception as e:
        board.set_status("error", f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------- demo source

def run_demo():
    try:
        df = pd.read_csv(INSTRUMENT_CSV, low_memory=False)
        prices, futs = {}, near_futures(df)
        now = int(time.time()) // 60 * 60
        rows = [(r, "indices") for r in index_rows(df).itertuples()]
        rows += [(r, "stocks") for r in fno_stocks(df).itertuples()]
        for r, group in rows:
            sym = r.trading_symbol
            board.add(sym, r.name, group, r.exchange, fut_for(futs, sym))
            p = random.uniform(9000, 80000) if group == "indices" else random.uniform(80, 4000)
            drift = random.uniform(-0.0002, 0.0002)
            hist = []
            for i in range(240, 0, -1):
                p *= 1 + drift + random.gauss(0, 0.0012)
                hist.append([now - i * 60, round(p, 2)])
            board.set_history(sym, hist)
            prices[sym] = p
        board.set_status("demo", "Simulated prices, not real market data")
        syms = list(prices)
        while True:
            time.sleep(1)
            for sym in random.sample(syms, k=len(syms) // 2):
                prices[sym] *= 1 + random.gauss(0, 0.0006)
                board.tick(sym, round(prices[sym], 2))
    except Exception as e:
        board.set_status("error", f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ web server

clients = set()


async def broadcaster():
    while True:
        await asyncio.sleep(1)
        ticks, hist = board.drain()
        if not clients:
            continue
        msg = {"type": "update", "status": board.status, "ticks": ticks, "hist": hist}
        for ws in list(clients):
            try:
                await ws.send_json(msg)
            except Exception:
                clients.discard(ws)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=run_demo if SOURCE == "demo" else run_groww, daemon=True).start()
    task = asyncio.create_task(broadcaster())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


def slim_quote(q):
    """Keep only the quote fields the detail view shows."""
    last, change = q.get("last_price"), q.get("day_change")
    return {
        "last": last,
        "prevClose": last - change if last is not None and change is not None else None,
        "volume": q.get("volume"),
        "high52": q.get("week_52_high"), "low52": q.get("week_52_low"),
        "oi": q.get("open_interest"), "oiChangePct": q.get("oi_day_change_percentage"),
    }


def demo_detail(sym, info):
    pts = [p[1] for p in info["points"]] or [100.0]
    rng = random.Random(sym)
    stock = info["group"] == "stocks"
    quote = {"last": pts[-1], "prevClose": round(pts[0] * (1 + rng.uniform(-0.012, 0.012)), 2),
             "volume": rng.randrange(200_000, 30_000_000) if stock else None,
             "high52": round(max(pts) * (1 + rng.uniform(0.04, 0.45)), 2),
             "low52": round(min(pts) * (1 - rng.uniform(0.04, 0.35)), 2)}
    fut = None
    if info["fut"]:
        fut = {"last": round(pts[-1] * (1 + rng.uniform(0.001, 0.006)), 2),
               "oi": rng.randrange(500, 90_000) * info["fut"]["lot"],
               "oiChangePct": round(rng.uniform(-9, 9), 2)}
    return quote, fut


detail_cache = {}


@app.get("/api/detail/{sym}")
def detail(sym: str):
    """Extra numbers for one instrument: quote, nearest futures contract, lot size."""
    info = board.symbols.get(sym)
    if not info:
        return JSONResponse({"error": "unknown symbol"}, status_code=404)
    hit = detail_cache.get(sym)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    f = info["fut"]
    out = {"s": sym, "quote": None, "fut": None, "note": "",
           "lot": f and f["lot"], "expiry": f and f["expiry"], "futSymbol": f and f["symbol"]}
    if SOURCE == "demo":
        out["quote"], out["fut"] = demo_detail(sym, info)
    elif groww_client:
        try:
            out["quote"] = slim_quote(groww_client.get_quote(
                trading_symbol=sym, exchange=info["exchange"], segment="CASH", timeout=8))
        except Exception as e:
            out["note"] = str(e)
        if f:
            try:
                out["fut"] = slim_quote(groww_client.get_quote(
                    trading_symbol=f["symbol"], exchange=f["exchange"], segment="FNO", timeout=8))
            except Exception as e:
                out["note"] = out["note"] or str(e)
    detail_cache[sym] = (time.time(), out)
    return out


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": "snapshot", "status": board.status, "symbols": board.snapshot()})
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
