# Magic Farm Vitamin 2026 — Phase 2 · KOL Dashboard

Live: https://araveevong-pixel.github.io/magicfarm-vitamin-phase2/

## How it updates
- Every 30 minutes GitHub Actions runs `scripts/sync.py`:
  1. Reads the KOL roster from the Google Sheet tab **Working Timeline** (status, post link)
     and **Client chooses** (followers, budget, shipping).
  2. Scrapes every posted TikTok clip with yt-dlp.
  3. Writes `data/live.json` and `data/history.json` only. `index.html` is never touched by the bot.
- Add a KOL or paste a post link in the sheet → it appears on the dashboard on the next run.

## Set KPI / Media budget
Edit `config.json` (`kpi_views`, `media_budget`, `kpi_engagement`), or add a tab named **KPI**
in the sheet with label/value rows (e.g. `KPI Views | 15000000`, `Media Budget | 370000`).
`config.json` wins if both are set.

## Video that won't scrape (age-restricted)
Add it to `MANUAL_OVERRIDES` in `scripts/sync.py`.

Privacy: addresses, phone numbers, tracking numbers, Spark Ads codes and internal comments
from the sheet are never written to this repo.
