#!/usr/bin/env python3
"""
Magic Farm Vitamin 2026 — Phase 2 dashboard sync.

Runs in GitHub Actions every 30 minutes:
  1. Pulls the KOL roster from the Google Sheet ("Working Timeline" = master list,
     "Client chooses" = followers / budget / shipping).
  2. Optionally reads a "KPI" tab (KPI views, media budget) if one exists.
  3. Scrapes every posted TikTok video (yt-dlp cascade) and keeps the last good value
     when a scrape fails, so numbers never drop to 0.
  4. Writes ONLY data files: data/live.json and data/history.json.
     index.html is never touched by CI (design and data stay decoupled).

Privacy: addresses, phone numbers, tracking numbers, Spark Ads codes, captions and
internal comments are never read into the output — only public-safe fields are written.

Local test:  python3 scripts/sync.py --wt fixtures/wt.csv --cc fixtures/cc.csv --no-scrape
"""

import argparse
import csv
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

SHEET_ID = "1X4mdguJ7tF266c1SJOCS_wQPcXbGvnhC8vRVrNuc7MI"
CLIENT_CHOOSES_GID = "941862681"
WORKING_SHEET = "Working Timeline"
KPI_SHEET = "KPI"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_PATH = os.path.join(ROOT, "data", "live.json")
HISTORY_PATH = os.path.join(ROOT, "data", "history.json")
TZ_BKK = timezone(timedelta(hours=7))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Videos that can't be scraped (age-gated / login wall). Used only when every
# scrape method returns 0. Format: 'handle': {'views':..,'likes':..,'comments':..,'shares':..,'saves':..}
MANUAL_OVERRIDES = {
}


# ─────────────────────────── helpers ───────────────────────────
def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=40):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def gviz_csv(sheet, query=None):
    params = {"tqx": "out:csv", "sheet": sheet}
    if query:
        params["tq"] = query
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?" + urllib.parse.urlencode(params)
    return http_get(url)


def export_csv(gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    return http_get(url)


def rows_of(text):
    return list(csv.reader(io.StringIO(text)))


def norm(handle):
    return re.sub(r"\s+", "", str(handle or "")).lstrip("@").lower()


def parse_num(v):
    """'42.2K' -> 42200, '1.4M' -> 1400000, '฿8,000' -> 8000, '67500.0' -> 67500."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("฿", "").replace(" ", "")
    if not s:
        return None
    m = re.match(r"^(-?\d+(?:\.\d+)?)([kKmM]?)$", s)
    if not m:
        return None
    n = float(m.group(1))
    suf = m.group(2).lower()
    if suf == "k":
        n *= 1_000
    elif suf == "m":
        n *= 1_000_000
    return int(round(n))


def handle_from_url(url):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", str(url or ""))
    return m.group(1) if m else None


def clean_url(url):
    u = str(url or "").strip()
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u
    return u.split("?")[0]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ─────────────────────────── sheet parsing ───────────────────────────
def parse_working(text):
    """Working Timeline → ordered list of KOL production rows (master roster)."""
    rows = rows_of(text)
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n:
                    return i
        return None

    i_no, i_cat, i_kol = col("#"), col("category"), col("kol")
    i_tl, i_d1, i_d2, i_fd = col("timeline"), col("1st draft"), col("2nd draft"), col("final draft")
    i_pd, i_link = col("post date"), col("link post")

    def g(r, i):
        return r[i].strip() if i is not None and i < len(r) and r[i] else ""

    out = []
    for r in rows[1:]:
        kol = g(r, i_kol)
        if not kol:
            continue
        timeline = g(r, i_tl)
        d1, d2, fd = g(r, i_d1), g(r, i_d2), g(r, i_fd)
        link = g(r, i_link)
        if not re.search(r"https?://|tiktok\.com", link):
            link = ""          # ignore stray text / spaces in the Link Post cell
        note = ""

        def is_draft(s):
            return bool(re.search(r"draft", s, re.I))

        if link:
            stage = "posted"
        elif fd and is_draft(fd):
            stage = "final"
        elif d2 and is_draft(d2):
            stage = "draft2"
            if fd:
                note = f"Final: {fd}"[:120]
        elif d1 and is_draft(d1):
            stage = "draft1"
            if d2:              # e.g. a due date typed into the 2nd Draft cell
                note = f"Draft 2: {d2}"[:120]
        elif d1:
            stage = "issue"
            note = d1[:120]
        else:
            stage = "waiting"
            if "รอ" in timeline:
                note = timeline.splitlines()[0][:80]

        planned = ""
        m = re.search(r"post\s*date\s*>\s*([^\n]+)", timeline, re.I)
        if m:
            planned = m.group(1).strip().rstrip(".")

        out.append({
            "no": parse_num(g(r, i_no)),
            "handle": kol.lstrip("@"),
            "key": norm(kol),
            "category": g(r, i_cat),
            "stage": stage,
            "note": note,
            "planned_post": planned,
            "post_date_sheet": g(r, i_pd),
            "post_url": clean_url(link) if link else "",
        })
    return out


def parse_client(text):
    """Client chooses → {key: {followers, budget, profile, shipped}} + sheet budget footer.
    The Address column and tracking numbers are deliberately never copied out."""
    rows = rows_of(text)
    if not rows:
        return {}, {}
    head = [h.strip().lower() for h in rows[0]]

    def col(name):
        return head.index(name) if name in head else None

    i_kol, i_fol, i_prof, i_bud, i_cat = col("kol"), col("follower"), col("link profile"), col("total budget"), col("category")
    tracking_re = re.compile(r"^TH[0-9A-Z]{10,}$")
    data, footer = {}, {}

    for r in rows[1:]:
        cells = [c.strip() for c in r]
        kol = cells[i_kol] if i_kol is not None and i_kol < len(cells) else ""
        if kol:
            data[norm(kol)] = {
                "followers": parse_num(cells[i_fol]) if i_fol is not None else None,
                "budget": parse_num(cells[i_bud]) if i_bud is not None else None,
                "profile": clean_url(cells[i_prof]) if i_prof is not None else "",
                "category": cells[i_cat] if i_cat is not None else "",
                "shipped": any(tracking_re.match(c or "") for c in cells),
            }
            continue
        # footer rows: "งบ | 500000 | 73450 | ใช้ไปแล้ว"  /  "คงเหลือ | 426550"
        for j, c in enumerate(cells):
            if c.startswith("งบ"):
                nums = [parse_num(x) for x in cells[j + 1:] if parse_num(x) is not None]
                if nums:
                    footer["total"] = nums[0]
                if len(nums) > 1:
                    footer["used"] = nums[1]
            elif c.startswith("คงเหลือ"):
                nums = [parse_num(x) for x in cells[j + 1:] if parse_num(x) is not None]
                if nums:
                    footer["remaining"] = nums[0]
    return data, footer


def parse_kpi(text):
    """Optional KPI tab. Looks for label cells and takes the first number to their right.
    gviz falls back to the first tab when a sheet name doesn't exist — detect that."""
    rows = rows_of(text)
    if not rows or any(h.strip().lower() == "kol" for h in rows[0]):
        return {}
    kpi = {}
    for r in rows:
        cells = [c.strip() for c in r]
        for j, c in enumerate(cells):
            lc = c.lower()
            nums = [parse_num(x) for x in cells[j + 1:] if parse_num(x) is not None]
            if not nums:
                continue
            if "views" not in kpi and re.search(r"view|วิว", lc):
                kpi["views"] = nums[0]
            elif "media_budget" not in kpi and re.search(r"media|มีเดีย|budget|งบ", lc):
                kpi["media_budget"] = nums[0]
            elif "engagement" not in kpi and re.search(r"engage|เอ็นเกจ", lc):
                kpi["engagement"] = nums[0]
    return kpi


# ─────────────────────────── TikTok scraping ───────────────────────────
def _ytdlp(url, extra=None):
    cmd = ["yt-dlp", "--dump-json", "--no-download", "--no-warnings", "--no-playlist"]
    if extra:
        cmd += extra
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=90)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    info = json.loads(r.stdout.splitlines()[0])
    return {
        "views": int(info.get("view_count") or 0),
        "likes": int(info.get("like_count") or 0),
        "comments": int(info.get("comment_count") or 0),
        "shares": int(info.get("repost_count") or 0),
        "saves": int(info.get("save_count") or info.get("collect_count") or 0),
        "followers": int(info.get("channel_follower_count") or 0),
        "author": info.get("uploader") or info.get("channel") or "",
        "uploaded": info.get("timestamp"),
        "thumbnail": info.get("thumbnail") or "",
        "video_url": info.get("webpage_url") or "",
        "duration": info.get("duration"),
    }


def _web_regex(url):
    html = http_get(url, timeout=30)
    def first(pats):
        for p in pats:
            m = re.search(p, html)
            if m:
                return int(m.group(1))
        return 0
    return {
        "views": first([r'"playCount"\s*:\s*(\d+)']),
        "likes": first([r'"diggCount"\s*:\s*(\d+)']),
        "comments": first([r'"commentCount"\s*:\s*(\d+)']),
        "shares": first([r'"shareCount"\s*:\s*(\d+)']),
        "saves": first([r'"collectCount"\s*:\s*"?(\d+)']),
        "followers": first([r'"followerCount"\s*:\s*(\d+)']),
    }


def scrape_video(url):
    attempts = [
        ("yt-dlp", lambda: _ytdlp(url)),
        ("yt-dlp/age", lambda: _ytdlp(url, ["--age-limit", "99"])),
        ("yt-dlp/mobile", lambda: _ytdlp(url, ["--extractor-args", "tiktok:api_hostname=api16-normal-c-useast1a.tiktokv.com"])),
        ("web", lambda: _web_regex(url)),
    ]
    for name, fn in attempts:
        try:
            d = fn()
            if d and d.get("views", 0) > 0:
                d["_method"] = name
                return d
        except Exception as e:
            log(f"     {name} failed: {str(e)[:120]}")
    return None


def scrape_profile_followers(handle):
    try:
        html = http_get(f"https://www.tiktok.com/@{handle}", timeout=30)
        m = re.search(r'"followerCount"\s*:\s*(\d+)', html)
        return int(m.group(1)) if m else None
    except Exception:
        return None


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wt", help="local CSV for Working Timeline (testing)")
    ap.add_argument("--cc", help="local CSV for Client chooses (testing)")
    ap.add_argument("--no-scrape", action="store_true")
    args = ap.parse_args()

    prev = load_json(LIVE_PATH, {})
    prev_by_key = {k["key"]: k for k in prev.get("kols", [])}

    # 1) Sheet
    try:
        wt_text = open(args.wt, encoding="utf-8").read() if args.wt else gviz_csv(
            WORKING_SHEET, "select A,B,C,D,E,H,K,O,P where C is not null")
        working = parse_working(wt_text)
    except Exception as e:
        log(f"!! Working Timeline fetch failed: {e}")
        working = []
    if not working:
        if prev.get("kols"):
            log("!! No roster from sheet — keeping previous data untouched.")
            return
        sys.exit("No roster available")

    try:
        cc_text = open(args.cc, encoding="utf-8").read() if args.cc else export_csv(CLIENT_CHOOSES_GID)
        client, footer = parse_client(cc_text)
    except Exception as e:
        log(f"!! Client chooses fetch failed: {e}")
        client, footer = {}, prev.get("sheet_budget", {})

    kpi = {}
    if not args.wt:
        try:
            kpi = parse_kpi(gviz_csv(KPI_SHEET))
        except Exception as e:
            log(f"   KPI tab not readable: {e}")
    log(f"Roster: {len(working)} KOLs in '{WORKING_SHEET}', {len(client)} in 'Client chooses', KPI tab: {kpi or '—'}")

    # Fallback post links from config.json ("post_links": {"handle": "url"}) for clips that
    # are live on TikTok but not yet pasted into the sheet. The sheet always wins when filled.
    cfg = load_json(os.path.join(ROOT, "config.json"), {})
    extra_links = {norm(h): clean_url(u) for h, u in (cfg.get("post_links") or {}).items() if u}

    # 2) Merge + scrape
    now = datetime.now(TZ_BKK)
    kols = []
    for w in working:
        if not w["post_url"] and w["key"] in extra_links:
            w["post_url"], w["stage"], w["note"] = extra_links[w["key"]], "posted", ""
            log(f"   {w['handle']}: using post link from config.json (not in sheet yet)")
        c = client.get(w["key"], {})
        p = prev_by_key.get(w["key"], {})
        profile = c.get("profile") or p.get("profile") or f"https://www.tiktok.com/@{w['handle']}"
        tiktok_handle = handle_from_url(profile) or w["handle"]

        k = {
            **w,
            "tiktok_handle": tiktok_handle,
            "profile": profile,
            "category": w["category"] or c.get("category", ""),
            "confirmed": bool(c),
            "followers": c.get("followers") or p.get("followers"),
            "budget": c.get("budget"),
            "shipped": c.get("shipped") if c else None,
            "stats": p.get("stats") if p.get("post_url") == w["post_url"] else None,
        }

        if w["post_url"] and not args.no_scrape:
            log(f"→ {w['handle']}: {w['post_url']}")
            d = scrape_video(w["post_url"])
            if (not d) and w["key"] in MANUAL_OVERRIDES:
                d = {**MANUAL_OVERRIDES[w["key"]], "_method": "manual"}
            if d:
                k["stats"] = {
                    "views": d.get("views", 0), "likes": d.get("likes", 0),
                    "comments": d.get("comments", 0), "shares": d.get("shares", 0),
                    "saves": d.get("saves", 0), "updated": now.isoformat(timespec="minutes"),
                    "method": d.get("_method"),
                }
                for f in ("author", "thumbnail", "video_url", "duration"):
                    if d.get(f):
                        k[f] = d[f]
                if d.get("uploaded"):
                    k["uploaded"] = datetime.fromtimestamp(d["uploaded"], TZ_BKK).isoformat(timespec="minutes")
                if d.get("followers"):
                    k["followers"] = d["followers"]
                log(f"   ✓ {k['stats']['views']:,} views ({d.get('_method')})")
            else:
                log("   ✗ scrape failed — keeping previous stats" if k["stats"] else "   ✗ scrape failed")
            time.sleep(1.5)
        else:
            for f in ("author", "thumbnail", "video_url", "duration", "uploaded"):
                if p.get(f) and p.get("post_url") == w["post_url"]:
                    k[f] = p[f]

        # Followers for KOLs not yet in Client chooses and not yet posted (once found, it is kept)
        if not k["followers"] and not args.no_scrape:
            f = scrape_profile_followers(tiktok_handle)
            if f:
                k["followers"] = f
        kols.append(k)

    # 3) Totals + history
    posted = [k for k in kols if k.get("stats")]
    tot = {m: sum(k["stats"][m] for k in posted) for m in ("views", "likes", "comments", "shares", "saves")}
    live = {
        "campaign": "Magic Farm Vitamin 2026 — Phase 2",
        "updated": now.isoformat(timespec="minutes"),
        "source": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}",
        "kpi_from_sheet": kpi,
        "sheet_budget": footer,
        "totals": {**tot, "kols": len(kols), "posted": len(posted)},
        "kols": kols,
    }
    write_json(LIVE_PATH, live)

    hist = load_json(HISTORY_PATH, [])
    point = {"t": now.isoformat(timespec="minutes"), "views": tot["views"],
             "eng": tot["likes"] + tot["comments"] + tot["shares"] + tot["saves"], "posted": len(posted)}
    if hist and hist[-1]["views"] == point["views"] and hist[-1]["posted"] == point["posted"]:
        hist[-1]["t"] = point["t"]          # nothing changed — just bump the timestamp
    elif not args.no_scrape or not hist:
        hist.append(point)
    write_json(HISTORY_PATH, hist[-1500:])

    log(f"Done: {len(posted)}/{len(kols)} posted · {tot['views']:,} views")


if __name__ == "__main__":
    main()
