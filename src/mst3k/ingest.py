"""Stage 1: ingest ANY video into source.mp4 + meta.json.

Accepts: local paths, YouTube URLs, archive.org direct file URLs,
archive.org item pages, or any direct video URL. Everything converges on
one source.mp4 so downstream stages are source-agnostic.
"""
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".ogv"}
MAX_SECONDS = 9000  # 2.5h cap


def slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "video"


def _is_youtube(url: str) -> bool:
    h = urlparse(url).netloc.lower()
    return h in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be") or h.endswith(".youtube.com")


def _is_archive_org(url: str) -> bool:
    h = urlparse(url).netloc.lower()
    return h.endswith("archive.org")


def _is_video_file(url: str) -> bool:
    """URL with explicit media filename we can simply fetch."""
    ext = Path(urlparse(url).path).suffix.lower()
    return ext in VIDEO_EXTS


def _has_extractor(url: str) -> bool:
    """Rough heuristic: known sites we trust yt-dlp to handle."""
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in (
        "youtube.com", "youtu.be", "youtube-nocookie.com",
        "archive.org",
        "vimeo.com", "player.vimeo.com",
        "dailymotion.com", "tiktok.com",
        "twitch.tv", "clips.twitch.tv",
        "reddit.com", "twitter.com", "x.com",
        "instagram.com", "facebook.com", "streamable.com",
    ))


def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    fmt = info.get("format", {})
    return {
        "duration": float(fmt.get("duration", 0)),
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "fps": eval(v.get("r_frame_rate", "30/1")) if v.get("r_frame_rate") else 30.0,
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
        "title": fmt.get("tags", {}).get("title", ""),
        "size_bytes": int(fmt.get("size", 0)),
    }


def _validate(job: dict, meta: dict) -> None:
    if meta["duration"] <= 0:
        raise SystemExit("Ingest: could not determine duration (corrupt or not a video?)")
    if meta["duration"] > MAX_SECONDS:
        raise SystemExit(f"Ingest: {meta['duration']:.0f}s > {MAX_SECONDS}s cap")
    if not meta["has_audio"]:
        print("    WARNING: no audio track — riffs will play over silence")


def _normalize(job: dict, src: Path) -> tuple[Path, dict]:
    """Ensure a probeable/usable mp4 (re-wrap/transcode if needed)."""
    out = job["dir"] / "source.mp4"
    if out.exists():
        return out, _probe(out)
    meta = _probe(src)
    if src.suffix.lower() == ".mp4":
        # hard-link (same fs) so the dir can be renamed without breaking the
        # descriptor; symlink breaks when the quoted directory moves.
        try:
            out.hardlink_to(src)
        except OSError:
            import shutil; shutil.copy2(src, out)
        return out, meta
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", str(out)], check=True)
    return out, _probe(out)


def _dl(url: str, dest: Path) -> Path:
    """Plain HTTP download with basic follow-redirects & progress."""
    req = urllib.request.Request(url, headers={"User-Agent": "mst3k-anything/0.1"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"    download {done/1048576:.1f}/{total/1048576:.1f} MB", end="\r", flush=True)
    print()
    return dest


def _ytdlp_bin() -> str:
    """Prefer ~/.local/bin/yt-dlp (recent) over system yt-dlp."""
    local = Path.home() / ".local" / "bin" / "yt-dlp"
    return str(local) if local.exists() else "yt-dlp"


def _ytdlp(url: str, job: dict) -> tuple[Path, dict]:
    out = job["dir"] / "ytdl.%(ext)s"
    base = [_ytdlp_bin(), "--no-playlist",
            "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
            "--merge-output-format", "mp4",
            "--write-info-json", "--write-subs", "--sub-langs", "en.*,en",
            "--sub-format", "srt/vtt/best", "--remote-components", "ejs:github",
            "-o", out, url]
    try:
        subprocess.run(base, check=True)
    except subprocess.CalledProcessError:
        # fallback client profile (yt-dlp sometimes works picks rarely fail
        # on datacenter IPs)
        fallback = base.copy()
        fallback[1:1] = ["--extractor-args", "youtube:player_client=android"]
        subprocess.run(fallback, check=True)
    vids = sorted(job["dir"].glob("ytdl.*"))
    vid = next((p for p in vids if p.suffix.lower() in VIDEO_EXTS), None)
    if vid is None:
        raise SystemExit("yt-dlp produced no video file")
    info = {}
    ij = next(job["dir"].glob("ytdl*.info.json"), None)
    if ij:
        info = json.loads(ij.read_text())
    meta_extra = {"title": info.get("title", ""),
                  "description": (info.get("description") or "")[:800],
                  "uploader": info.get("uploader", "")}
    sub = None
    for ext in (".srt", ".vtt"):
        sub = next(job["dir"].glob(f"ytdl.*{ext}"), None)
        if sub:
            break
    return vid, meta_extra, sub


def ingest(source: str, job: dict) -> tuple[Path, dict]:
    d = job["dir"]
    d.mkdir(parents=True, exist_ok=True)
    p = urlparse(source)

    if p.scheme in ("", "file") or Path(source).exists():
        src = Path(source).resolve()
        vid, meta = _normalize(job, src)
        meta.update({"kind_hint": "local", "description": "", "uploader": ""})
    elif _is_video_file(source):
        # Looks like a direct media URL — download plain, keep metadata thin
        ext = Path(p.path).suffix.lower()
        dest = d / ("download" + ext)
        if not dest.exists():
            _dl(source, dest)
        vid, meta = _normalize(job, dest)
        meta.update({"kind_hint": "direct", "title": Path(p.path).stem,
                     "description": "", "uploader": ""})
    elif _has_extractor(source):
        vid, extra, sub = _ytdlp(source, job)
        vid, meta = _normalize(job, vid)
        meta.update(extra)
        if sub:
            meta["subtitle_file"] = str(sub)
    else:
        # Unknown URL — ask yt-dlp's generic extractor (it handles lots more
        # than the fast list)
        try:
            vid, extra, sub = _ytdlp(source, job)
            vid, meta = _normalize(job, vid)
            meta.update(extra)
            if sub:
                meta["subtitle_file"] = str(sub)
        except subprocess.CalledProcessError as e:
            # Generic extractor failed; last ditch is direct HTTPS GET
            ext = Path(p.path).suffix.lower() or ".mp4"
            dest = d / ("download" + ext)
            if not dest.exists():
                _dl(source, dest)
            vid, meta = _normalize(job, dest)
            meta.update({"kind_hint": "direct-fallback", "title": Path(p.path).stem,
                         "description": "", "uploader": "", "_ytdlp_error": str(e)})

    _validate(job, meta)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    return vid, meta
