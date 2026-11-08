#!/usr/bin/env python3
"""
Noir Vault v2 – Dynamic Search-Driven & Remote Streaming Scene Extraction Pipeline
Optimized with single-pass remote stream caching, FFmpeg clipping, and resilient Gemini AI analysis.
"""

import gc
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

# Optional dependencies check
try:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector
    SCENEDETECT_OK = True
except ImportError:
    SCENEDETECT_OK = False
    print("⚠ PySceneDetect not available – scene detection disabled")

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
    print("⚠ supabase-py not available – DB upload disabled")

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
        from google import genai as genai_module
        genai = genai_module
        GEMINI_OK = True
    except ImportError:
        genai = None
        GEMINI_OK = False

# Environment & Configurations
MODE = os.environ.get("MODE", "auto").strip()
SEARCH_KEYWORD = os.environ.get("SEARCH_KEYWORD", "").strip()
SELECTED_GENRE = os.environ.get("SELECTED_GENRE", "All Genres").strip()
DATE_RANGE = os.environ.get("DATE_RANGE", "All Eras").strip()
PUBLIC_DOMAIN_ONLY = os.environ.get("PUBLIC_DOMAIN_ONLY", "true").lower().strip()
NUM_MOVIES = int(os.environ.get("NUM_MOVIES", "20"))
ARCHIVE_ID_OVERRIDE = os.environ.get("ARCHIVE_ID_OVERRIDE", "").strip()
SELECTED_MOVIES_JSON = os.environ.get("SELECTED_MOVIES_JSON", "[]").strip()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip().strip("[]'\"")
RELEASE_TAG = os.environ.get("RELEASE_TAG", "").strip().strip("[]'\"")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

WORKSPACE = Path("temp_workspace")
OUTPUT_DIR = Path("output")
CATALOG_PATH = Path("catalog.json")

SCENES_PER_MOVIE = int(os.environ.get("SCENES_PER_MOVIE", "30"))
MIN_SCENE_SECS = 2
MAX_SCENE_SECS = 5

GENRE_SEARCH_MAP = {
    "Film Noir": "film noir",
    "Crime & Gangster": "crime gangster",
    "Mystery & Suspense": "mystery suspense",
    "Psychological Thriller": "psychological thriller",
    "Western Noir": "western noir",
    "Gothic Horror": "gothic horror",
    "Sci-Fi B-Movie": "science fiction",
    "War & Espionage": "war espionage spy",
    "All Genres": "",
}

FALLBACK_CATALOG = [
    {"title": "Detour", "id": "Detour"},
    {"title": "Suddenly", "id": "suddenly"},
    {"title": "The Stranger", "id": "TheStranger_0"},
    {"title": "Quicksand", "id": "Quicksand_clear"},
    {"title": "The Strange Love of Martha Ivers", "id": "Martha_Ivers_movie"},
    {"title": "Please Murder Me", "id": "Please_Murder_Me_movie"},
]


def _get(url, timeout=20, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NoirVaultV2/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    ⚠ GET {url[:80]}… failed: {e}")
        return None


def parse_date_range(date_range_label: str) -> tuple[int | None, int | None]:
    label = date_range_label.strip()
    if not label or label.lower() == "all eras":
        return None, None
    m = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", label)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def search_archive(keyword="", genre="All Genres", date_range="All Eras", pd_only="true", num_needed=10) -> list[dict]:
    terms = ["mediatype:(movies)", "collection:(feature_films)"]
    genre_term = GENRE_SEARCH_MAP.get(genre, "")
    if genre_term:
        words = genre_term.split()
        genre_parts = " OR ".join(f'subject:("{w}")' for w in words)
        terms.append(f"({genre_parts})")

    if keyword:
        safe_kw = keyword.replace('"', "")
        terms.append(f'(title:("{safe_kw}") OR subject:("{safe_kw}") OR description:("{safe_kw}"))')

    start_year, end_year = parse_date_range(date_range)
    if start_year and end_year:
        terms.append(f"year:[{start_year} TO {end_year}]")

    query = " AND ".join(terms)
    print(f"  🔍 Archive search query: {query}")

    params = {
        "q": query,
        "fl[]": "identifier,title,year,description",
        "rows": min(num_needed * 4, 50),
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    data = _get("https://archive.org/advancedsearch.php", params=params)
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
    return results


def resolve_archive_mp4(archive_id: str) -> str | None:
    meta = _get(f"https://archive.org/metadata/{archive_id}", timeout=15)
    if not meta:
        return None
    files = meta.get("files", [])
    mp4s = [f for f in files if f.get("name", "").lower().endswith(".mp4") and "_thumb" not in f.get("name", "").lower()]
    if not mp4s:
        return None

    best = max(mp4s, key=lambda f: int(f.get("size", 0)))
    return f"https://archive.org/download/{archive_id}/{best['name']}"


def discover_movies(num_needed: int) -> list[dict]:
    if MODE == "manual" and SELECTED_MOVIES_JSON and SELECTED_MOVIES_JSON != "[]":
        try:
            items = json.loads(SELECTED_MOVIES_JSON)
            verified = []
            for item in items:
                archive_id = item.get("id") or item.get("archive_id")
                url = resolve_archive_mp4(archive_id)
                if url:
                    verified.append({"title": item.get("title", archive_id), "id": archive_id, "url": url, "year": None})
            if verified:
                print(f"🎯 Selected {len(verified)} films via manual Web UI dispatch")
                return verified
        except Exception as e:
            print(f"⚠ Failed to parse MANUAL mode payload: {e}")

    if ARCHIVE_ID_OVERRIDE:
        print(f"🎯 Using archive ID override: {ARCHIVE_ID_OVERRIDE}")
        url = resolve_archive_mp4(ARCHIVE_ID_OVERRIDE)
        if url:
            return [{"title": ARCHIVE_ID_OVERRIDE, "id": ARCHIVE_ID_OVERRIDE, "url": url, "year": None}]

    candidates = search_archive(
        keyword=SEARCH_KEYWORD,
        genre=SELECTED_GENRE,
        date_range=DATE_RANGE,
        pd_only=PUBLIC_DOMAIN_ONLY,
        num_needed=num_needed,
    )

    if not candidates:
        print("  ⚠ No candidates matched exact search parameters. Falling back to broad search...")
        candidates = search_archive(genre="All Genres", date_range="All Eras", num_needed=num_needed)

    verified = []
    for c in candidates:
        if len(verified) >= num_needed:
            break
        url = resolve_archive_mp4(c["id"])
        if url:
            verified.append({"title": c["title"], "id": c["id"], "url": url, "year": c.get("year")})

    if len(verified) < num_needed:
        print("  ⚠ Filling remaining slots from fallback catalog...")
        for fb in FALLBACK_CATALOG:
            if len(verified) >= num_needed:
                break
            url = resolve_archive_mp4(fb["id"])
            if url:
                verified.append({"title": fb["title"], "id": fb["id"], "url": url, "year": None})

    return verified[:num_needed]


def stream_sample_segment(stream_url: str, dest_path: Path, duration_mins: int = 15) -> bool:
    random_start_sec = random.randint(300, 1800)
    start_time_str = f"{random_start_sec // 3600:02d}:{(random_start_sec % 3600) // 60:02d}:{random_start_sec % 60:02d}"
    print(f"  🌐 Remote Streaming {duration_mins}-min window starting at {start_time_str}...")

    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", start_time_str,
        "-i", stream_url,
        "-t", f"00:{duration_mins:02d}:00",
        "-c", "copy",
        str(dest_path)
    ]
    try:
        t0 = time.time()
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if res.returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 100_000:
            print(f"  [✓] Segment cached in {time.time()-t0:.1f}s ({dest_path.stat().st_size / (1024*1024):.1f} MB)")
            return True
    except Exception as e:
        print(f"  ⚠ Remote stream copy error: {e}")

    return False


def detect_and_pick_scenes(sample_path: Path, n: int = SCENES_PER_MOVIE) -> list[tuple[float, float]]:
    if not SCENEDETECT_OK:
        return [(i * 60, i * 60 + 45) for i in range(1, n + 1)]

    print(f"  🎞 Running scene detection on cached segment...")
    video = None
    try:
        video = open_video(str(sample_path))
        manager = SceneManager()
        manager.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=24))
        manager.detect_scenes(video, frame_skip=2)
        scene_list = manager.get_scene_list()
    except Exception as e:
        print(f"  ⚠ Scene detection error: {e}")
        scene_list = []
    finally:
        if video is not None:
            del video
        gc.collect()

    candidates = [(s.seconds, e.seconds) for s, e in scene_list if MIN_SCENE_SECS <= (e.seconds - s.seconds) <= MAX_SCENE_SECS]
    if len(candidates) < n:
        candidates = [(i * 60, i * 60 + 45) for i in range(1, n + 1)]

    random.shuffle(candidates)
    return candidates[:n]


def transcode_scene(src: Path, start: float, end: float, dest: Path) -> bool:
    dur = end - start
    cmd = [
        FFMPEG_BIN, "-y",
        # 1. Input options (placed BEFORE -i)
        "-ss", str(start),
        "-i", str(src),
        
        # 2. Trim duration (placed AFTER -i for precise output trimming)
        "-t", str(dur),
        
        # 3. Video filters (fixed scale expression syntax and formatting)
        "-vf", "scale=w='min(iw,1920)':h='min(ih,1080)':force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        
        # 4. Audio filters & sync correction
        "-af", "aresample=async=1,loudnorm=I=-16:TP=-1.5:LRA=11",
        
        # 5. Video & Audio Encoders
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        
        # 6. Output flags & sync handling
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
        str(dest)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=300)
        return res.returncode == 0
    except Exception as e:
        print(f"  ⚠ Transcode failed: {e}")
        return False


def transcribe_audio(clip_path: Path) -> str:
    if WHISPER_OK:
        try:
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(clip_path), beam_size=1)
            text = " ".join(s.text for s in segments).strip()
            del model
            gc.collect()
            return text or "[No dialogue detected]"
        except Exception:
            pass
    return "[Transcript unavailable]"


NOIR_PROMPT_TEMPLATE = """You are a film noir scholar.
Movie: "{title}" ({year})
Scene: {scene_num} of {total_scenes}
Transcript: "{transcript}"

Respond ONLY with valid JSON:
{{
  "description": "2-sentence visual description",
  "synopsis": "1-sentence scene plot summary",
  "mood": "Moody, Tense",
  "noir_tropes": ["shadow play", "moral ambiguity"]
}}"""


def analyse_scene_ai(title: str, year, scene_num: int, total: int, transcript: str) -> dict:
    default = {
        "description": f"Scene {scene_num} from {title} ({year})",
        "synopsis": "A pivotal moment in the film.",
        "mood": "Moody, Tense",
        "noir_tropes": ["shadow play", "moral ambiguity"],
    }
    if not GEMINI_OK:
        print("  ⚠ Gemini library (google-generativeai / google-genai) not available.")
        return default

    if not GEMINI_API_KEY:
        print("  ⚠ GEMINI_API_KEY secret is missing or empty.")
        return default

    prompt = NOIR_PROMPT_TEMPLATE.format(
        title=title, year=year or "unknown", scene_num=scene_num, total_scenes=total, transcript=transcript[:500]
    )

    candidate_models = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-flash"]
    for model_name in candidate_models:
        try:
            print(f"    🤖 Requesting Gemini analysis using model '{model_name}'...")
            if hasattr(genai, "Client"):
                client = genai.Client(api_key=GEMINI_API_KEY)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                raw_text = resp.text
            else:
                genai.configure(api_key=GEMINI_API_KEY)
                model_obj = genai.GenerativeModel(model_name)
                resp = model_obj.generate_content(prompt)
                raw_text = resp.text

            raw = raw_text.strip()
            raw = re.sub(r"^```(json)?", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            
            parsed = json.loads(raw)
            print("    [✓] Gemini scene analysis generated successfully.")
            return {
                "description": str(parsed.get("description", default["description"])),
                "synopsis": str(parsed.get("synopsis", default["synopsis"])),
                "mood": str(parsed.get("mood", default["mood"])),
                "noir_tropes": list(parsed.get("noir_tropes", default["noir_tropes"])),
            }
        except Exception as e:
            print(f"    ⚠ Gemini error on model '{model_name}': {e}")
            continue

    print("  ⚠ All Gemini model attempts failed; using fallback default descriptions.")
    return default


def upload_asset_to_github_release(tag_name: str, file_path: Path) -> str | None:
    raw_repo = (GITHUB_REPOSITORY or "").strip().strip("[]'\"")
    tag = (tag_name or "").strip().strip("[]'\"")
    repo = re.sub(r"[\[\]'\"]", "", raw_repo).strip()

    if not GITHUB_TOKEN or not repo or not tag:
        return None

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        get_rel_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        res = requests.get(get_rel_url, headers=headers)
        if res.status_code != 200:
            return None

        release_info = res.json()
        base_upload_url = release_info["upload_url"].split("{")[0]
        upload_url = f"{base_upload_url}?name={urllib.parse.quote(file_path.name)}"

        with open(file_path, "rb") as f:
            file_data = f.read()

        upload_headers = headers.copy()
        upload_headers["Content-Type"] = "application/octet-stream"

        upload_res = requests.post(upload_url, headers=upload_headers, data=file_data)
        if upload_res.status_code in [200, 201]:
            download_url = upload_res.json().get("browser_download_url", "")
            print(f"    ☁ Uploaded to GitHub Release: {file_path.name}")
            return download_url
    except Exception as e:
        print(f"    ⚠ Upload asset error: {e}")
    return None


def upload_to_supabase(movie_meta: dict, scenes_meta: list[dict]):
    if not SUPABASE_OK or not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        movie_row = {
            "title": movie_meta["title"],
            "year": movie_meta.get("year"),
            "archive_id": movie_meta.get("id"),
            "genre": SELECTED_GENRE,
            "is_public_domain": PUBLIC_DOMAIN_ONLY == "true",
        }
        res = client.table("movies").insert(movie_row).execute()
        movie_id = res.data[0]["id"] if res.data else None

        for s in scenes_meta:
            s_row = {**s, "movie_id": movie_id}
            s_row.pop("clip_path", None)
            client.table("scenes").insert(s_row).execute()
        print(f"  📦 Supabase updated: {len(scenes_meta)} scenes inserted")
    except Exception as e:
        print(f"  ⚠ Supabase notice: {e}")


def update_catalog(all_movie_entries: list[dict]):
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

    existing["generated_at"] = datetime.now(timezone.utc).isoformat()
    CATALOG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def process_movie(movie: dict, workspace: Path, output_dir: Path) -> dict | None:
    title = movie.get("title", "Untitled Movie")
    print(f"\n🎬 Processing (Remote Streaming): {title}")

    movie_dir = workspace / re.sub(r"[^A-Za-z0-9_-]", "_", movie["id"])[:40]
    movie_dir.mkdir(parents=True, exist_ok=True)

    sample_path = movie_dir / "sample_stream.mp4"
    if not stream_sample_segment(movie["url"], sample_path):
        return None

    scenes = detect_and_pick_scenes(sample_path, SCENES_PER_MOVIE)
    if not scenes:
        return None

    scenes_meta = []
    clips_dir = output_dir / re.sub(r"[^A-Za-z0-9_-]", "_", movie["id"])[:40]
    clips_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^A-Za-z0-9_-]", "_", title)[:40].strip("_") or "movie"

    for i, (start, end) in enumerate(scenes, 1):
        clip_name = f"{safe_title}_scene_{i:02d}_{int(start):05d}s.mp4"
        clip_path = clips_dir / clip_name

        if not transcode_scene(sample_path, start, end, clip_path):
            continue

        transcript = transcribe_audio(clip_path)
        ai_meta = analyse_scene_ai(title, movie.get("year"), i, len(scenes), transcript)
        download_url = upload_asset_to_github_release(RELEASE_TAG, clip_path) or ""

        scenes_meta.append({
            "scene_number": i,
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "duration": round(end - start, 2),
            "transcript": transcript,
            "description": ai_meta["description"],
            "synopsis": ai_meta["synopsis"],
            "mood": ai_meta["mood"],
            "noir_tropes": ai_meta["noir_tropes"],
            "thumbnail_url": "https://images.unsplash.com/photo-1509023464722-18d996393ca8?w=640",
            "download_url": download_url,
            "release_tag": RELEASE_TAG,
            "clip_path": str(clip_path),
        })

    if sample_path.exists():
        sample_path.unlink()

    if not scenes_meta:
        return None

    catalog_entry = {
        "id": movie["id"],
        "title": title,
        "year": movie.get("year"),
        "archive_id": movie["id"],
        "genre": SELECTED_GENRE,
        "is_public_domain": PUBLIC_DOMAIN_ONLY == "true",
        "scenes": [{k: v for k, v in s.items() if k != "clip_path"} for s in scenes_meta],
    }

    movie_json_path = clips_dir / f"{safe_title}_metadata.json"
    movie_json_path.write_text(json.dumps(catalog_entry, indent=2))
    upload_asset_to_github_release(RELEASE_TAG, movie_json_path)
    upload_to_supabase(movie, scenes_meta)

    return catalog_entry


def main():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    movies = discover_movies(NUM_MOVIES)
    if not movies:
        print("❌ No movies found! Exiting.")
        sys.exit(1)

    catalog_entries = []
    for movie in movies:
        try:
            entry = process_movie(movie, WORKSPACE, OUTPUT_DIR)
            if entry:
                catalog_entries.append(entry)
        except Exception as e:
            print(f"❌ Error processing {movie.get('title')}: {e}")

    update_catalog(catalog_entries)


if __name__ == "__main__":
    main()
