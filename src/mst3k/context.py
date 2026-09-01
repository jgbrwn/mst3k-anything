"""Build per-cue setup/payoff bundles for the writer and judge."""

import json
import subprocess
from pathlib import Path

from . import transcribe
from .cache import file_signature


CONTEXT_FRAME_POLICY_VERSION = 1


def grab_context_frames(job: dict, gaps: list[dict],
                        hot_moments: list[float] | None = None) -> None:
    """Capture pre/mid/post frames for each cue, invalidating stale cue frames."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    hot_moments = hot_moments or []
    marker = job["dir"] / "context_frames_policy.json"
    policy = {
        "version": CONTEXT_FRAME_POLICY_VERSION,
        "source": file_signature(job.get("source", "")),
        "cues": [{"id": g["id"], "anchor": round(float(g.get("anchor", 0)), 3)}
                 for g in gaps],
        "hot": [round(float(h), 2) for h in hot_moments],
    }
    old_policy = None
    if marker.exists():
        try:
            old_policy = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    if old_policy != policy:
        for old_frame in frames.glob("gap*_*.png"):
            old_frame.unlink(missing_ok=True)

    dur = float(job["meta"]["duration"])

    def nearest_hot(mid: float, radius: float = 6.0):
        nearby = [h for h in hot_moments if abs(h - mid) <= radius]
        return min(nearby, key=lambda h: abs(h - mid), default=None)

    for gap in gaps:
        mid = float(gap.get("anchor", (gap["start"] + gap["end"]) / 2))
        post_t = mid + 3.0
        hot = nearest_hot(mid)
        post_tag = "post"
        if hot is not None and abs(hot - mid) < 3.0:
            post_t = hot
            post_tag = "hot"
        for tag, timestamp in (("pre", mid - 3.0), ("mid", mid),
                               (post_tag, post_t)):
            timestamp = max(0.0, min(timestamp, max(0.0, dur - 0.1)))
            frame = frames / f"gap{gap['id']:03d}_{tag}.png"
            if frame.exists():
                continue
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{timestamp:.2f}",
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(frame)],
                           check=False)
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2))
    tmp.replace(marker)


def build_bundles(job: dict, gaps: list[dict], transcript: dict,
                  hot_moments: list[float]) -> list[dict]:
    """Assemble local evidence plus a generous transcript setup/payoff window."""
    frames = job["dir"] / "frames"
    lines = transcript.get("lines", [])
    radius = float(job.get("context_radius_sec", 18.0))
    hot_moments = hot_moments or []
    out = []
    for gap in gaps:
        mid = float(gap.get("anchor", (gap["start"] + gap["end"]) / 2))
        ctx = transcribe.context_at(lines, mid, radius=radius)
        hot = min((h for h in hot_moments if abs(h - mid) <= 10.0),
                  key=lambda h: abs(h - mid), default=None)
        post_tag = "hot" if hot is not None and abs(hot - mid) < 3.0 else "post"
        post_t = hot if post_tag == "hot" else mid + 3.0
        frame_paths = {tag: str(frames / f"gap{gap['id']:03d}_{tag}.png")
                       for tag in ("pre", "mid", post_tag)}
        frame_paths = {key: value for key, value in frame_paths.items()
                       if Path(value).exists()}
        frame_times = {"pre": round(max(0.0, mid - 3.0), 2),
                       "mid": round(mid, 2), post_tag: round(post_t, 2)}
        out.append({
            "gap": gap,
            "frames": frame_paths,
            "frame_times": {key: frame_times[key] for key in frame_paths},
            "transcript_before": ctx["before"],
            "transcript_over": ctx["overlapping"],
            "transcript_after": ctx["after"],
            "hot_moment": hot,
            "context_radius": radius,
            "candidate_evidence": [f"frame:{tag}" for tag in frame_paths],
        })
    return out
