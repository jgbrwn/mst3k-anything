#!/usr/bin/env python3
"""mst3k-anything writer stage: gaps+frames -> riffs via OpenAI-compliant chat completions."""
import base64, json, os, sys, urllib.request

API_URL = "https://hyper.charm.land/v1/chat/completions"
KEY = open("/tmp/hyper_api_key").read().strip()
MODEL = "qwen3.8-flash"

SYSTEM = """You are THE RIFFER, an original dry theater commentator writing spoken riffs
for this specific video. Do not imitate named fictional characters, actors, voices,
catchphrases, or source dialogue.

For each cue, use the supplied frame and transcript as evidence. Prefer an exact object,
gesture, caption, edit, expression, contradiction, or phrase over generic heckling. Add
a comic turn: literal reading, comparison, escalation, production critique, mock narration,
or precise button. A callback is welcome when an earlier concrete detail returns.
Write densely: silence is a deliberate exception, not the default. Dialogue overlap is
allowed for a purposeful aside, setup, reaction, reveal, button, or callback; mark it
intentional in `timing`.

Return ONLY one JSON object per offered cue, in order:
[
  {"gap": <n>, "speaker": "riffer", "status": "riff|silence", "line": "...",
   "when": 0.0, "timing": "cue|button|overlap",
   "mechanism": "observation|literalization|comparison|mock_narration|callback|other",
   "evidence": ["frame:mid"], "callback_to": null}
]
No prose, markdown, stage directions, or emojis."""

gaps = [
    {"id": 1, "t": 75.37,  "dur": 1.26, "budget": 3,  "frame": "/tmp/p9/frames/win2_0075.png"},
    {"id": 2, "t": 161.36, "dur": 4.40, "budget": 11, "frame": "/tmp/p9/frames/win4_0161.png"},
    {"id": 3, "t": 176.39, "dur": 1.45, "budget": 4,  "frame": "/tmp/p9/frames/win5_0176.png"},
]

def b64(path):
    return base64.b64encode(open(path, "rb").read()).decode()

user_parts = []
for g in gaps:
    user_parts.append({"type": "text", "text":
        f"GAP {g['id']} at {g['t']}s, {g['dur']}s long, budget {g['budget']} words. Frame:"
    })
    user_parts.append({"type": "image_url", "image_url":
        {"url": f"data:image/png;base64,{b64(g['frame'])}"}})

body = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_parts},
    ],
    "temperature": 0.9,
    "max_tokens": 500,
}
req = urllib.request.Request(
    API_URL,
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
)
resp = json.load(urllib.request.urlopen(req, timeout=120))
content = resp["choices"][0]["message"]["content"]
print(content)
# parse defensively
s = content.strip()
if s.startswith("```"):
    s = s.split("```", 2)[1]
    if s.startswith("json"): s = s[4:]
    s = s.rsplit("```", 1)[0]
riffs = json.loads(s)
json.dump(riffs, open("/tmp/p9/riffs/riffs.json", "w"), indent=2)
print("usage:", resp.get("usage"))
