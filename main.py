"""
Video Downloader API — MVP backend.

Wraps yt-dlp to:
  1. Analyze a URL and list available formats/qualities.
  2. Download the chosen format (merging video+audio with ffmpeg when needed)
     and stream the resulting file back to the client.

Requires: ffmpeg installed on the host (see Dockerfile).
"""

import re
import shutil
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Dict

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from starlette.background import BackgroundTask
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ---------- Logging Setup ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("downloader")

APP_DIR = Path(__file__).parent
DOWNLOAD_DIR = APP_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

progress_store: Dict[str, dict] = {}

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Video Downloader API", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Tighten allow_origins to your actual frontend domain before going public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Schemas ----------

class AnalyzeRequest(BaseModel):
    url: HttpUrl


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: Optional[str] = None
    fps: Optional[float] = None
    filesize_approx: Optional[int] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    label: str


class AnalyzeResponse(BaseModel):
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    uploader: Optional[str] = None
    formats: List[FormatInfo]


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str


class DownloadResponse(BaseModel):
    job_id: str
    status: str


# ---------- Helpers ----------

def _base_ydl_opts() -> dict:
    opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
        'max_filesize': 2 * 1024 * 1024 * 1024,
    }
    
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
        
    return opts


def _has_video(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none")


def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def _label_for(f: dict) -> str:
    if _has_video(f) and _has_audio(f):
        res = f.get("format_note") or f.get("resolution") or ""
        fps = f"{int(f['fps'])}fps" if f.get("fps") else ""
        return f"{res} {fps} — video+audio".strip()
    if _has_video(f):
        res = f.get("format_note") or f.get("resolution") or ""
        return f"{res} — video only".strip()
    if _has_audio(f):
        abr = f.get("abr")
        return f"Audio only — {int(abr)}kbps" if abr else "Audio only"
    return f.get("format") or f.get("format_id", "unknown")


def _sort_key(f: dict):
    has_v, has_a = _has_video(f), _has_audio(f)
    height = 0
    res = f.get("format_note") or f.get("resolution") or ""
    m = re.search(r"(\d+)p", str(res))
    if m:
        height = int(m.group(1))
    # combined video+audio first, then video-only, then audio-only; highest res first
    return (not (has_v and has_a), not has_v, -height)


def _sanitize_filename(name: str) -> str:
    path = Path(name)
    stem = re.sub(r"[^\w\-. ]", "_", path.stem)
    ext = re.sub(r"[^\w\-. ]", "_", path.suffix)
    return (stem[:100] + ext) or "video.mp4"


# ---------- Endpoints ----------

@app.get("/")
async def serve_frontend():
    return FileResponse(APP_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/analyze", response_model=AnalyzeResponse)
@limiter.limit("5/minute")
async def analyze(req: AnalyzeRequest, request: Request):
    logger.info(f"Analyzing URL: {req.url}")
    def extract():
        with yt_dlp.YoutubeDL(_base_ydl_opts()) as ydl:
            return ydl.extract_info(str(req.url), download=False)

    try:
        info = await asyncio.to_thread(extract)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Could not process this URL: {e}")

    raw_formats = info.get("formats") or []
    if not raw_formats and info.get("url"):
        raw_formats = [info]

    video_formats = {}
    audio_formats = {}
    
    for f in raw_formats:
        # Ignore formats with no format_id
        if not f.get("format_id"):
            continue
            
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        
        if has_v:
            height = f.get("height") or 0
            if height == 0:
                res_str = f.get("format_note") or f.get("resolution") or ""
                m = re.search(r"(\d+)p", str(res_str))
                if m:
                    height = int(m.group(1))
            
            # Prefer formats with higher tbr (bitrate) for the same resolution
            tbr = f.get("tbr") or 0
            ext = f.get("ext", "")
            
            # Group by height and extension so we get 1080p mp4 and 1080p webm uniquely
            key = f"{height}_{ext}"
            existing = video_formats.get(key)
            if not existing or (tbr > (existing.get("tbr") or 0)):
                video_formats[key] = f
                
        elif has_a and not has_v:
            abr = f.get("abr") or 0
            if abr > 0:
                ext = f.get("ext", "")
                key = f"{int(abr)}_{ext}"
                audio_formats[key] = f

    formats = []
    
    # Add a smart default best option
    formats.append(
        FormatInfo(
            format_id="bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            ext="mp4",
            label="Best Quality (Auto) — video+audio",
            filesize_approx=None
        )
    )

    # Sort video by height descending
    sorted_v = sorted(video_formats.values(), key=lambda x: x.get("height") or 0, reverse=True)
    for f in sorted_v:
        height = f.get("height") or 0
        if height == 0: continue
        fps = f.get("fps")
        fps_str = f"{int(fps)}fps " if fps and fps > 30 else ""
        ext = f.get("ext", "mp4")
        
        # If it already has audio natively, don't append +bestaudio
        has_a = f.get("acodec") not in (None, "none")
        fmt_id = f["format_id"] if has_a else f"{f['format_id']}+bestaudio/best"
        
        label = f"{height}p {fps_str}({ext}) — video+audio"
        formats.append(
            FormatInfo(
                format_id=fmt_id,
                ext=ext,
                label=label,
                filesize_approx=f.get("filesize") or f.get("filesize_approx")
            )
        )

    # Sort audio by bitrate descending
    sorted_a = sorted(audio_formats.values(), key=lambda x: x.get("abr") or 0, reverse=True)
    for f in sorted_a:
        abr = int(f.get("abr") or 0)
        ext = f.get("ext", "m4a")
        label = f"{abr}kbps ({ext}) — audio only"
        formats.append(
            FormatInfo(
                format_id=f["format_id"],
                ext=ext,
                label=label,
                filesize_approx=f.get("filesize") or f.get("filesize_approx")
            )
        )

    return AnalyzeResponse(
        title=info.get("title", "video"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        uploader=info.get("uploader"),
        formats=formats,
    )


@app.post("/api/download", response_model=DownloadResponse)
@limiter.limit("2/minute")
async def download(req: DownloadRequest, request: Request):
    logger.info(f"Download requested for URL {req.url} with format {req.format_id}")
    job_id = str(uuid.uuid4())
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(job_dir / "%(title).80s.%(ext)s")

    progress_store[job_id] = {"status": "starting", "percent": 0, "eta": "unknown"}

    def my_hook(d):
        if d['status'] == 'downloading':
            p_str = d.get('_percent_str', '0%')
            # Remove ANSI escape codes
            p_str = re.sub(r'\x1b\[[0-9;]*m', '', p_str).strip()
            e_str = d.get('_eta_str', 'unknown')
            e_str = re.sub(r'\x1b\[[0-9;]*m', '', e_str).strip()
            try:
                percent = float(p_str.replace('%', ''))
            except ValueError:
                percent = 0
            progress_store[job_id].update({
                "status": "downloading",
                "percent": percent,
                "eta": e_str
            })
        elif d['status'] == 'finished':
            progress_store[job_id].update({
                "status": "processing",
                "percent": 100
            })

    fmt = req.format_id
    if "+" not in fmt and fmt not in ("best", "bestaudio"):
        fmt = f"{fmt}+bestaudio/best"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": fmt,
        "progress_hooks": [my_hook],
        "extractor_args": {"youtube": ["player_client=ios,android,web"]},
        "rm_cachedir": True,
        "max_filesize": 2000 * 1024 * 1024,  # 2GB limit
    }
    
    if os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = "cookies.txt"

    def run_download():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(str(req.url), download=True)
            progress_store[job_id]["status"] = "completed"
            logger.info(f"Job {job_id} completed successfully.")
        except Exception as e:
            progress_store[job_id]["status"] = "error"
            progress_store[job_id]["error"] = str(e)
            logger.error(f"Job {job_id} failed: {e}")

    asyncio.create_task(asyncio.to_thread(run_download))
    return DownloadResponse(job_id=job_id, status="started")

@app.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    if job_id not in progress_store:
        raise HTTPException(status_code=404, detail="Job not found")
    return progress_store[job_id]

@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    if job_id not in progress_store or progress_store[job_id]["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not ready")
        
    job_dir = DOWNLOAD_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job directory not found")

    candidates = [p for p in job_dir.glob("*") if p.is_file()]
    if not candidates:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Download produced no file")
        
    final_path = candidates[0]
    
    cleanup = BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True)
    return FileResponse(
        path=final_path,
        filename=_sanitize_filename(final_path.name),
        media_type="application/octet-stream",
        background=cleanup,
    )
