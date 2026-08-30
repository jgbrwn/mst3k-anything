"""Stage 5a: synthesize riffs with Pocket TTS and color per character.

Solo v1 ships one riffer voice. Voice identity comes from (a) an optional cloned
public-domain reference wav and (b) post-hoc ffmpeg pitch/tempo coloring — the
movie-sign recipe. Overrunning riffs are stretched slightly, then dropped.
"""
import hashlib
import json
import subprocess
from pathlib import Path


def _cache_key(text: str, voice: str, rate: float) -> str:
    blob = json.dumps({"t": text, "v": voice, "r": rate}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def synthesize(job: dict, riff: dict) -> dict:
    """Render one riff to wav. Returns {path, duration, tempo, ok} or None if dropped."""
    cache = job["dir"] / "tts"
    cache.mkdir(exist_ok=True)
    key = _cache_key(riff["line"], job["voice_ref"] or "", job["voice_rate"])
    wav = cache / f"{key}.wav"
    if not wav.exists():
        cmd = [job["pocket_tts"], "generate", "-q", "--text", riff["line"],
               "--output-path", str(wav)]
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

    if tempo != 1.0 or job["voice_pitch"] != 0.0:
        colored = cache / f"{key}_final.wav"
        if not colored.exists():
            af = []
            if tempo != 1.0:
                af.append(f"atempo={tempo:.4f}")
            if job["voice_pitch"] != 0.0:
                st = 2 ** (job["voice_pitch"] / 12)
                af.append(f"atempo={st:.4f}")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                            "-af", ",".join(af), str(colored)], check=True)
        wav = colored
        dur = probe_duration(wav)
    return {"path": wav, "duration": dur, "tempo": tempo, "ok": dur <= budget + 0.05}


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0
