#!/usr/bin/env python3
"""
M3U → Jellyfin .strm generator
Built specifically for this M3U format:
  - No group-title attributes (URL-based categorization only)
  - Name format: "LANG - Show Name S09 E11"  (space between S and E)
  - /series/  in URL → TV show episode
  - plain stream ID  → live TV channel

Output:
  /media/Shows/{Show Name}/Season {NN}/{Show Name} S{NN}E{NN}.strm
  /media/live_clean.m3u  (adult-filtered, for Jellyfin Live TV tuner)
"""

import os
import re
import sys
import sqlite3
import requests
from pathlib import Path

M3U_URL  = os.environ.get("M3U_URL", "")
OUT      = Path(os.environ.get("STRM_OUT", "/media"))
CACHE_DB = Path(os.environ.get("CACHE_DB", OUT / "m3u2strm_cache.db"))

if not M3U_URL:
    print("❌ M3U_URL not set — exiting")
    sys.exit(1)

# ── Adult content filter ───────────────────────────────────────────────────────
ADULT_KEYWORDS = {
    "xxx", "adult", "18+", "porn", "erotic", "sex", "nude", "naked",
    "playboy", "penthouse", "hustler", "x-rated", "xrated",
    "hardcore", "softcore", "hentai", "milf", "fetish", "onlyfans",
    "naughty", "seductive", "stripclub", "redlight", "lewd", "nsfw",
}

def is_adult(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in ADULT_KEYWORDS)

# ── Name helpers ───────────────────────────────────────────────────────────────
# Matches: "EN - ", "FR - ", "AR - ", "DE - " etc. at start of name
LANG_PREFIX = re.compile(r"^[A-Z]{2,3}\s*-\s*")

# Matches: S09 E11, S9 E1, S09E11, s9e1 etc.
EP_PATTERN  = re.compile(r"[Ss](\d{1,2})\s*[Ee](\d{1,2})")

# Matches subtitle/dub tags like [SUB], [DUB], [ENG SUB]
TAG_PATTERN = re.compile(r"\s*\[[^\]]*\]\s*")

def safe(name: str) -> str:
    """Strip filesystem-illegal chars."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip(". ")[:120] or "Unknown"

def strip_lang(name: str) -> str:
    """Remove leading language prefix like 'EN - '."""
    return LANG_PREFIX.sub("", name).strip()

def write_strm(path: Path, url: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url, encoding="utf-8")

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS strm_cache (path TEXT PRIMARY KEY, url TEXT)")
    conn.commit()
    return conn

# ── Download M3U ───────────────────────────────────────────────────────────────
print(f"📥 Downloading M3U...")
try:
    r = requests.get(M3U_URL, timeout=60)
    r.raise_for_status()
    lines = r.text.splitlines()
except Exception as e:
    print(f"❌ Download failed: {e}")
    sys.exit(1)
print(f"   {len(lines):,} lines — {len(lines)//2:,} entries approx")

# ── Parse + categorise ─────────────────────────────────────────────────────────
live_m3u  = ["#EXTM3U"]
nuked = 0
skipped_headers = 0
i = 0

OUT.mkdir(parents=True, exist_ok=True)
shows_dir      = OUT / "Shows"
movies_dir     = OUT / "Movies"

shows_total    = 0
shows_written  = 0
movies_total   = 0
movies_written = 0

print(f"🗄️  Loading SQLite cache from {CACHE_DB}...")
conn = init_db(CACHE_DB)
cache_dict = {row[0]: row[1] for row in conn.execute("SELECT path, url FROM strm_cache")}
new_cache = {}
seen_paths = set()

while i < len(lines):
    line = lines[i].strip()

    if not line.startswith("#EXTINF:"):
        i += 1
        continue

    meta = line
    url  = lines[i + 1].strip() if i + 1 < len(lines) else ""
    i   += 2

    # Name is everything after the last comma in #EXTINF
    name = meta.split(",", 1)[-1].strip() if "," in meta else ""

    # Skip section headers like "##### ENGLISH #####"
    if re.match(r"^#+\s*[A-Z ]+\s*#+$", name):
        skipped_headers += 1
        continue

    # Nuke adult content
    if is_adult(name):
        nuked += 1
        continue

    is_series = "/series/" in url.lower()
    is_movie  = "/movie/"  in url.lower()

    if is_series:
        # ── Series → .strm ────────────────────────────────────────────────────
        clean = strip_lang(name)          # "Bonanza S09 E11"
        ep_m  = EP_PATTERN.search(clean)

        if not ep_m:
            # No episode pattern — skip (likely a trailer/extra)
            continue

        season  = int(ep_m.group(1))
        episode = int(ep_m.group(2))

        # Show name = everything before the SxxExx, strip tags like [SUB]
        show_raw = clean[:ep_m.start()].strip(" -_|")
        show_raw = TAG_PATTERN.sub(" ", show_raw).strip()
        show     = safe(show_raw) or "Unknown Show"

        season_folder = f"Season {season:02d}"
        filename      = f"{show} S{season:02d}E{episode:02d}.strm"
        strm_path     = shows_dir / show / season_folder / filename
        path_str      = str(strm_path)

        seen_paths.add(path_str)
        new_cache[path_str] = url
        shows_total += 1

        if cache_dict.get(path_str) == url:
            continue  # Cached!

        write_strm(strm_path, url)
        shows_written += 1

    elif is_movie:
        # ── Movie → .strm ─────────────────────────────────────────────────────
        # Format: "LANG - Movie Title - YEAR [Tag]"
        clean = strip_lang(name)                           # "Movie Title - 2025 [VOSTFR]"
        clean = TAG_PATTERN.sub("", clean).strip()         # "Movie Title - 2025"

        # Extract year from LAST " - YEAR" at end
        year_m = re.search(r"\s*-\s*(\d{4})\s*$", clean)
        if year_m:
            year  = year_m.group(1)
            title = clean[:year_m.start()].strip(" -")
        else:
            year  = ""
            title = clean.strip()

        title  = safe(title) or "Unknown Movie"
        folder = f"{title} ({year})" if year else title
        strm_path = movies_dir / folder / f"{folder}.strm"
        path_str  = str(strm_path)

        seen_paths.add(path_str)
        new_cache[path_str] = url
        movies_total += 1

        if cache_dict.get(path_str) == url:
            continue  # Cached!

        write_strm(strm_path, url)
        movies_written += 1

    else:
        # ── Live TV → filtered M3U ─────────────────────────────────────────────
        live_m3u.append(meta)
        live_m3u.append(url)

# ── Write live TV M3U ──────────────────────────────────────────────────────────
live_path = OUT / "live_clean.m3u"
live_path.write_text("\n".join(live_m3u), encoding="utf-8")
live_count = (len(live_m3u) - 1) // 2

# ── Cleanup & Save Cache ───────────────────────────────────────────────────────
orphans = set(cache_dict.keys()) - seen_paths
orphans_deleted = 0
for p in orphans:
    try:
        Path(p).unlink(missing_ok=True)
        orphans_deleted += 1
    except:
        pass

conn.execute("DELETE FROM strm_cache")
conn.executemany("INSERT INTO strm_cache (path, url) VALUES (?, ?)", list(new_cache.items()))
conn.commit()
conn.close()

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n✅ Done!")
print(f"   🎬 Movies:  {movies_total:,} total  ({movies_written:,} newly written)")
print(f"   📺 Series:  {shows_total:,} total  ({shows_written:,} newly written)")
print(f"   🗑️  Cleaned: {orphans_deleted:,} orphaned strm files removed")
print(f"   📡 Live TV: {live_count:,} channels  → {live_path}")
print(f"   🚫 {nuked:,} adult entries nuked")
print(f"   ⏭️  {skipped_headers:,} section headers skipped")
print(f"\n   Add in Jellyfin:")
print(f"   → Library: Movies → /media/Movies")
print(f"   → Library: Shows  → /media/Shows")
print(f"   → Live TV → Tuners → M3U → /media/live_clean.m3u")
