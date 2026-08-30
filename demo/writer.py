#!/usr/bin/env python3
"""mst3k-anything writer stage: gaps+frames -> riffs via OpenAI-compliant chat completions."""
import base64, json, os, sys, urllib.request

API_URL = "https://hyper.charm.land/v1/chat/completions"
KEY = open("/tmp/hyper_api_key").read().strip()
MODEL = "qwen3.8-flash"

SYSTEM = """You are the writers' room for an MST3K-style riff track.
VOICES:
- host: dry, weary, deadpan human. Flat observations, setup lines.
- crow: sardonic, fast. Attacks production values and continuity. Cruel, enjoying it.
- servo: theatrical, overplays everything. Speaks AS characters, narrates.
RULES:
1. Never exceed the word budget; talking over movie dialogue is the unforgivable sin.
2. Riff about what is VISIBLE in the frame for that gap.
3. Rotate speakers; no two adjacent riffs share a speaker.
4. Punch up at the film's mistakes, never at real people.
Return ONLY a JSON array of objects: {"gap": <n>, "speaker": "host|crow|servo", "line": "..."}"""

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
