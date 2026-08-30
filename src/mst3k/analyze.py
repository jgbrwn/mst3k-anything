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
    """Riff windows: silence when plentiful, else quiet/lull moments (never
    over active dialogue). Density controller compensates when a video talks
    too much to leave pauses."""
    cache = job["dir"] / "gaps.json"
    if cache.exists():
        return json.loads(cache.read_text())
    audio = extract_audio(job)
    duration = job["meta"]["duration"]

    silence = _detect_silence(audio, job["min_gap"])
    sil_gaps = [g for g in (_mk_gap(job, s, e) for s, e in silence) if g]

    silence_ratio = sum(e - s for s, e in silence) / max(duration, 1e-6)
    enough = len(sil_gaps) >= job["target_riff_count"] * job["min_silence_frac"]

    if silence_ratio >= job["silence_ratio_ok"] and enough:
        gaps = sil_gaps
    else:
        moment_gaps = _detect_quiet_moments(audio, job)
        anchors = sil_gaps + [g for g in moment_gaps
                              if not _overlaps(sil_gaps, g["start"], g["end"], 2.0)]
        anchors.sort(key=lambda g: g["start"])
        gap_sec = max(duration / max(job["target_riff_count"], 1),
                      job["min_riff_space_sec"])
        gaps = _spread(anchors, gap_sec, job["target_riff_count"])

    for i, g in enumerate(gaps, 1):
        g["id"] = i
    if len(gaps) > job["max_riffs"]:
        step = len(gaps) / job["max_riffs"]
        gaps = [gaps[int(i * step)] for i in range(job["max_riffs"])]
    cache.write_text(json.dumps(gaps, indent=2))
    return gaps


def _mk_gap(job, start, end):
    dur = end - start
    if dur < job["min_gap"] or start < 0.5:
        return None
    usable = max(0.4, min(dur - 2 * job["margin"], job["max_riff_seconds"]))
    return {"id": 0, "start": round(start, 3), "end": round(end, 3),
            "dur": round(dur, 3), "usable": round(usable, 3), "kind": "silence",
            "at": "gap_start",
            "budget_words": max(2, int(usable * job["words_per_second"]))}


def _detect_silence(audio: Path, min_gap: float) -> list[tuple]:
    proc = subprocess.run(["ffmpeg", "-i", str(audio), "-af",
                           "silencedetect=noise=-35dB:d=1.0", "-f", "null", "-"],
                          capture_output=True, text=True)
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: ([-\d.]+)", line)
        if m: starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", line)
        if m: ends.append((float(m.group(1)), float(m.group(2))))
    out = []
    for i, (end, dur) in enumerate(ends):
        s = starts[i] if i < len(starts) else end - dur
        if dur >= min_gap:
            out.append((s, end))
    return out


def _detect_quiet_moments(audio: Path, job: dict) -> list[dict]:
    """Quiet/lull windows via speech gate + astats low-RMS ranking. Windows that
    survive the gate (not active dialogue) can be riffed over (ducking applies);
    active dialogue cannot."""
    duration = job["meta"]["duration"]
    win, hop = job["moment_win_sec"], job["moment_hop_sec"]
    scored = []
    for start in _frange(0.5, max(0.5, duration - win), hop):
        end = min(start + win, duration)
        aseg = _seg(job, audio, start, end)
        if aseg is None:
            continue
        if _is_speech(aseg, job["speech_noise_db"], job["speech_dur"]):
            continue  # active dialogue — skip
        mean_db = _rms_db(aseg)
        if mean_db is None:
            continue
        scored.append((mean_db, start, end))
    scored.sort(key=lambda x: x[0])
    out = []
    for _db, start, end in scored:
        usable = min(end - start, job["max_riff_seconds"])
        out.append({"id": 0, "start": round(start, 1), "end": round(end, 1),
                    "dur": round(end - start, 1), "usable": round(usable, 1),
                    "kind": "moment", "at": "mid",
                    "budget_words": max(2, int(usable * job["words_per_second"]))})
    return out


def _seg(job, audio: Path, start: float, end: float) -> Path | None:
    """Cached mono segment of the source audio for scoring."""
    cache = job["dir"] / "segs" / f"{start:.1f}.wav"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t",
                        str(end - start), "-i", str(audio), "-ac", "1", "-ar", "8000",
                        str(cache)], check=True)
    return cache


def _is_speech(seg: Path, noise_db: float, d: float) -> bool:
    proc = subprocess.run(["ffmpeg", "-i", str(seg), "-af",
                           f"silencedetect=noise={noise_db}:d={d}", "-f", "null", "-"],
                          capture_output=True, text=True)
    return "silence_start" not in proc.stderr  # sound above gate => speech


def _rms_db(seg: Path) -> float | None:
    proc = subprocess.run(["ffmpeg", "-i", str(seg), "-af", "astats=metadata=0",
                           "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"RMS level dB:\s*(-?\d+\.\d+)", proc.stderr)
    return float(m.group(1)) if m else None


def _overlaps(sil_gaps, start, end, pad):
    return any(not (end < g["start"] - pad or start > g["end"] + pad)
               for g in sil_gaps)


def _spread(anchors, gap_sec, target):
    """Keep highest-priority anchors (silence first), spaced ≥ gap_sec, padded to target."""
    if not anchors:
        return []
    picked = [anchors[0]]
    last = anchors[0]["start"]
    for a in anchors[1:]:
        if a["start"] - last >= gap_sec:
            picked.append(a)
            last = a["start"]
    if len(picked) >= target:
        return picked[:target]
    # fill from remaining anchors between fixed anchors
    remaining = [a for a in anchors if a not in picked]
    for a in remaining:
        inserted = False
        for i, b in enumerate(picked + [None]):
            lo = b["start"] if b else float("inf")
            prev_start = picked[i - 1]["start"] if i > 0 else -1
            if prev_start < a["start"] < lo:
                picked.append(a)
                picked.sort(key=lambda g: g["start"])
                inserted = True
                break
        if inserted and len(picked) >= target:
            break
    return picked


def _frange(start, stop, step):
    x = start
    while x <= stop:
        yield x
        x += step


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
