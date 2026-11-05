import gc
import json
import os
import re
import requests
from pathlib import Path

SCENES_PER_MOVIE = 5
SELECTED_GENRE = os.getenv("SELECTED_GENRE", "Film Noir")
PUBLIC_DOMAIN_ONLY = os.getenv("PUBLIC_DOMAIN_ONLY", "true")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # e.g., "user/repo"
RELEASE_TAG = os.getenv("RELEASE_TAG")


def upload_asset_to_github_release(tag_name: str, file_path: Path) -> str | None:
    """
    Uploads a single file asset directly to an existing GitHub Release via REST API.
    Returns the public download URL or None if skipped/failed.
    """
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY or not tag_name:
        print("  ℹ  GitHub Release upload skipped (missing GITHUB_TOKEN/RELEASE_TAG)")
        return None

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        # 1. Get the release ID for tag
        get_rel_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/tags/{tag_name}"
        res = requests.get(get_rel_url, headers=headers)
        if res.status_code != 200:
            print(f"  ⚠  Could not find release tag '{tag_name}': {res.text}")
            return None

        release_info = res.json()
        upload_url_template = release_info["upload_url"]  # Format: url{?name,label}
        upload_url = upload_url_template.split("{")[0] + f"?name={file_path.name}"

        # 2. Upload file binary
        with open(file_path, "rb") as f:
            file_data = f.read()

        upload_headers = headers.copy()
        upload_headers["Content-Type"] = "application/octet-stream"

        upload_res = requests.post(upload_url, headers=upload_headers, data=file_data)
        if upload_res.status_code in [200, 201]:
            asset_info = upload_res.json()
            download_url = asset_info.get("browser_download_url", "")
            print(f"    ☁  Uploaded to GitHub Release: {file_path.name}")
            return download_url
        else:
            print(f"    ⚠  Failed to upload asset {file_path.name}: {upload_res.text}")
            return None

    except Exception as e:
        print(f"    ⚠  Error uploading asset {file_path.name} to GitHub Release: {e}")
        return None


def process_movie(movie: dict, workspace: Path, output_dir: Path) -> dict | None:
    """
    Full pipeline for one movie with instant per-movie uploads to GitHub Releases and Supabase.
    """
    title = movie.get("title", "Untitled Movie")
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

    # Clean title for safe filenames across all operating systems
    safe_title = re.sub(r'[^A-Za-z0-9_-]', '_', title)[:40].strip('_') or "movie"

    for i, (start, end) in enumerate(scenes, 1):
        clip_name = f"{safe_title}_scene_{i:02d}_{int(start):05d}s.mp4"
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

        # Upload MP4 clip immediately to GitHub Release
        download_url = upload_asset_to_github_release(RELEASE_TAG, clip_path) or ""

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
            "thumbnail_url": "https://images.unsplash.com/photo-1509023464722-18d996393ca8?w=640",
            "download_url": download_url,
            "release_tag":  RELEASE_TAG or "",
            "clip_path":    str(clip_path),
        }
        scenes_meta.append(scene_entry)
        print(f"    ✅ Scene {i} complete: {ai_meta['mood']}")

    # 4. Release video file handle & clean up download
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

    # 5. Build catalog entry & per-movie JSON metadata file
    catalog_entry = {
        "id":               movie["id"],
        "title":            title,
        "year":             movie.get("year"),
        "archive_id":       movie["id"],
        "genre":            SELECTED_GENRE,
        "is_public_domain": PUBLIC_DOMAIN_ONLY.lower() == "true",
        "scenes": [
            {k: v for k, v in s.items() if k != "clip_path"}
            for s in scenes_meta
        ],
    }

    movie_json_path = clips_dir / f"{safe_title}_metadata.json"
    with open(movie_json_path, "w", encoding="utf-8") as f:
        json.dump(catalog_entry, f, indent=2)
    print(f"  📄 Saved metadata JSON: {movie_json_path.name}")

    # Upload JSON metadata to GitHub Release
    upload_asset_to_github_release(RELEASE_TAG, movie_json_path)

    # 6. Upload to Supabase (if configured)
    try:
        upload_to_supabase(movie, scenes_meta)
    except Exception as e:
        print(f"  ⚠  Supabase upload skipped or failed: {e}")

    return catalog_entry
