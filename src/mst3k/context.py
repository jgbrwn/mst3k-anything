"""Build per-cue setup/payoff bundles for the writer and judge.

Each bundle contains:
- pre/mid frames timestamped at or before the cue;
- transcript setup and overlap context through the cue;
- a continuity/profile reference for earlier callbacks.
Future frames and post-cue transcript are retained only as internal audit data, not shown
as writing evidence.
"""

import json
import subprocess
from pathlib import Path

from . import config
from . import transcribe
from .cache import file_signature


CONTEXT_FRAME_POLICY_VERSION = 2


def grab_context_frames(job: dict, gaps: list[dict],
                        hot_moments: list[float] | None = None) -> None:
    """Capture pre/mid frames for each cue, invalidating stale cue frames."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    hot_moments = hot_moments or []
    marker = job["dir"] / "context_frames_policy.json"
    policy = {
        "version": CONTEXT_FRAME_POLICY_VERSION,
        "source": file_signature(job.get("source", "")),
        "cues": [{"id": gap["id"],
                  "anchor": round(float(gap.get("anchor", 0)), 3)}
                 for gap in gaps],
        "hot": [round(float(hot), 2) for hot in hot_moments],
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

    duration = float(job["meta"]["duration"])
    for gap in gaps:
        mid = float(gap.get("anchor", (gap["start"] + gap["end"]) / 2))
        # Only show what an audience member could have seen by the anchor.
        for tag, timestamp in (("pre", mid - 2.5), ("mid", mid)):
            timestamp = max(0.0, min(timestamp, max(0.0, duration - 0.1)))
            frame = frames / f"gap{gap['id']:03d}_{tag}.png"
            if frame.exists():
                continue
            subprocess.run([config.tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{timestamp:.2f}",
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(frame)],
                           check=False)
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2))
    tmp.replace(marker)


def build_bundles(job: dict, gaps: list[dict], transcript: dict,
                  hot_moments: list[float]) -> list[dict]:
    """Assemble local evidence plus transcript context through each cue."""
    frames = job["dir"] / "frames"
    lines = transcript.get("lines", [])
    radius = float(job.get("context_radius_sec", 18.0))
    hot_moments = hot_moments or []
    out = []
    for gap in gaps:
        mid = float(gap.get("anchor", (gap["start"] + gap["end"]) / 2))
        ctx = transcribe.context_at(lines, mid, radius=radius)
        hot = min((timestamp for timestamp in hot_moments
                   if 0.0 <= timestamp - mid <= 0.25),
                  key=lambda timestamp: abs(timestamp - mid), default=None)
        frame_paths = {tag: str(frames / f"gap{gap['id']:03d}_{tag}.png")
                       for tag in ("pre", "mid")}
        frame_paths = {key: value for key, value in frame_paths.items()
                       if Path(value).exists()}
        frame_times = {"pre": round(max(0.0, mid - 2.5), 2),
                       "mid": round(mid, 2)}
        out.append({
            "gap": gap,
            "frames": frame_paths,
            "frame_times": {key: frame_times[key] for key in frame_paths},
            "transcript_before": ctx["before"],
            "transcript_over": ctx["overlapping"],
            # Kept for internal diagnostics; writers/judges do not receive it.
            "transcript_after": ctx["after"],
            "hot_moment": hot,
            "context_radius": radius,
            "candidate_evidence": [f"frame:{tag}" for tag in frame_paths],
        })
    return out
