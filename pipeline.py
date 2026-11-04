#!/usr/bin/env python3
"""
Noir Vault v2 – Dynamic Search-Driven Scene Extraction Pipeline
================================================================
Reads workflow inputs from environment variables:
  SEARCH_KEYWORD      – free-text keyword filter
  SELECTED_GENRE      – genre label from dropdown
  DATE_RANGE          – era label with embedded years
  PUBLIC_DOMAIN_ONLY  – "true" or "false"
  NUM_MOVIES          – how many films to process (default 3)
  ARCHIVE_ID_OVERRIDE – if set, use this single Archive.org identifier

Dynamically discovers films via the Internet Archive Search API,
then runs: scene detection → 1080p transcode → AI analysis → Supabase
→ catalog.json → GitHub Release upload-ready ZIPs.
"""

import os, sys, gc, re, json, random, time, shutil, tempfile, hashlib, subprocess
import urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime

# ── optional heavy deps (fail gracefully) ──────────────────────────────────
try:
    import requests
except ImportError:
    requests = None

try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector
    SCENEDETECT_OK = True
except ImportError:
    SCENEDETECT_OK = False
    print("⚠  PySceneDetect not available – scene detection disabled")

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

try:
    from supabase import create_client
    SUPABASE_OK = True
except ImportError:
    SUPABASE_OK = False
    print("⚠  supabase-py not available – DB upload disabled")

try:
    from faster_whisper import WhisperModel
    WHISPER_OK = True
except ImportError:
    WHISPER_OK = False

try:
    import google.generativeai as genai
    GEMINI_OK = True
except ImportError:
    try:
        from google import genai as genai_new
        GEMINI_OK = True
    except ImportError:
        GEMINI_OK = False

# ── env / config ───────────────────────────────────────────────────────────
SEARCH_KEYWORD     = os.environ.get("SEARCH_KEYWORD", "").strip()
SELECTED_GENRE     = os.environ.get("SELECTED_GENRE", "All Genres").strip()
DATE_RANGE         = os.environ.get("DATE_RANGE", "All Eras").strip()
PUBLIC_DOMAIN_ONLY = os.environ.get("PUBLIC_DOMAIN_ONLY", "true").lower().strip()
NUM_MOVIES         = int(os.environ.get("NUM_MOVIES", "3"))
ARCHIVE_ID_OVERRIDE = os.environ.get("ARCHIVE_ID_OVERRIDE", "").strip()
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPOSITORY", "")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GITHUB_ENV_FILE    = os.environ.get("GITHUB_ENV", "")

WORKSPACE      = Path("temp_workspace")
OUTPUT_DIR     = Path("output")
CATALOG_PATH   = Path("catalog.json")
SCENES_PER_MOVIE = 30
MIN_SCENE_SECS   = 30
MAX_SCENE_SECS   = 180

# Genre → search term mapping
GENRE_SEARCH_MAP = {
    "Film Noir":           "film noir",
    "Crime & Gangster":    "crime gangster",
    "Mystery & Suspense":  "mystery suspense",
    "Psychological Thriller": "psychological thriller",
    "Western Noir":        "western noir",
    "Gothic Horror":       "gothic horror",
    "Sci-Fi B-Movie":      "science fiction",
    "War & Espionage":     "war espionage spy",
    "All Genres":          "",
}

# ── Internet Archive fallback catalog (used if dynamic search fails) ────────
FALLBACK_CATALOG = [
    {"title": "Detour",                  "id": "Detour"},
    {"title": "Suddenly",                "id": "suddenly"},
    {"title": "The Stranger",            "id": "TheStranger_0"},
    {"title": "Quicksand",               "id": "Quicksand_clear"},
    {"title": "The Strange Love of Martha Ivers", "id": "Martha_Ivers_movie"},
    {"title": "Please Murder Me",        "id": "Please_Murder_Me_movie"},
    {"title": "Behind Green Lights",     "id": "BehindGreenLights_high_Q_mp4"},
    {"title": "He Walked by Night",      "id": "He_Walked_By_Night.avi"},
    {"title": "The Red House",           "id": "TheRedHouse"},
    {"title": "Kansas City Confidential","id": "kansascityconfidencial"},
    {"title": "Woman on the Run",        "id": "Woman_on_the_Run"},
    {"title": "The Chase",               "id": "TheChase_"},
    {"title": "The Man Who Cheated Himself","id": "Man_Who_Cheated_Himself"},
    {"title": "Inner Sanctum",           "id": "Inner_Sanctum_movie"},
    {"title": "Whistle Stop",            "id": "Whistle_Stop"},
    {"title": "The Second Woman",        "id": "The_Second_Woman_"},
    {"title": "Jigsaw",                  "id": "Jigsaw_"},
]


# ══════════════════════════════════════════════════════════════════════════════
#  ARCHIVE.ORG HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get(url, timeout=20, params=None):
    """Simple HTTP GET, returns parsed JSON or None."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NoirVaultV2/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    ⚠  GET {url[:80]}… failed: {e}")
        return None


def parse_date_range(date_range_label: str) -> tuple[int | None, int | None]:
    """
    Convert human label to (start_year, end_year) tuple.
    Examples:
      "1940-1959 (Classic Noir Era)"  → (1940, 1959)
      "1960-1979 (Neo-Noir)"          → (1960, 1979)
      "Pre-1930 (Silent Era)"         → (None, 1929)
      "1930-1939 (Pre-Code Era)"      → (1930, 1939)
      "1980-Present (Modern Noir)"    → (1980, None)
      "All Eras"                      → (None, None)
    """
    label = date_range_label.strip()
    if not label or label.lower() == "all eras":
        return None, None
    m = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.search(r"Pre-(\d{4})", label, re.IGNORECASE)
    if m2:
        return None, int(m2.group(1)) - 1
    m3 = re.search(r"(\d{4})\s*-\s*Present", label, re.IGNORECASE)
    if m3:
        return int(m3.group(1)), None
    m4 = re.search(r"(\d{4})", label)
    if m4:
        y = int(m4.group(1))
        return y, y
    return None, None


def search_archive(keyword="", genre="All Genres", date_range="All Eras",
                   pd_only="true", num_needed=10) -> list[dict]:
    """
    Query the Internet Archive Advanced Search API and return a list of
    candidate movie dicts: {title, id, year, description}.
    Returns up to num_needed * 4 candidates so we can filter for quality.
    """
    terms = ['mediatype:(movies)', 'collection:(feature_films)']

    genre_term = GENRE_SEARCH_MAP.get(genre, "")
    if genre_term:
        words = genre_term.split()
        genre_parts = " OR ".join(f'subject:("{w}")' for w in words)
        terms.append(f'({genre_parts})')

    if keyword:
        safe_kw = keyword.replace('"', '')
        terms.append(f'(title:("{safe_kw}") OR subject:("{safe_kw}") OR description:("{safe_kw}"))')

    start_year, end_year = parse_date_range(date_range)
    if start_year and end_year:
        terms.append(f"year:[{start_year} TO {end_year}]")
    elif start_year:
        terms.append(f"year:[{start_year} TO 2000]")
    elif end_year:
        terms.append(f"year:[1895 TO {end_year}]")

    query = " AND ".join(terms)
    print(f"  🔍 Archive search query: {query}")

    rows = min(num_needed * 4, 50)
    params = {
        "q": query,
        "fl[]": "identifier,title,year,description,subject",
        "rows": rows,
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    url = "https://archive.org/advancedsearch.php"
    data = _get(url, params=params)
    if not data:
        return []

    docs = data.get("response", {}).get("docs", [])
    results = []
    for doc in docs:
        results.append({
            "title": doc.get("title", "Unknown"),
            "id": doc.get("identifier", ""),
            "year": doc.get("year"),
            "description": str(doc.get("description", ""))[:300],
        })
    print(f"  📋 Found {len(results)} candidates from Archive search")
    return results


def resolve_archive_mp4(archive_id: str) -> str | None:
    """
    Use the Archive.org metadata API to find the best (largest) MP4 file
    for a given identifier. Returns the full download URL or None.
    """
    meta = _get(f"https://archive.org/metadata/{archive_id}", timeout=15)
    if not meta:
        return None
    files = meta.get("files", [])
    # Prefer .mp4; filter out _512kb, thumbs, sample
    mp4s = [
        f for f in files
        if f.get("name", "").lower().endswith(".mp4")
        and "_thumb" not in f.get("name", "").lower()
        and "sample" not in f.get("name", "").lower()
    ]
    if not mp4s:
        # Try .avi or .mkv as fallback
        mp4s = [f for f in files if re.search(r'\.(avi|mkv|mov|ogv)$', f.get("name","").lower())]
    if not mp4s:
        return None
    # Pick the largest by size
    def _sz(f):
        try:
            return int(f.get("size", 0))
        except (ValueError, TypeError):
            return 0
    best = max(mp4s, key=_sz)
    fname = best["name"]
    return f"https://archive.org/download/{archive_id}/{fname}"


def discover_movies(num_needed: int) -> list[dict]:
    """
    Main discovery function. Returns a list of movie dicts ready for
    processing: {title, id, url, year}.
    """
    # Override: single specific Archive ID
    if ARCHIVE_ID_OVERRIDE:
        print(f"🎯 Using archive ID override: {ARCHIVE_ID_OVERRIDE}")
        url = resolve_archive_mp4(ARCHIVE_ID_OVERRIDE)
        if url:
            return [{"title": ARCHIVE_ID_OVERRIDE, "id": ARCHIVE_ID_OVERRIDE, "url": url, "year": None}]
        else:
            print("  ❌ Could not resolve override ID – falling back to search")

    # Dynamic search
    candidates = search_archive(
        keyword=SEARCH_KEYWORD,
        genre=SELECTED_GENRE,
        date_range=DATE_RANGE,
        pd_only=PUBLIC_DOMAIN_ONLY,
        num_needed=num_needed,
    )
    random.shuffle(candidates)

    verified = []
    checked = 0
    for c in candidates:
        if len(verified) >= num_needed * 2:  # over-provision
            break
        checked += 1
        print(f"  🔗 Verifying [{checked}/{len(candidates)}]: {c['title'][:50]} ({c['id']})")
        url = resolve_archive_mp4(c["id"])
        if url:
            verified.append({"title": c["title"], "id": c["id"], "url": url, "year": c.get("year")})
            print(f"    ✅ {url[:80]}…")
        else:
            print(f"    ⛔ No playable video found")

    if len(verified) < num_needed:
        print(f"  ⚠  Only {len(verified)} verified from search. Topping up from fallback catalog…")
        used_ids = {m["id"] for m in verified}
        random.shuffle(FALLBACK_CATALOG)
        for fb in FALLBACK_CATALOG:
            if len(verified) >= num_needed * 2:
                break
            if fb["id"] in used_ids:
                continue
            url = resolve_archive_mp4(fb["id"])
            if url:
                verified.append({"title": fb["title"], "id": fb["id"], "url": url, "year": None})
                used_ids.add(fb["id"])

    random.shuffle(verified)
    selected = verified[:num_needed]
    print(f"\n🎬 Selected {len(selected)} films for processing:")
    for m in selected:
        print(f"   • {m['title']} ({m.get('year','?')}) – {m['url'][:70]}…")
    return selected


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def download_movie(movie: dict, dest_dir: Path) -> Path | None:
    """Download a movie file, return local path or None on failure."""
    url = movie["url"]
    ext = Path(urllib.parse.urlparse(url).path).suffix or ".mp4"
    safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', movie["id"])[:60]
    dest = dest_dir / f"{safe_id}{ext}"

    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  ✅ Already downloaded: {dest.name}")
        return dest

    print(f"  ⬇  Downloading {movie['title'][:50]} …")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NoirVaultV2/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 1 << 20  # 1 MB
            while True:
                block = r.read(chunk)
                if not block:
                    break
                f.write(block)
                downloaded += len(block)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r    {pct:.1f}% ({downloaded//1048576} MB / {total//1048576} MB)", end="", flush=True)
        print()
        size_mb = dest.stat().st_size / 1048576
        print(f"  ✅ Downloaded {size_mb:.1f} MB → {dest.name}")
        return dest
    except Exception as e:
        print(f"  ❌ Download failed: {e}")
        if dest.exists():
            dest.unlink()
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  SCENE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_and_pick_scenes(video_path: Path, n: int = SCENES_PER_MOVIE) -> list[tuple[float, float]]:
    """
    Run AdaptiveDetector, pick n random scenes within valid duration bounds.
    Returns list of (start_sec, end_sec) tuples.
    """
    if not SCENEDETECT_OK:
        # Fallback: evenly spaced scenes
        total = 3600
        step = total // (n + 1)
        return [(step * i, step * i + 30) for i in range(1, n + 1)]

    print(f"  🎞  Detecting scenes in {video_path.name} …")
    video = None
    try:
        video = open_video(str(video_path))
        manager = SceneManager()
        manager.add_detector(AdaptiveDetector())
        manager.detect_scenes(video, show_progress=False)
        scene_list = manager.get_scene_list()
        print(f"    Found {len(scene_list)} scenes")
    except Exception as e:
        print(f"  ⚠  Scene detection error: {e}")
        scene_list = []
    finally:
        if video is not None:
            del video
            gc.collect()

    candidates = []
    for start_tc, end_tc in scene_list:
        s = start_tc.seconds
        e = end_tc.seconds
        dur = e - s
        if MIN_SCENE_SECS <= dur <= MAX_SCENE_SECS:
            candidates.append((s, e))

    if len(candidates) < n:
        # Fallback: random segments
        print(f"    ⚠  Only {len(candidates)} valid scenes found, padding with random segments")
        random_start = random.randint(300, 2400)
        for i in range(n - len(candidates)):
            start = random_start + i * 120
            candidates.append((start, start + random.randint(20, 55)))

    random.shuffle(candidates)
    chosen = candidates[:n]
    return chosen


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSCODE (1080p H.264 / AAC)
# ══════════════════════════════════════════════════════════════════════════════

def transcode_scene(src: Path, start: float, end: float, dest: Path) -> bool:
    """Extract and transcode a scene clip to 1080p MP4."""
    dur = end - start
    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", str(start),
        "-i", str(src),
        "-t", str(dur),
        #"-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-vf", "scale='if(lt(ih,1080),-2,iw)':'if(lt(ih,1080),1080,ih)':force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            print(f"  ⚠  FFmpeg error: {result.stderr[-300:].decode(errors='replace')}")
            return False
        return True
    except Exception as e:
        print(f"  ⚠  Transcode failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  AI ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def transcribe_audio(clip_path: Path) -> str:
    """Transcribe audio using faster-whisper, or return heuristic placeholder."""
    if WHISPER_OK:
        try:
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(clip_path), beam_size=1)
            text = " ".join(s.text for s in segments).strip()
            del model
            gc.collect()
            return text or "[No dialogue detected]"
        except Exception as e:
            print(f"    ⚠  Whisper error: {e}")
    return "[Transcript unavailable – whisper not installed]"


NOIR_PROMPT_TEMPLATE = """You are a film noir scholar analysing a scene clip.
Movie: "{title}" ({year})
Scene: {scene_num} of {total_scenes}
Transcript excerpt: "{transcript}"

Respond ONLY with valid JSON (no markdown fences), exactly this structure:
{{
  "description": "Vivid 2-sentence visual description of what is seen",
  "synopsis": "1-sentence plot summary of this scene",
  "mood": "Comma-separated mood adjectives (e.g. Tense, Fatalistic, Menacing)",
  "noir_tropes": ["trope1", "trope2", "trope3"]
}}"""

def analyse_scene_ai(title: str, year, scene_num: int, total: int, transcript: str) -> dict:
    """Use Gemini to produce rich metadata for a scene."""
    default = {
        "description": f"Scene {scene_num} from {title} ({year})",
        "synopsis": "A pivotal moment in the film.",
        "mood": "Moody, Tense",
        "noir_tropes": ["shadow play", "moral ambiguity"],
    }
    if not GEMINI_OK or not GEMINI_API_KEY:
        return default
    prompt = NOIR_PROMPT_TEMPLATE.format(
        title=title, year=year or "unknown",
        scene_num=scene_num, total_scenes=total,
        transcript=transcript[:500],
    )
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model_obj = genai.GenerativeModel("gemini-1.5-flash")
        resp = model_obj.generate_content(prompt)
        raw = resp.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        return {
            "description": str(parsed.get("description", default["description"])),
            "synopsis":    str(parsed.get("synopsis",    default["synopsis"])),
            "mood":        str(parsed.get("mood",        default["mood"])),
            "noir_tropes": list(parsed.get("noir_tropes", default["noir_tropes"])),
        }
    except Exception as e:
        print(f"    ⚠  Gemini error: {e}")
        return default


# ══════════════════════════════════════════════════════════════════════════════
#  SUPABASE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_supabase(movie_meta: dict, scenes_meta: list[dict]):
    """Insert movie + scenes rows into Supabase."""
    if not SUPABASE_OK or not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠  Supabase not configured – skipping DB upload")
        return
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        # Upsert movie
        movie_row = {
            "title":           movie_meta["title"],
            "year":            movie_meta.get("year"),
            "archive_id":      movie_meta.get("id"),
            "genre":           SELECTED_GENRE,
            "search_terms":    SEARCH_KEYWORD,
            "is_public_domain": PUBLIC_DOMAIN_ONLY == "true",
        }
        res = client.table("movies").insert(movie_row).execute()
        movie_id = res.data[0]["id"] if res.data else None
        print(f"  📦 Supabase movie inserted: {movie_id}")

        for s in scenes_meta:
            scene_row = {
                "movie_id":      movie_id,
                "scene_number":  s["scene_number"],
                "start_time":    s["start_time"],
                "end_time":      s["end_time"],
                "duration":      s["duration"],
                "transcript":    s.get("transcript"),
                "description":   s.get("description"),
                "synopsis":      s.get("synopsis"),
                "mood":          s.get("mood"),
                "noir_tropes":   s.get("noir_tropes", []),
                "thumbnail_url": s.get("thumbnail_url"),
                "download_url":  s.get("download_url"),
                "release_tag":   s.get("release_tag"),
            }
            client.table("scenes").insert(scene_row).execute()
        print(f"  📦 Supabase: {len(scenes_meta)} scenes inserted")
    except Exception as e:
        print(f"  ⚠  Supabase upload failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  CATALOG.JSON UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def update_catalog(all_movie_entries: list[dict]):
    """Write / merge catalog.json with new results."""
    existing = {"movies": []}
    if CATALOG_PATH.exists():
        try:
            existing = json.loads(CATALOG_PATH.read_text())
        except Exception:
            pass

    existing_ids = {m["id"] for m in existing.get("movies", [])}
    for entry in all_movie_entries:
        if entry["id"] not in existing_ids:
            existing.setdefault("movies", []).insert(0, entry)

    existing["generated_at"] = datetime.utcnow().isoformat() + "Z"
    existing["pipeline_version"] = "2"
    CATALOG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"  ✅ catalog.json updated ({len(existing['movies'])} total movies)")


# ══════════════════════════════════════════════════════════════════════════════
#  EXPORT GITHUB_ENV VARIABLES
# ══════════════════════════════════════════════════════════════════════════════

def export_env(key: str, value: str):
    """Write a key=value pair to GITHUB_ENV for use in subsequent steps."""
    if GITHUB_ENV_FILE:
        with open(GITHUB_ENV_FILE, "a") as f:
            f.write(f"{key}={value}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_movie(movie: dict, workspace: Path, output_dir: Path) -> dict | None:
    """
    Full pipeline for one movie.
    Returns a catalog-ready dict or None on total failure.
    """
    title = movie["title"]
    print(f"\n{'='*60}")
    print(f"🎬 Processing: {title}")
    print(f"{'='*60}")

    movie_dir = workspace / re.sub(r'[^A-Za-z0-9_-]', '_', movie["id"])[:40]
    movie_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download
    video_path = download_movie(movie, movie_dir)
    if not video_path:
        print(f"  ❌ Skipping {title} – download failed")
        return None

    # 2. Scene detection
    scenes = detect_and_pick_scenes(video_path, SCENES_PER_MOVIE)
    if not scenes:
        print(f"  ❌ No valid scenes found for {title}")
        return None

    # 3. Process each scene
    scenes_meta = []
    clips_dir = output_dir / re.sub(r'[^A-Za-z0-9_-]', '_', movie["id"])[:40]
    clips_dir.mkdir(parents=True, exist_ok=True)

    for i, (start, end) in enumerate(scenes, 1):
        clip_name = f"scene_{i:02d}_{int(start):05d}s.mp4"
        clip_path = clips_dir / clip_name
        print(f"\n  🎞  Scene {i}: {start:.1f}s → {end:.1f}s ({end-start:.0f}s)")

        ok = transcode_scene(video_path, start, end, clip_path)
        if not ok or not clip_path.exists():
            print(f"    ⚠  Transcode failed – skipping scene")
            continue

        transcript = transcribe_audio(clip_path)
        print(f"    💬 Transcript: {transcript[:80]}…")

        ai_meta = analyse_scene_ai(
            title=title,
            year=movie.get("year"),
            scene_num=i,
            total=len(scenes),
            transcript=transcript,
        )

        scene_entry = {
            "scene_number": i,
            "start_time":   round(start, 2),
            "end_time":     round(end, 2),
            "duration":     round(end - start, 2),
            "transcript":   transcript,
            "description":  ai_meta["description"],
            "synopsis":     ai_meta["synopsis"],
            "mood":         ai_meta["mood"],
            "noir_tropes":  ai_meta["noir_tropes"],
            "thumbnail_url": f"https://images.unsplash.com/photo-1509023464722-18d996393ca8?w=640",
            "download_url": "",  # filled in after release upload
            "release_tag":  "",
            "clip_path":    str(clip_path),
        }
        scenes_meta.append(scene_entry)
        print(f"    ✅ Scene {i} complete: {ai_meta['mood']}")

    # 4. Release the video file handle, then clean up download
    try:
        gc.collect()
        if video_path.exists():
            video_path.unlink()
        print(f"  🗑  Cleaned download: {video_path.name}")
    except Exception as e:
        print(f"  ⚠  Could not delete download: {e}")

    if not scenes_meta:
        print(f"  ❌ No scenes extracted for {title}")
        return None

    # 5. Upload to Supabase
    upload_to_supabase(movie, scenes_meta)

    # 6. Build catalog entry
    catalog_entry = {
        "id":    movie["id"],
        "title": title,
        "year":  movie.get("year"),
        "archive_id": movie["id"],
        "genre": SELECTED_GENRE,
        "is_public_domain": PUBLIC_DOMAIN_ONLY == "true",
        "scenes": [
            {k: v for k, v in s.items() if k != "clip_path"}
            for s in scenes_meta
        ],
    }
    return catalog_entry


def main():
    print("\n" + "="*60)
    print("  NOIR VAULT v2 – Dynamic Search-Driven Pipeline")
    print("="*60)
    print(f"  Keyword:       '{SEARCH_KEYWORD}' (empty = any)")
    print(f"  Genre:         {SELECTED_GENRE}")
    print(f"  Date range:    {DATE_RANGE}")
    print(f"  Public domain: {PUBLIC_DOMAIN_ONLY}")
    print(f"  Films:         {NUM_MOVIES}")
    print(f"  Archive override: {ARCHIVE_ID_OVERRIDE or 'none'}")
    print("="*60 + "\n")

    WORKSPACE.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Export a human-readable run label to GITHUB_ENV for the release name
    genre_label = SELECTED_GENRE if SELECTED_GENRE != "All Genres" else "Mixed Genres"
    kw_label = f" [{SEARCH_KEYWORD}]" if SEARCH_KEYWORD else ""
    run_label = f"{genre_label}{kw_label} – {DATE_RANGE}"
    export_env("RUN_LABEL", run_label)
    export_env("SELECTED_GENRE_LABEL", genre_label)
    export_env("NUM_MOVIES_ACTUAL", str(NUM_MOVIES))

    # Discover films
    print("🔍 Discovering films…")
    movies = discover_movies(NUM_MOVIES)

    if not movies:
        print("❌ No movies found! Exiting.")
        sys.exit(1)

    # Process each
    all_catalog_entries = []
    successes = 0
    for movie in movies:
        try:
            entry = process_movie(movie, WORKSPACE, OUTPUT_DIR)
            if entry:
                all_catalog_entries.append(entry)
                successes += 1
        except Exception as e:
            print(f"  ❌ Unhandled error processing {movie.get('title','?')}: {e}")
            gc.collect()

    # Update catalog.json
    print("\n📝 Updating catalog.json…")
    update_catalog(all_catalog_entries)

    # Summary
    print(f"\n{'='*60}")
    print(f"  ✅ Pipeline complete: {successes}/{len(movies)} films processed")
    print(f"  📁 Output directory: {OUTPUT_DIR.resolve()}")
    print(f"{'='*60}\n")

    if successes == 0:
        print("❌ No movies were successfully processed. Exiting with error.")
        sys.exit(1)


if __name__ == "__main__":
    main()
