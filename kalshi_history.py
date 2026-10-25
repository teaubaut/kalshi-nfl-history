#!/usr/bin/env python3
"""
Daily Kalshi NFL snapshot -> per-week price history.

Pulls every open NFL market on Kalshi (moneylines, spreads, totals, player
props, plus anything else in a KXNFL* series), and files each market under
the NFL week its game is played in:

  data/2026/week-08/
      snapshots/2026-10-25.json   full snapshot for that day
      history.csv                 one row per market per day (append-only)
      movement.json               per-market price path + top movers
  data/2026/futures/       season-long markets (MVP, Super Bowl, ...)
  data/2026/index.json     list of weeks and when each was updated

Re-running on the same day replaces that day's rows instead of duplicating.
Kalshi's market-data endpoints are public, so no API key is needed.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

API = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(os.environ.get("KALSHI_DATA_DIR", "data"))

# Tuesday before Week 1 kickoff; NFL weeks are bucketed Tue-Mon.
WEEK1_TUESDAY = date.fromisoformat(os.environ.get("NFL_WEEK1_TUESDAY", "2026-09-08"))
SEASON = WEEK1_TUESDAY.year

# Always scraped, even if series discovery misses them. Any other series whose
# ticker starts with KXNFL is picked up automatically.
FALLBACK_SERIES = [
    "KXNFLGAME",      # moneyline
    "KXNFLSPREAD",    # point spread
    "KXNFLTOTAL",     # game total
    "KXNFLTD",        # anytime TD scorer
    "KXNFLPASSYDS",   # passing yards
    "KXNFLRSHYDS",    # rushing yards
    "KXNFLRECYDS",    # receiving yards
    "KXNFLPASSTDS",   # passing TDs
]

FIELDS = [
    "date", "captured_at", "series", "event_ticker", "event_title", "game_date",
    "market_ticker", "title", "subtitle", "status", "yes_bid", "yes_ask",
    "last_price", "mid", "volume", "volume_24h", "open_interest", "close_time",
]

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
DATE_IN_TICKER = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")

session = requests.Session()
session.headers["User-Agent"] = "nfl-pool-kalshi-history/1.0"


# ---------------------------------------------------------------- API helpers

def get(path, params=None, tries=5):
    for attempt in range(tries):
        try:
            r = session.get(f"{API}{path}", params=params, timeout=30)
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        time.sleep(0.12)  # stay well under the public rate limit
        return r.json()
    r.raise_for_status()
    return {}


def paged(path, key, params):
    params = dict(params)
    while True:
        data = get(path, params)
        yield from data.get(key) or []
        cursor = data.get("cursor")
        if not cursor:
            return
        params["cursor"] = cursor


def discover_series():
    found = set(FALLBACK_SERIES)
    try:
        for s in get("/series", {"category": "Sports"}).get("series") or []:
            ticker = (s.get("ticker") or "").upper()
            if ticker.startswith("KXNFL"):
                found.add(ticker)
    except requests.RequestException as e:
        print(f"series discovery failed ({e}); using fallback list", file=sys.stderr)
    return sorted(found)


# ------------------------------------------------------------- value parsing

def price(m, name):
    """Dollars (0-1). Handles both *_dollars strings and legacy cent ints."""
    v = m.get(f"{name}_dollars")
    if v not in (None, ""):
        return round(float(v), 4)
    v = m.get(name)
    return None if v is None else round(v / 100, 4)


def number(m, name):
    for key in (f"{name}_fp", name):
        v = m.get(key)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def game_date(event_ticker):
    match = DATE_IN_TICKER.search(event_ticker or "")
    if not match or match.group(2) not in MONTHS:
        return None
    yy, mon, dd = match.groups()
    try:
        return date(2000 + int(yy), MONTHS[mon], int(dd))
    except ValueError:
        return None


def bucket_for(gd):
    if gd is None:
        return "futures"
    if gd < WEEK1_TUESDAY:
        return "preseason"
    return f"week-{(gd - WEEK1_TUESDAY).days // 7 + 1:02d}"


# ------------------------------------------------------------------ scraping

def scrape(today, captured_at):
    rows = []
    for series in discover_series():
        n = 0
        params = {"series_ticker": series, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        try:
            for ev in paged("/events", "events", params):
                gd = game_date(ev.get("event_ticker"))
                for m in ev.get("markets") or []:
                    bid, ask = price(m, "yes_bid"), price(m, "yes_ask")
                    last = price(m, "last_price")
                    mid = round((bid + ask) / 2, 4) if bid and ask else last
                    rows.append({
                        "date": today.isoformat(),
                        "captured_at": captured_at,
                        "series": series,
                        "event_ticker": ev.get("event_ticker"),
                        "event_title": ev.get("title"),
                        "game_date": gd.isoformat() if gd else "",
                        "market_ticker": m.get("ticker"),
                        "title": m.get("title"),
                        "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
                        "status": m.get("status"),
                        "yes_bid": bid, "yes_ask": ask, "last_price": last, "mid": mid,
                        "volume": number(m, "volume"),
                        "volume_24h": number(m, "volume_24h"),
                        "open_interest": number(m, "open_interest"),
                        "close_time": m.get("close_time"),
                        "_bucket": bucket_for(gd),
                    })
                    n += 1
        except requests.RequestException as e:
            print(f"{series}: failed ({e})", file=sys.stderr)
        print(f"{series}: {n} markets")
    return rows


# ------------------------------------------------------------------- storage

def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_bucket(folder, today, rows):
    (folder / "snapshots").mkdir(parents=True, exist_ok=True)
    clean = [{k: r[k] for k in FIELDS} for r in rows]

    (folder / "snapshots" / f"{today}.json").write_text(
        json.dumps(clean, indent=1, ensure_ascii=False))

    # history.csv: drop any earlier run from today, then add this run
    hist_path = folder / "history.csv"
    history = []
    if hist_path.exists():
        with hist_path.open(newline="", encoding="utf-8") as f:
            history = [r for r in csv.DictReader(f) if r["date"] != today]
    history.extend(clean)
    history.sort(key=lambda r: (r["market_ticker"] or "", r["date"]))
    with hist_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(history)

    # movement.json: price path per market, first vs latest, high/low
    markets = {}
    for r in history:
        t = r["market_ticker"]
        mk = markets.setdefault(t, {
            "series": r["series"], "event": r["event_title"], "game_date": r["game_date"],
            "title": r["title"], "subtitle": r["subtitle"], "path": []})
        mk["path"].append({"date": r["date"], "mid": to_float(r["mid"]),
                           "bid": to_float(r["yes_bid"]), "ask": to_float(r["yes_ask"]),
                           "volume": to_float(r["volume"])})
    for mk in markets.values():
        mids = [p["mid"] for p in mk["path"] if p["mid"] is not None]
        mk["first"] = mids[0] if mids else None
        mk["latest"] = mids[-1] if mids else None
        mk["high"] = max(mids) if mids else None
        mk["low"] = min(mids) if mids else None
        mk["change"] = round(mids[-1] - mids[0], 4) if len(mids) > 1 else 0.0

    movers = sorted(
        ({"ticker": t, "title": m["title"], "subtitle": m["subtitle"],
          "first": m["first"], "latest": m["latest"], "change": m["change"]}
         for t, m in markets.items() if m["change"]),
        key=lambda x: abs(x["change"]), reverse=True)[:50]

    (folder / "movement.json").write_text(json.dumps({
        "updated": today, "days": sorted({r["date"] for r in history}),
        "top_movers": movers, "markets": markets}, indent=1, ensure_ascii=False))


def main():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    rows = scrape(now.date(), now.isoformat(timespec="seconds"))
    if not rows:
        print("No open NFL markets found; nothing written.")
        return

    season_dir = DATA_DIR / str(SEASON)
    buckets = {}
    for r in rows:
        buckets.setdefault(r["_bucket"], []).append(r)
    for name, bucket_rows in sorted(buckets.items()):
        write_bucket(season_dir / name, today, bucket_rows)
        print(f"{name}: {len(bucket_rows)} markets saved")

    index_path = season_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    for name, bucket_rows in buckets.items():
        index[name] = {"last_updated": today, "markets": len(bucket_rows)}
    index_path.write_text(json.dumps(dict(sorted(index.items())), indent=1))


if __name__ == "__main__":
    main()
