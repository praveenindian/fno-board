"""fno-board: live price charts for every NSE F&O stock on one page.

Run:  .venv/bin/python server.py      then open http://127.0.0.1:8765
Set DATA_SOURCE=demo in .env (or the environment) for simulated prices.
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
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
# Finished days are saved here, one JSON file per instrument: archive/2026-11-20/RELIANCE.json
# Simulated days go to a separate "-demo" folder so they never mix with real ones.
ARCHIVE = ROOT / (os.getenv("ARCHIVE_DIR", "archive") + ("-demo" if SOURCE == "demo" else ""))
MARKET_OPEN = dt.time(9, 15)
ARCHIVE_AT = dt.time(16, 5)             # IST; the last 1-minute candle of a session is 15:59


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
            return [{"s": s, "n": v["name"], "g": v["group"], "f": bool(v["fut"]),
                     "p": [list(p) for p in v["points"]]}
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
                                             "groww_symbol": r.groww_symbol,
                                             "expiry": r.expiry_date, "lot": int(float(r.lot_size))})
    return out


def fut_for(futs, sym):
    return futs.get(FUT_ALIAS.get(sym, sym))


groww_client = None
groww_symbols = {}                       # sym -> (exchange, groww_symbol)


# --------------------------------------------------------------------- archive

archive_lock = threading.Lock()          # one archiving pass at a time
day_cache = {}                           # day -> assembled board for the Archive view


def archive_file(day, sym):
    return ARCHIVE / day / (re.sub(r"[^A-Za-z0-9&_.-]", "_", sym) + ".json")


def session_over(day):
    now = dt.datetime.now(IST)
    return day < now.date().isoformat() or now.time() >= ARCHIVE_AT


def save_day(sym, day, rows, kind, prev_close=None):
    """Write one instrument's finished day. rows are [epoch, open, high, low, close, volume].

    kind is "candles" for exchange candles or "prices" when only the prices this board
    saw are known. A "prices" file is replaced once candles are available for that day."""
    info, path = board.symbols.get(sym), archive_file(day, sym)
    if not info or not rows:
        return
    if path.exists():
        try:
            if kind != "candles" or json.loads(path.read_text()).get("kind") == "candles":
                return
        except ValueError:
            pass                                 # unreadable file: write it again
    volumes = [r[5] for r in rows if r[5] is not None]
    # Open, high and low come from the regular session; pre-open candles (09:00-09:14) only
    # carry indicative prices. They stay in "candles" so the file keeps everything received.
    regular = [r for r in rows if is_regular(r)] or rows
    doc = {
        "symbol": sym, "name": info["name"], "group": info["group"], "exchange": info["exchange"],
        "date": day, "source": SOURCE, "kind": kind,
        "open": regular[0][1], "high": max(r[2] for r in regular), "low": min(r[3] for r in regular),
        "close": rows[-1][4], "prevClose": prev_close,
        "volume": sum(volumes) if volumes else None,
        "fut": None,                             # filled in by archive_futures
        "columns": ["time", "open", "high", "low", "close", "volume"],
        "candles": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    tmp.replace(path)
    day_cache.pop(day, None)


def save_days(sym, days):
    """Archive every finished day in {day: candle rows}."""
    prev = None
    for day in sorted(days):
        if session_over(day):
            save_day(sym, day, days[day], "candles", prev)
        prev = days[day][-1][4]


def save_from_board(sym, day):
    """Fallback when candles are unavailable: keep the prices the board collected."""
    with board.lock:
        pts = [list(p) for p in board.symbols[sym]["points"]]
    save_day(sym, day, [[t, p, p, p, p, None] for t, p in pts if ist_date(t).isoformat() == day],
             "prices")


def is_regular(row):
    return dt.datetime.fromtimestamp(row[0], IST).time() >= MARKET_OPEN


def fut_figures(rows, prev):
    """One day's summary for a futures contract. rows are [epoch, o, h, l, c, volume, oi]."""
    regular = [r for r in rows if is_regular(r)] or rows
    volumes = [r[5] for r in rows if r[5] is not None]
    return {"open": regular[0][1], "high": max(r[2] for r in regular),
            "low": min(r[3] for r in regular), "close": rows[-1][4],
            "prevClose": prev and prev["close"], "volume": sum(volumes) if volumes else None,
            "oi": next((r[6] for r in reversed(rows) if r[6] is not None), None),
            "prevOi": prev and prev["oi"]}


def archive_futures(g):
    """Add the nearest futures contract's price, volume and open interest for the day to
    recently saved files that do not have them yet. Only the day's summary is kept."""
    since = (dt.datetime.now(IST) - dt.timedelta(days=5)).date().isoformat()
    recent = [d for d in archive_days() if d >= since]
    failures = 0
    for sym, info in list(board.symbols.items()):
        f = info["fut"]
        if not f or not f.get("groww_symbol"):
            continue
        todo = {}                                # day -> (path, saved file) still lacking figures
        for day in recent:
            path = archive_file(day, sym)
            try:
                doc = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not doc.get("fut") or "close" not in doc["fut"]:
                todo[day] = (path, doc)
        if not todo:
            continue
        try:
            days = fetch_candles(g, f["exchange"], f["groww_symbol"], "FNO")
            failures = 0
        except Exception:
            failures += 1
            if failures >= 3:
                return
            continue
        finally:
            time.sleep(0.3)
        contract = {"symbol": f["symbol"], "expiry": f["expiry"], "lot": f["lot"]}
        blank = dict.fromkeys(("open", "high", "low", "close", "prevClose", "volume", "oi", "prevOi"))
        prev = None
        for day in sorted(set(days) | set(todo)):
            figures = fut_figures(days[day], prev) if day in days else None
            prev = figures or prev
            if day in todo:                      # no trades that day: saved blank, not retried
                path, doc = todo[day]
                doc["fut"] = {**contract, **(figures or blank)}
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(doc, separators=(",", ":")))
                tmp.replace(path)


def end_of_day(day):
    """Archive today's session for every instrument that has no file yet."""
    with archive_lock:
        failures = 0
        for sym in list(board.symbols):
            if archive_file(day, sym).exists():
                continue
            days = None
            if groww_client and sym in groww_symbols and failures < 3:
                try:
                    days = fetch_candles(groww_client, *groww_symbols[sym])
                    failures = 0
                except Exception:
                    failures += 1
                time.sleep(0.3)
            if days and day in days:
                save_days(sym, days)
            else:
                save_from_board(sym, day)
        if groww_client:
            archive_futures(groww_client)


def archive_loop():
    """Once the market has closed, save the day if the board saw any prices for it."""
    done = None
    while True:
        time.sleep(30)
        now = dt.datetime.now(IST)
        day = now.date().isoformat()
        if day == done or now.time() < ARCHIVE_AT:
            continue
        with board.lock:
            traded = any(v["points"] and ist_date(v["points"][-1][0]).isoformat() == day
                         for v in board.symbols.values())
        if not board.symbols or board.status["state"] == "starting":
            continue                             # instruments still loading
        if traded:
            try:
                end_of_day(day)
            except Exception as e:
                print(f"Archive for {day} failed: {type(e).__name__}: {e}")
        done = day


def archive_days():
    if not ARCHIVE.is_dir():
        return []
    return sorted((d.name for d in ARCHIVE.iterdir()
                   if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)), reverse=True)


def archive_board(day):
    """Every instrument saved for one day, in the same shape as the live snapshot."""
    if day not in day_cache:
        order = {s: i for i, s in enumerate(list(board.symbols))}
        out = []
        for f in (ARCHIVE / day).glob("*.json"):
            try:
                d = json.loads(f.read_text())
                out.append({"s": d["symbol"], "n": d["name"], "g": d["group"],
                            "f": bool((d.get("fut") or {}).get("symbol")),
                            "p": [[c[0], c[4]] for c in d["candles"]]})
            except (ValueError, KeyError, IndexError):
                continue
        out.sort(key=lambda x: (order.get(x["s"], len(order)), x["s"]))
        while len(day_cache) >= 6:
            day_cache.pop(next(iter(day_cache)))
        day_cache[day] = {"day": day, "symbols": out}
    return day_cache[day]


# ---------------------------------------------------------------- Groww source

def parse_candle_time(t):
    if isinstance(t, (int, float)):
        return int(t / 1000 if t > 1e11 else t)
    return int(dt.datetime.fromisoformat(str(t).replace(" ", "T")).replace(tzinfo=IST).timestamp())


def fetch_candles(g, exchange, groww_symbol, segment="CASH"):
    """1-minute candles for the last few sessions: {day: [[epoch, o, h, l, c, volume], ...]}.
    Futures (segment "FNO") rows carry open interest as a seventh value."""
    now = dt.datetime.now(IST)
    r = g.get_historical_candles(exchange=exchange, segment=segment, groww_symbol=groww_symbol,
                                 start_time=(now - dt.timedelta(days=5)).strftime("%Y-%m-%d 09:00:00"),
                                 end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                                 candle_interval=g.CANDLE_INTERVAL_MIN_1)
    days = {}
    for c in r.get("candles", []):
        if c[4] is None:
            continue
        t = parse_candle_time(c[0])
        o, h, lo = (c[i] if c[i] is not None else c[4] for i in (1, 2, 3))
        row = [t, o, h, lo, c[4], c[5] if len(c) > 5 else None]
        if segment == "FNO":
            row.append(c[6] if len(c) > 6 else None)
        days.setdefault(ist_date(t).isoformat(), []).append(row)
    for rows in days.values():
        rows.sort(key=lambda row: row[0])
    return days


def groww_backfill(g):
    """Chart the latest session for each instrument and archive any finished days not yet saved."""
    failures = 0
    with archive_lock:
        for sym, (exchange, groww_symbol) in list(groww_symbols.items()):
            try:
                days = fetch_candles(g, exchange, groww_symbol)
                if days:
                    board.set_history(sym, [[c[0], c[4]] for c in days[max(days)]])
                failures = 0
            except Exception as e:
                failures += 1
                if failures >= 3:
                    board.set_status(board.status["state"], f"History unavailable: {e}")
                    return
                days = None
            if days:
                try:
                    save_days(sym, days)
                except OSError as e:
                    print(f"Archive write failed: {e}")
            time.sleep(0.3)
        try:
            archive_futures(g)
        except OSError as e:
            print(f"Archive write failed: {e}")


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
        groww_symbols.update({s: (e, gs) for s, e, gs in
                              zip(rows.trading_symbol, rows.exchange, rows.groww_symbol)})
        threading.Thread(target=groww_backfill, daemon=True, args=(g,)).start()

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
    threading.Thread(target=archive_loop, daemon=True).start()
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


@app.get("/api/archive")
def archive_index():
    """Days that have been archived, newest first."""
    return {"days": archive_days()}


@app.get("/api/archive/{day}")
def archive_day(day: str):
    if day not in archive_days():
        return JSONResponse({"error": "no archive for that day"}, status_code=404)
    return archive_board(day)


@app.get("/archive/{day}/{name}.json")
def archive_one(day: str, name: str):
    """One instrument's saved day, straight from its file."""
    path = archive_file(day, name)
    if day not in archive_days() or not path.is_file():
        return JSONResponse({"error": "not archived"}, status_code=404)
    return FileResponse(path, media_type="application/json")


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
