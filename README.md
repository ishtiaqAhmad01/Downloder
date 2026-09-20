# Downloder

A clean, ad-free video downloader — paste a link, pick a quality, download.
Backend: FastAPI + yt-dlp + ffmpeg. Frontend: plain HTML/JS served directly by the backend.

## Run locally

### 1. Backend

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Install ffmpeg (required for merging video+audio at high quality):
- Ubuntu/Debian: `sudo apt install ffmpeg`
- macOS: `brew install ffmpeg`
- Windows: [download from ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

Start the API:

```bash
uvicorn main:app --reload --port 8000
```

Check it's alive: http://localhost:8000
The frontend will be served directly at this URL.

## Run via Docker (Recommended)

You can spin up the entire application, including the frontend, backend, and necessary dependencies like `ffmpeg`, using Docker.

```bash
docker compose up -d
```
Then visit `http://localhost:8000`.

## Run with Docker (backend only)

```bash
cd backend
docker build -t downloder-api .
docker run -p 8000:8000 downloder-api
```

## How it works

1. `POST /api/analyze` — takes a URL, runs `yt-dlp` in "info only" mode, returns
   title/thumbnail/duration and a sorted list of available formats
   (combined video+audio first, highest resolution first).
2. `POST /api/download` — takes the URL + chosen `format_id`, runs `yt-dlp`
   for real (downloading + using ffmpeg to merge video/audio if needed),
   streams the finished file back, then deletes the temp file.

No per-site code is needed — yt-dlp's built-in extractors cover 1000+ sites.
Keep the `yt-dlp` pin in `requirements.txt` reasonably fresh; site extractors
break when platforms change their internals, and yt-dlp ships fixes often.

## Before going public

Read the "Compliance & legal" section of `PRODUCT_BACKLOG.md`. In short:
add a clear ToS/disclaimer, keep the supported-content scope sensible,
and have a takedown process before you promote this widely.
