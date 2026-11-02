# 🎞 Noir Vault v2 — Dynamic Search-Driven Cinema Engine

> AI-curated scenes from public domain films — discovered dynamically by keyword, genre, and era.

**Live Gallery:** https://hipster-doofus.github.io/noir-vault-v2/  
**v1 Repository:** https://github.com/hipster-doofus/noir-catalogue

---

## What's New in v2

| Feature | v1 | v2 |
|---|---|---|
| Movie discovery | Hardcoded catalog (20 films) | Dynamic Internet Archive search |
| Workflow inputs | None | Keyword, Genre, Era, Public Domain, Count |
| Genres supported | Film Noir only | 8 genre categories |
| Era filtering | None | Pre-1930 through 1960s+ |
| Archive ID override | None | `specific_archive_id` input |

---

## 🚀 Quick Start

### 1. Prerequisites
- GitHub account with this repo forked/cloned
- Supabase project (free tier works)
- Gemini API key (free at https://aistudio.google.com)

### 2. GitHub Secrets
Add these in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase service role key |
| `GEMINI_API_KEY` | Your Gemini API key |

### 3. Supabase Schema
Run `schema.sql` in your Supabase SQL Editor once to create the tables.

### 4. GitHub Pages
Go to **Settings → Pages → Source** and select **GitHub Actions**.

### 5. Run the Workflow
Go to **Actions → Extract & Publish → Run workflow** and fill in:

| Input | Description | Example |
|---|---|---|
| `search_keyword` | Optional keyword to search for | `heist`, `detective`, `femme` |
| `genre` | Genre filter | `Film Noir`, `Crime & Gangster` |
| `date_range` | Era filter | `1940-1959 (Classic Noir Era)` |
| `public_domain_only` | Restrict to PD films | `true` |
| `num_movies` | How many films to process | `3` (default), up to `10` |
| `specific_archive_id` | Override with one specific Archive.org ID | `Detour`, `suddenly` |

---

## 📁 Repository Structure

```
noir-vault-v2/
├── .github/
│   └── workflows/
│       └── extract_scenes.yml    # Search-driven workflow with inputs
├── pipeline.py                   # Main extraction pipeline
├── index.html                    # Web gallery (Noir Vault v2 UI)
├── catalog.json                  # Cumulative scene catalogue
├── schema.sql                    # Supabase DDL
├── requirements.txt              # Python dependencies
├── .gitignore
└── README.md
```

---

## 🔍 How Discovery Works

1. **Workflow inputs** are passed as environment variables to `pipeline.py`
2. The pipeline builds a search query for the [Internet Archive Advanced Search API](https://archive.org/advancedsearch.php):
   - Genre → `subject:("film noir")` etc.
   - Keyword → `title:("heist") OR description:("heist")`
   - Date range → `year:[1940 TO 1959]`
3. Results are shuffled and each identifier is verified via the [Archive.org Metadata API](https://archive.org/metadata/{id}) to confirm a playable video file exists
4. Films that fail verification are silently skipped; the fallback hardcoded catalog tops up the selection
5. Each confirmed film is downloaded, scene-detected, transcoded, AI-analysed, and uploaded

---

## 🎬 Scene Extraction Process

```
Internet Archive Search API
        ↓
   Metadata API (verify MP4)
        ↓
   Download film (~500MB–2GB)
        ↓
   PySceneDetect (AdaptiveDetector)
        ↓
   Pick 3 random valid scenes (10–60s)
        ↓
   FFmpeg 1080p transcode (H.264/AAC)
        ↓
   faster-whisper (audio transcript)
        ↓
   Gemini AI (description, mood, tropes)
        ↓
   Supabase insert + catalog.json update
        ↓
   GitHub Release (clip attachments)
        ↓
   GitHub Pages (web gallery)
```

---

## 🎭 Genre Reference

| Workflow Option | Archive.org Search Terms |
|---|---|
| Film Noir | `film noir` |
| Crime & Gangster | `crime gangster` |
| Mystery & Suspense | `mystery suspense` |
| Psychological Thriller | `psychological thriller` |
| Western Noir | `western noir` |
| Gothic Horror | `gothic horror` |
| Sci-Fi B-Movie | `science fiction` |
| War & Espionage | `war espionage spy` |
| All Genres | *(no genre filter)* |

---

## 🗄 Database Schema

Two tables in Supabase (`movies` + `scenes`). See [`schema.sql`](schema.sql) for full DDL.

**movies:** id, title, year, director, archive_id, genre, search_terms, is_public_domain  
**scenes:** id, movie_id, scene_number, start/end_time, transcript, description, synopsis, mood, noir_tropes[], thumbnail_url, download_url

---

## 🏃 Local Development

```powershell
# Set up environment
cd noir-vault-v2
pip install -r requirements.txt

# Set environment variables
$env:SEARCH_KEYWORD = "detective"
$env:SELECTED_GENRE = "Film Noir"
$env:DATE_RANGE = "1940-1959 (Classic Noir Era)"
$env:PUBLIC_DOMAIN_ONLY = "true"
$env:NUM_MOVIES = "1"
$env:SUPABASE_URL = "your-url"
$env:SUPABASE_KEY = "your-key"
$env:GEMINI_API_KEY = "your-key"

# Run
python pipeline.py
```

---

## 📜 License

All extracted film content is public domain. Pipeline code is MIT licensed.
