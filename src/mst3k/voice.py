"""Stage 5a: synthesize riffs with Pocket TTS and color per character.

Ensemble v1: config.voices defines the pool; each riff is routed to a voice
deterministically so re-renders stay consistent. Expressiveness comes from the
writer's hint marks (*word*, trailing …/!/?) — voice.py translates those into
ffmpeg timing/tone tweaks for color.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path


def _cache_key(line: str, voice_name: str) -> str:
    blob = json.dumps({"t": line, "v": voice_name}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def pick_voice(job: dict, riff: dict) -> dict:
    """Deterministically assign a pool voice to a riff based on (gap,line)."""
    pool = job.get("voices") or [{"name": None, "pitch": job["voice_pitch"],
                                  "rate": job["voice_rate"], "weight": 1.0}]
    weights = [float(v.get("weight", 1.0)) for v in pool]
    total = sum(weights)
    h = int(hashlib.sha256(f"{riff['gap']}:{riff['line']}".encode()).hexdigest(), 16)
    pick = (h % 100000) / 100000.0 * total
    cume = 0.0
    for v, w in zip(pool, weights):
        cume += w
        if pick <= cume:
            return v
    return pool[-1]


def synthesize(job: dict, riff: dict) -> dict:
    """Render one riff to wav. Returns {path, duration, tempo, ok} or None if dropped.
    Voice is picked deterministically per riff; expressiveness hints in the line
    (*word* / trailing …/!/?) get baked into ffmpeg tone tweaks."""
    voice = pick_voice(job, riff)
    vname = voice["name"] or ""
    cache = job["dir"] / "tts"
    cache.mkdir(exist_ok=True)
    key = _cache_key(riff["line"], vname)
    wav = cache / f"{key}.wav"
    if not wav.exists():
        cmd = [job["pocket_tts"], "generate", "-q", "--text", riff["line"],
               "--output-path", str(wav)]
        if vname:
            cmd += ["--voice", vname]
        if job["voice_ref"]:
            cmd += ["--voice", job["voice_ref"]]
        subprocess.run(cmd, check=True, capture_output=True)

    dur = probe_duration(wav)
    if dur <= 0:
        return None
    gap = riff["_gap"]
    budget = gap["usable"]
    tempo = 1.0
    if dur > budget:
        needed = dur / budget
        if needed > job["max_tempo_stretch"]:
            return None  # can't fit without chipmunking — drop it
        tempo = needed

    af = []
    # base voice coloring (pitch per character)
    pitch = float(voice.get("pitch", 0.0))
    if abs(pitch) > 0.05:
        st = 2 ** (pitch / 12)
        af.append(f"asetrate=24000*{st},aresample=24000")
    if tempo != 1.0:
        af.append(f"atempo={tempo:.4f}")
    # expressiveness hints from the writer's markup
    af.extend(hint_filters(riff["line"]))

    if af:
        out = cache / f"{key}_final.wav"
        if not out.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                            "-af", ",".join(af), str(out)], check=True)
        wav = out
        dur = probe_duration(wav)
    return {"path": wav, "duration": dur, "tempo": tempo, "voice": vname,
            "ok": dur <= budget + 0.05}


def hint_filters(line: str) -> list:
    """Wild-card emphasis from the writers' shorthand:
    *word* → slow slightly + drop pitch (a spoonerized delivery)
    trailing … → slow the final fifth
    trailing ! or ? → lift the final third
    """
    af = []
    if re.search(r"\*[^* \n][^*]*\*", line):
        af.extend(["atempo=1.05", "asetrate=24000*0.97,aresample=24000"])
    if line.rstrip().endswith("...") or line.rstrip().endswith("…"):
        af.append("atempo=0.97")
    if line.rstrip().endswith(("!", "?")):
        af.append("asetrate=24000*1.05,aresample=24000")
    return af


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0