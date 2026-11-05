import gc
import os
import re
from pathlib import Path

# Adjust existing constants/imports as required by your module structure
SCENES_PER_MOVIE = 5
SELECTED_GENRE = os.getenv("SELECTED_GENRE", "Film Noir")
PUBLIC_DOMAIN_ONLY = os.getenv("PUBLIC_DOMAIN_ONLY", "true")


def process_movie(movie: dict, workspace: Path, output_dir: Path) -> dict | None:
    """
    Full pipeline for one movie.
    Returns a catalog-ready dict or None on total failure.
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

    # Sanitize title for OS-safe filenames (with fallback to default)
    safe_title = re.sub(r'[^A-Za-z0-9_-]', '_', title)[:40].strip('_') or "movie"

    for i, (start, end) in enumerate(scenes, 1):
        # Format filename: <MovieTitle>_scene_<num>_<start_sec>s.mp4
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
            "download_url": "",  # filled in after release upload
            "release_tag":  "",
            "clip_path":    str(clip_path),
        }
        scenes_meta.append(scene_entry)
        print(f"    ✅ Scene {i} complete: {ai_meta['mood']}")

    # 4. Release video handle & clean up download
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
        "is_public_domain": PUBLIC_DOMAIN_ONLY.lower() == "true",
        "scenes": [
            {k: v for k, v in s.items() if k != "clip_path"}
            for s in scenes_meta
        ],
    }
    return catalog_entry
