import argparse
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

# Force UTF-8 encoding for standard output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

# ── Command-Line Arguments & Config ───────────────────────────────────────────
parser = argparse.ArgumentParser(description="Generate Lib .strm files and clean M3U playlists from M3U URL(s).")
parser.add_argument(
    "-u", "--url",
    default=os.environ.get("M3U_URL", ""),
    help="M3U URL or comma-separated URLs (defaults to M3U_URL env var)"
)
parser.add_argument(
    "-o", "--out",
    default=os.environ.get("STRM_OUT", "media"),
    help="Output directory (defaults to STRM_OUT env var or ./media)"
)
parser.add_argument(
    "-c", "--cache",
    default=os.environ.get("CACHE_DB", ""),
    help="Path to SQLite cache file (default: <OUT>/m3u2strm_cache.db)"
)

args = parser.parse_args()

M3U_URL = args.url.strip()
OUT = Path(args.out)
CACHE_DB = Path(args.cache) if args.cache else OUT / "m3u2strm_cache.db"

if not M3U_URL:
    print("❌ Error: No M3U URL provided. Supply -u/--url or set the M3U_URL environment variable.")
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
LANG_PREFIX = re.compile(r"^[A-Z]{2,3}\s*-\s*")
EP_PATTERN  = re.compile(r"[Ss](\d{1,2})\s*[Ee](\d{1,2})")
TAG_PATTERN = re.compile(r"\s*\[[^\]]*\]\s*")

def safe(name: str) -> str:
    """Strip filesystem-illegal chars and trim length for Windows compatibility."""
    # Replace filesystem illegal characters
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    # Strip trailing spaces, dots, and commas (Windows path requirements)
    s = s.strip(". ,_-")
    # Cap length to 75 characters to prevent Windows MAX_PATH (260 char limit) issues
    return s[:75].strip(". ,_-") or "Unknown"

def extract_title(meta_line: str) -> str:
    """Extract clean title from #EXTINF line, handling extra commas in attributes."""
    if "," not in meta_line:
        return ""
    # Split on the LAST comma to separate metadata from the actual title
    title = meta_line.rsplit(",", 1)[-1].strip()
    
    # If the extracted title contains leftover M3U attributes, strip them out
    if "=" in title:
        title = re.sub(r'[a-zA-Z0-9\-_]+="[^"]*"', '', title)
        title = re.sub(r'[a-zA-Z0-9\-_]+=[^\s,]*', '', title)
        title = title.strip(" ,.-")
    return title

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

# ── Main Processing Logic ──────────────────────────────────────────────────────
nuked = 0
skipped_headers = 0

OUT.mkdir(parents=True, exist_ok=True)
shows_dir  = OUT / "Shows"
movies_dir = OUT / "Movies"

shows_total    = 0
shows_written  = 0
movies_total   = 0
movies_written = 0
total_live     = 0

print(f"🗄️  Loading SQLite cache from {CACHE_DB}...")
conn = init_db(CACHE_DB)
cache_dict = {row[0]: row[1] for row in conn.execute("SELECT path, url FROM strm_cache")}
new_cache = {}
seen_paths = set()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}
urls = [u.strip() for u in M3U_URL.split(",") if u.strip()]

for u in urls:
    print(f"\n📥 Downloading M3U from {u}...")
    try:
        r = requests.get(u, headers=headers, timeout=60)
        r.raise_for_status()
        lines = r.text.splitlines()
    except Exception as e:
        print(f"❌ Download failed for {u}: {e}")
        continue

    print(f"   {len(lines):,} lines — {len(lines)//2:,} entries approx")

    domain = urllib.parse.urlparse(u).netloc.replace("www.", "").split(":")[0]
    provider_name = safe(domain.split(".")[0]) or "custom"

    live_m3u = ["#EXTM3U"]
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line.startswith("#EXTINF:"):
            i += 1
            continue

        meta = line
        url  = lines[i + 1].strip() if i + 1 < len(lines) else ""
        i   += 2

        name = extract_title(meta)

        # Skip section headers
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
            clean = strip_lang(name)
            ep_m  = EP_PATTERN.search(clean)

            if not ep_m:
                continue

            season  = int(ep_m.group(1))
            episode = int(ep_m.group(2))

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
                continue

            write_strm(strm_path, url)
            shows_written += 1

        elif is_movie:
            clean = strip_lang(name)
            clean = TAG_PATTERN.sub("", clean).strip()

            year_m = re.search(r"\s*-\s*(\d{4})\s*$", clean)
            if year_m:
                year  = year_m.group(1)
                title = clean[:year_m.start()].strip(" -")
            else:
                year  = ""
                title = clean.strip()

            title     = safe(title) or "Unknown Movie"
            folder    = f"{title} ({year})" if year else title
            strm_path = movies_dir / folder / f"{folder}.strm"
            path_str  = str(strm_path)

            seen_paths.add(path_str)
            new_cache[path_str] = url
            movies_total += 1

            if cache_dict.get(path_str) == url:
                continue

            write_strm(strm_path, url)
            movies_written += 1

        else:
            live_m3u.append(meta)
            live_m3u.append(url)

    # Write live TV M3U for this provider
    if len(live_m3u) > 1:
        live_path = OUT / f"live_clean_{provider_name}.m3u"
        live_path.write_text("\n".join(live_m3u), encoding="utf-8")
        live_count = (len(live_m3u) - 1) // 2
        total_live += live_count
        print(f"   📡 Wrote {live_count:,} live channels to {live_path.name}")

# ── Cleanup & Save Cache ───────────────────────────────────────────────────────
orphans = set(cache_dict.keys()) - seen_paths
orphans_deleted = 0
for p in orphans:
    try:
        Path(p).unlink(missing_ok=True)
        orphans_deleted += 1
    except Exception:
        pass

conn.execute("DELETE FROM strm_cache")
conn.executemany("INSERT INTO strm_cache (path, url) VALUES (?, ?)", list(new_cache.items()))
conn.commit()
conn.close()

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n✅ Done!")
print(f"   🎬 Movies:  {movies_total:,} total  ({movies_written:,} newly written)")
print(f"   📺 Series:  {shows_total:,} total  ({shows_written:,} newly written)")
print(f"   📡 Live TV: {total_live:,} total channels written")
print(f"   🗑️  Cleaned: {orphans_deleted:,} orphaned strm files removed")
print(f"   🚫 {nuked:,} adult entries nuked")
print(f"   ⏭️  {skipped_headers:,} section headers skipped")
print(f"\n   Add in Lib:")
print(f"   → Library: Movies → {OUT.resolve() / 'Movies'}")
print(f"   → Library: Shows  → {OUT.resolve() / 'Shows'}")
print(f"   → Live TV → Tuners → M3U → {OUT.resolve() / 'live_clean_<provider>.m3u'}")
