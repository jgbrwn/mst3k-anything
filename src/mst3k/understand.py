"""Stage 3: content profile — what kind of thing is this video, and what's funny."""
import json
from pathlib import Path

from . import llm

PROFILE_SYSTEM = """You are a video analyst for a comedy-riffing pipeline.
You will see metadata (title/description) and ~10 evenly-spaced frames from a video.
Produce a concise profile so a joke-writer can target the right material.

Return ONLY JSON:
{
  "kind": "movie|tv|vlog|tutorial|gaming|music|home|commercial|other",
  "tone": "one phrase describing the vibe",
  "premise": "1-2 sentences: what happens in this video",
  "targets": ["up to 5 concrete things ripe for riffing: editing, props, acting, claims, pacing, graphics..."],
  "visual_gags": ["up to 3 things actually visible in the frames that are inherently funny"],
  "style_guide": "one sentence of riff-writing guidance tuned to this specific video's kind and content"
}

Be specific and concrete. No hedging, no disclaimers."""


def build_profile(job: dict) -> dict:
    cache = job["dir"] / "profile.json"
    if cache.exists():
        return json.loads(cache.read_text())
    meta = job["meta"]
    frames = job["dir"] / "frames"
    ctx = sorted(frames.glob("ctx*.png"))[:10]

    user = [{"type": "text", "text":
             f"TITLE: {meta.get('title') or 'unknown'}\n"
             f"DESCRIPTION: {(meta.get('description') or 'none')[:400]}\n"
             f"DURATION: {meta.get('duration', 0):.0f}s\n\nFrames (evenly sampled):"}]
    for f in ctx:
        user.append({"type": "image_url", "image_url":
                     {"url": f"data:image/png;base64,{llm.b64_image(f)}"}})

    profile = llm.chat_json(job, PROFILE_SYSTEM, user, temperature=0.3, max_tokens=700)
    if not isinstance(profile, dict):
        profile = {"kind": "other", "tone": "unknown", "premise": "",
                   "targets": [], "visual_gags": []}
    cache.write_text(json.dumps(profile, indent=2))
    return profile
