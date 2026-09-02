"""Stage 5a: synthesize riffs with Pocket TTS and color per character.

Ensemble v1: config.voices defines the pool; each riff is routed to a voice
deterministically so re-renders stay consistent. Expressiveness comes from the
writer's hint marks (*word*, trailing …/!/?) — voice.py translates those into
ffmpeg timing/tone tweaks for color.
"""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from .cache import file_signature, value_digest

VOICE_CACHE_VERSION = 1


def _voice_reference(job: dict):
    """Resolve the configured local/remote conditioning source."""
    raw = str(job.get("voice_ref") or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"VOICE_REF does not exist or is not a file: {path}")
    return path


def validate_voice_reference(job: dict) -> None:
    """Fail before media work when a configured local reference is missing."""
    _voice_reference(job)


def prepare_voice_reference(source: str | Path, output: str | Path,
                            pocket_tts: str | Path, *, force: bool = False) -> Path:
    """Convert a voice sample to PocketTTS state, once, for fast reuse."""
    source_text = str(source)
    if "://" not in source_text:
        source_path = Path(source_text).expanduser()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError(f"custom voice source does not exist or is not a file: {source_path}")
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if source_text.lower().split("?", 1)[0].endswith(".safetensors"):
        source_path = Path(source_text).expanduser()
        if not source_path.exists():
            raise RuntimeError(f"custom voice state does not exist: {source_path}")
        if source_path.resolve() != output.resolve():
            shutil.copy2(source_path, output)
            return output
        return source_path
    if output.exists() and output.stat().st_size > 0 and not force:
        return output
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        subprocess.run([str(pocket_tts), "export-voice", "-q", source_text,
                        str(tmp)], check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "PocketTTS could not prepare VOICE_REF. Accept the PocketTTS model "
            "conditions on Hugging Face and run `hf auth login`, then retry."
        ) from exc
    if not tmp.exists() or tmp.stat().st_size <= 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("PocketTTS produced an empty custom voice state")
    tmp.replace(output)
    return output


def _prepared_voice(job: dict):
    source = _voice_reference(job)
    if source is None:
        return None
    source_text = str(source)
    if source_text.lower().split("?", 1)[0].endswith(".safetensors"):
        return Path(source_text).expanduser() if "://" not in source_text else source_text
    signature = file_signature(source) if isinstance(source, Path) else None
    key = value_digest({"version": VOICE_CACHE_VERSION, "source": source_text,
                        "signature": signature})[:20]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(source_text).stem).strip("-") or "voice"
    cache_dir = Path(job.get("voice_cache_dir") or
                     (Path.home() / ".cache" / "mst3k-anything" / "voices"))
    return prepare_voice_reference(source, cache_dir / f"{stem}-{key}.safetensors",
                                  job["pocket_tts"])


def prepare_configured_voice(job: dict):
    """Validate and precompute the configured custom voice before media work."""
    return _prepared_voice(job)


def _cache_key(line: str, voice_name: str, voice: dict | None = None,
               job: dict | None = None) -> str:
    voice = voice or {}
    job = job or {}
    blob = {
        "version": 3,
        "text": line,
        "voice": voice_name,
        "pitch": float(voice.get("pitch", job.get("voice_pitch", 0.0)) or 0.0),
        "rate": float(voice.get("rate", job.get("voice_rate", 1.0)) or 1.0),
        "voice_ref": _voice_ref_cache_value(job),
        "hints": hint_filters(line),
    }
    return value_digest(blob)[:20]


def _voice_ref_cache_value(job: dict):
    raw = str(job.get("voice_ref") or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return {"value": raw}
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return {"value": raw, "file": file_signature(path)}


def pick_voice(job: dict, riff: dict) -> dict:
    """Deterministically assign a pool voice, or use one custom reference voice."""
    if job.get("voice_ref"):
        return {"name": "custom", "pitch": float(job.get("voice_pitch", 0.0) or 0.0),
                "rate": float(job.get("voice_rate", 1.0) or 1.0), "weight": 1.0}
    pool = job.get("voices") or [{"name": None, "pitch": 0.0,
                                  "rate": 1.0, "weight": 1.0}]
    weights = [float(v.get("weight", 1.0)) for v in pool]
    total = sum(weights)
    h = int(hashlib.sha256(f"{riff['gap']}:{riff['line']}".encode()).hexdigest(), 16)
    pick = (h % 100000) / 100000.0 * total
    cume = 0.0
    for v, w in zip(pool, weights):
        cume += w
        if pick <= cume:
            selected = dict(v)
            selected["pitch"] = float(selected.get("pitch", 0.0) or 0.0) + float(job.get("voice_pitch", 0.0) or 0.0)
            selected["rate"] = float(selected.get("rate", 1.0) or 1.0) * float(job.get("voice_rate", 1.0) or 1.0)
            return selected
    selected = dict(pool[-1])
    selected["pitch"] = float(selected.get("pitch", 0.0) or 0.0) + float(job.get("voice_pitch", 0.0) or 0.0)
    selected["rate"] = float(selected.get("rate", 1.0) or 1.0) * float(job.get("voice_rate", 1.0) or 1.0)
    return selected


def synthesize(job: dict, riff: dict) -> dict:
    """Render one riff to wav. Returns {path, duration, tempo, ok} or None if dropped.
    Voice is picked deterministically per riff; expressiveness hints in the line
    (*word* / trailing …/!/?) get baked into ffmpeg tone tweaks. A long line is
    returned as an intentional overlap rather than rejected for timing."""
    voice = pick_voice(job, riff)
    vname = voice["name"] or ""
    cache = job["dir"] / "tts"
    cache.mkdir(exist_ok=True)
    key = _cache_key(riff["line"], vname, voice, job)
    wav = cache / f"{key}.wav"
    if wav.exists() and probe_duration(wav) <= 0:
        wav.unlink(missing_ok=True)
    if not wav.exists():
        conditioned_voice = _prepared_voice(job)
        tmp = cache / f"{key}.tmp.wav"
        tmp.unlink(missing_ok=True)
        cmd = [job["pocket_tts"], "generate", "-q", "--text", riff["line"],
               "--output-path", str(tmp)]
        if conditioned_voice is not None:
            cmd += ["--voice", str(conditioned_voice)]
        elif vname:
            cmd += ["--voice", vname]
        subprocess.run(cmd, check=True, capture_output=True)
        if probe_duration(tmp) <= 0:
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(wav)

    dur = probe_duration(wav)
    if dur <= 0:
        return None
    preferred = max(0.8, float(riff["_gap"].get("usable", job["max_riff_seconds"])))
    tempo = 1.0
    if dur > preferred:
        needed = dur / preferred
        # A modest squeeze is useful for a button, but exceeding the preferred
        # envelope is deliberately allowed. Dialogue overlap is a feature, not
        # a failed fit, so never discard a grounded riff for timing alone.
        if needed <= job["max_tempo_stretch"]:
            tempo = needed

    af = []
    # base voice coloring (pitch per character)
    pitch = float(voice.get("pitch", 0.0))
    if abs(pitch) > 0.05:
        st = 2 ** (pitch / 12)
        af.append(f"asetrate=24000*{st},aresample=24000")
    rate = float(voice.get("rate", job.get("voice_rate", 1.0)))
    if abs(rate - 1.0) > 0.01:
        af.append(f"atempo={max(0.5, min(2.0, rate)):.4f}")
    if tempo != 1.0:
        # 1.1x headroom callout: stretch slightly under target so silent holder
        # doesn't swallow the tail; atempo chain hits <2.0 max via loop
        while tempo > 2.0:
            af.append("atempo=2.0")
            tempo /= 2.0
        af.append(f"atempo={max(0.5, tempo):.4f}")
    # expressiveness hints from the writer's markup
    af.extend(hint_filters(riff["line"]))

    if af:
        out = cache / f"{key}_final.wav"
        if out.exists() and probe_duration(out) <= 0:
            out.unlink(missing_ok=True)
        if not out.exists():
            tmp = cache / f"{key}_final.tmp.wav"
            tmp.unlink(missing_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                            "-af", ",".join(af), str(tmp)], check=True)
            if probe_duration(tmp) <= 0:
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
        wav = out
        dur = probe_duration(wav)
        if dur <= 0:
            return None
    return {"path": wav, "duration": dur, "tempo": tempo, "voice": vname,
            "ok": True, "preferred_duration": preferred,
            "overlaps_dialogue": dur > preferred + 0.05}


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