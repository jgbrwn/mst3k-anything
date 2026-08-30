"""Stage 3b: build per-gap context bundles — what the writer actually needs
to write a riff that *makes sense at that exact moment*.

Each bundle:
- 3 frames: T-3s, T (mid-gap), T+3s  -> what the shot looks like around the beat
- transcript window: lines within ±8s of the gap -> what was just said / what's next
- nearest hot moment: if an audio spike is near -> "there's a dramatic beat here"
- prev_riff: the last riff placed (for callbacks / not-repeating-yourself)
"""
import json
import subprocess
from pathlib import Path

from . import transcribe


def grab_context_frames(job: dict, gaps: list[dict]) -> None:
    """For each gap, capture T-3, T, T+3 frames."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    dur = job["meta"]["duration"]
    for g in gaps:
        mid = (g["start"] + g["end"]) / 2
        for tag, t in (("pre", mid - 3.0), ("mid", mid), ("post", mid + 3.0)):
            t = max(0.0, min(t, dur - 0.1))
            f = frames / f"gap{g['id']:03d}_{tag}.png"
            if f.exists():
                continue
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(f)],
                           check=False)


def build_bundles(job: dict, gaps: list[dict], transcript: dict,
                  hot_moments: list[float]) -> list[dict]:
    """Assemble the full context bundle list passed to the writer."""
    frames = job["dir"] / "frames"
    lines = transcript.get("lines", [])
    out = []
    for g in gaps:
        mid = (g["start"] + g["end"]) / 2
        ctx = transcribe.context_at(lines, mid, radius=8.0)
        # nearest hot moment within 10s
        hot = None
        for h in hot_moments:
            if abs(h - mid) <= 10.0 and (hot is None or abs(h - mid) < abs(hot - mid)):
                hot = h
        out.append({
            "gap": g,
            "frames": {tag: str(frames / f"gap{g['id']:03d}_{tag}.png")
                       for tag in ("pre", "mid", "post")},
            "transcript_before": ctx["before"],
            "transcript_over": ctx["overlapping"],
            "transcript_after": ctx["after"],
            "hot_moment": hot,
        })
    return out
