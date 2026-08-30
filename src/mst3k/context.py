"""Stage 3b: build per-gap context bundles — what the writer actually needs
to write a riff that *makes sense at that exact moment*.

Each bundle:
- frames at T-3 / T / T+3, OR (when a hot moment is adjacent) T-3 / T / hot_frame
  -> what the shot looks like around the beat + the high-energy payoff if any
- transcript window: lines within ±8s of the gap -> what was just said / what's next
- nearest hot moment: if an audio spike is near -> "there's a dramatic beat here"
- prev_riff: the last riff placed (for callbacks / not-repeating-yourself)
"""
import json
import subprocess
from pathlib import Path

from . import transcribe


def grab_context_frames(job: dict, gaps: list[dict],
                        hot_moments: list[float] | None = None) -> None:
    """For each gap, capture T-3 / T / T+3 frames.
    When a hot moment is closer than T+3 to this gap's mid, also capture
    that hot frame in place of T+3 (more contextually informative than a blur)."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    dur = job["meta"]["duration"]
    hot_moments = hot_moments or []

    def nearest_hot(mid: float, radius: float = 6.0):
        best = None
        for h in hot_moments:
            d = abs(h - mid)
            if d <= radius and (best is None or d < abs(best - mid)):
                best = h
        return best

    for g in gaps:
        mid = (g["start"] + g["end"]) / 2
        post_t = mid + 3.0
        # prefer hot-moment frame over plain post when one is close
        hot = nearest_hot(mid)
        post_tag = "post"
        if hot is not None and abs(hot - mid) < 3.0:
            post_t = hot
            post_tag = "hot"
        for tag, t in (("pre", mid - 3.0), ("mid", mid), (post_tag, post_t)):
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
        hot = None
        for h in hot_moments:
            if abs(h - mid) <= 10.0 and (hot is None or abs(h - mid) < abs(hot - mid)):
                hot = h
        post_tag = "post"
        if hot is not None and abs(hot - mid) < 3.0:
            post_tag = "hot"
        frames_map = {tag: str(frames / f"gap{g['id']:03d}_{tag}.png")
                      for tag in ("pre", "mid", post_tag)}
        frames_map = {k: v for k, v in frames_map.items() if Path(v).exists()}
        out.append({
            "gap": g,
            "frames": frames_map,
            "transcript_before": ctx["before"],
            "transcript_over": ctx["overlapping"],
            "transcript_after": ctx["after"],
            "hot_moment": hot,
        })
    return out
