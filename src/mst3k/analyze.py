"""Stage 2: decompose — silence gaps, scene cuts, keyframes, transcript."""
import json
import re
import subprocess
from pathlib import Path


def extract_audio(job: dict) -> Path:
    out = job["dir"] / "audio.wav"
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(job["source"]),
                        "-vn", "-ac", "1", "-ar", "16000", str(out)], check=True)
    return out


def find_gaps(job: dict) -> list[dict]:
    """Silence windows with riff budgets (movie-sign recipe)."""
    cache = job["dir"] / "gaps.json"
    if cache.exists():
        return json.loads(cache.read_text())
    audio = extract_audio(job)
    proc = subprocess.run(["ffmpeg", "-i", str(audio), "-af",
                           "silencedetect=noise=-30dB:d=1.2", "-f", "null", "-"],
                          capture_output=True, text=True)
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: ([-\d.]+)", line)
        if m: starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", line)
        if m: ends.append((float(m.group(1)), float(m.group(2))))
    gaps = []
    for i, (end, dur) in enumerate(ends):
        start = starts[i] if i < len(starts) else end - dur
        if dur < job["min_gap"] or start < 0.5:
            continue
        usable = max(0.4, dur - 2 * job["margin"])
        usable = min(usable, job["max_riff_seconds"])
        gaps.append({"id": len(gaps) + 1, "start": round(start, 3),
                     "end": round(end, 3), "dur": round(dur, 3),
                     "usable": round(usable, 3),
                     "budget_words": max(2, int(usable * job["words_per_second"]))})
    # spread across the runtime if there are too many
    if len(gaps) > job["max_riffs"]:
        step = len(gaps) / job["max_riffs"]
        gaps = [gaps[int(i * step)] for i in range(job["max_riffs"])]
    cache.write_text(json.dumps(gaps, indent=2))
    return gaps


def find_cuts(job: dict, max_cuts: int = 400) -> list[float]:
    """Scene-change timestamps (v1.1 anchors), cached."""
    cache = job["dir"] / "cuts.json"
    if cache.exists():
        return json.loads(cache.read_text())
    proc = subprocess.run(["ffmpeg", "-i", str(job["source"]), "-vf",
                           "select='gt(scene,0.4)',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True)
    cuts = []
    for line in proc.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            cuts.append(round(float(m.group(1)), 3))
            if len(cuts) >= max_cuts:
                break
    cache.write_text(json.dumps(cuts))
    return cuts


def grab_frames(job: dict, gaps: list[dict]) -> None:
    """One keyframe at the middle of each gap + ~10 context frames for understand."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    dur = job["meta"]["duration"]
    for g in gaps:
        t = (g["start"] + g["end"]) / 2
        f = frames / f"gap{g['id']:03d}.png"
        if not f.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(f)])
    # context frames spread across runtime (for the understand stage)
    n = 10
    for i in range(n):
        t = dur * (i + 0.5) / n
        f = frames / f"ctx{i:02d}.png"
        if not f.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(f)])
