# Kalshi NFL price history

Daily snapshot of every open Kalshi NFL market (moneylines, spreads, totals,
player props, futures), filed by NFL week.

```
data/2026/
  index.json                  weeks + last update
  week-08/
    snapshots/2026-10-25.json full snapshot for that day
    history.csv               one row per market per day
    movement.json             price path per market + top movers
  futures/                    season-long markets (MVP, Super Bowl, ...)
```

Runs daily at 15:00 UTC via `.github/workflows/daily.yml`; run it by hand from
the Actions tab. Week numbers count from `NFL_WEEK1_TUESDAY` (Tue–Mon weeks).

Run locally: `pip install -r requirements.txt && python kalshi_history.py`
