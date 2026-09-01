"""Stage 2b: transcribe audio via sherpa-onnx Parakeet CTC 110M INT8.

Runs in the asr-venv (has sherpa-onnx + numpy). Emits transcript.json:
{"lines": [{"start": s, "end": e, "text": "..."}], "words": [...]}
"""
import json
from pathlib import Path
import signal
import subprocess
import wave

ASR_VENV = Path(__file__).resolve().parents[2] / "asr-venv"
MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "parakeet-ctc"
ASR_CHUNK_SECONDS = 60
TRANSCRIPT_POLICY_VERSION = 2

_RUNNER = r'''
import json, os, sys, wave
import sherpa_onnx
import numpy as np

audio, out, start_sec, end_sec = sys.argv[1:5]
start_sec, end_sec = float(start_sec), float(end_sec)
with wave.open(audio, "rb") as wf:
    sample_rate = wf.getframerate()
    if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
        raise RuntimeError("ASR audio must be mono 16-bit PCM")
    start_frame = max(0, int(round(start_sec * sample_rate)))
    end_frame = min(wf.getnframes(), int(round(end_sec * sample_rate)))
    wf.setpos(start_frame)
    samples = np.frombuffer(wf.readframes(max(0, end_frame - start_frame)),
                            dtype=np.int16).astype(np.float32) / 32768.0

rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
    model="MODEL/model.int8.onnx", tokens="MODEL/tokens.txt",
    num_threads=2, sample_rate=sample_rate, feature_dim=80,
    decoding_method="greedy_search", debug=False)
s = rec.create_stream()
s.accept_waveform(sample_rate, samples)
rec.decode_stream(s)
r = s.result
tokens = list(getattr(r, "tokens", []))
payload = {"text": r.text,
           "tokens": tokens,
           "words": tokens,           # parity: CTC tokens are subword pieces
           "timestamps": list(getattr(r, "timestamps", []))}
tmp = out + ".tmp"
with open(tmp, "w") as fp:
    json.dump(payload, fp)
os.replace(tmp, out)
'''


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _transcript_policy(audio: Path | None) -> dict:
    from .cache import file_signature
    return {"version": TRANSCRIPT_POLICY_VERSION,
            "chunk_seconds": ASR_CHUNK_SECONDS,
            "audio": file_signature(audio) if audio else None}


def _chunk_error(exc: subprocess.CalledProcessError, index: int, total: int) -> RuntimeError:
    if exc.returncode < 0:
        try:
            signal_name = signal.Signals(-exc.returncode).name
        except ValueError:
            signal_name = f"signal {-exc.returncode}"
        if exc.returncode == -signal.SIGKILL:
            reason = "SIGKILL (the ASR worker likely ran out of memory)"
        else:
            reason = signal_name
    else:
        reason = f"exit code {exc.returncode}"
    return RuntimeError(f"ASR chunk {index}/{total} failed with {reason}")


def transcribe(job: dict) -> dict:
    """Return {"lines": [...], "text": ...} with word timestamps.

    Parakeet's offline decoder retains substantially more state when fed a long
    recording at once. Process bounded 60-second chunks in separate workers so
    a long submission cannot grow one decoder to host-OOM size. Chunk results
    are cached individually, making a retry resume after an interrupted chunk.
    """
    cache = job["dir"] / "transcript.json"
    from . import analyze
    has_audio = job.get("meta", {}).get("has_audio", True)
    audio = analyze.extract_audio(job) if has_audio else None
    marker = job["dir"] / "transcript_policy.json"
    policy = _transcript_policy(audio)
    cache_valid = False
    if cache.exists() and marker.exists():
        try:
            cache_valid = json.loads(marker.read_text()) == policy
            if cache_valid:
                return json.loads(cache.read_text())
        except (OSError, json.JSONDecodeError):
            cache_valid = False
    chunk_dir = job["dir"] / "asr_chunks"
    if chunk_dir.exists() and not cache_valid:
        try:
            old_policy = json.loads(marker.read_text()) if marker.exists() else None
        except (OSError, json.JSONDecodeError):
            old_policy = None
        if old_policy is not None and old_policy != policy:
            # Source/settings changed: never combine chunks from another recording.
            for old_chunk in chunk_dir.glob("*.json"):
                old_chunk.unlink(missing_ok=True)
    if not has_audio:
        out = {"text": "", "lines": [], "words": [], "timestamps": []}
        _write_json(job["dir"] / "transcript_raw.json", out)
        _write_json(cache, out)
        _write_json(marker, policy)
        return out
    if audio is None:
        raise RuntimeError("ASR audio is unavailable")
    script = _RUNNER.replace("MODEL", str(MODEL_DIR))
    runner = job["dir"] / "_asr_run.py"
    runner.write_text(script)

    with wave.open(str(audio), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise RuntimeError("ASR audio must be mono 16-bit PCM")
        sample_rate = wf.getframerate()
        total_frames = wf.getnframes()
    if sample_rate <= 0 or total_frames <= 0:
        out = {"text": "", "lines": [], "words": [], "timestamps": []}
        _write_json(cache, out)
        _write_json(marker, policy)
        return out
    duration = total_frames / sample_rate
    chunk_seconds = ASR_CHUNK_SECONDS
    total = max(1, (total_frames + int(sample_rate * chunk_seconds) - 1)
                // int(sample_rate * chunk_seconds))
    chunk_dir = job["dir"] / "asr_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    all_tokens, all_timestamps, texts = [], [], []
    for index in range(total):
        start = index * chunk_seconds
        end = min(duration, start + chunk_seconds)
        raw_path = chunk_dir / f"{index:05d}.json"
        if raw_path.exists():
            try:
                raw = json.loads(raw_path.read_text())
            except (OSError, json.JSONDecodeError):
                raw_path.unlink(missing_ok=True)
                raw = None
        else:
            raw = None
        if raw is None:
            print(f"    [transcribe] chunk {index + 1}/{total} "
                  f"({start:.0f}-{end:.0f}s)", flush=True)
            try:
                subprocess.run([
                    str(ASR_VENV / "bin" / "python"), str(runner),
                    str(audio), str(raw_path), str(start), str(end)],
                    check=True)
            except subprocess.CalledProcessError as exc:
                raise _chunk_error(exc, index + 1, total) from exc
            try:
                raw = json.loads(raw_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"ASR chunk {index + 1}/{total} produced invalid output") from exc
        tokens = list(raw.get("tokens") or raw.get("words") or [])
        timestamps = list(raw.get("timestamps") or [])
        all_tokens.extend(tokens)
        all_timestamps.extend(float(t) + start for t in timestamps[:len(tokens)])
        if raw.get("text"):
            texts.append(raw["text"])

    raw = {"text": " ".join(texts), "tokens": all_tokens,
           "words": all_tokens, "timestamps": all_timestamps}
    _write_json(job["dir"] / "transcript_raw.json", raw)
    lines = _group_lines(raw["words"], raw["timestamps"])
    out = {"text": raw["text"], "lines": lines,
           "words": raw["words"], "timestamps": raw["timestamps"]}
    _write_json(cache, out)
    _write_json(marker, policy)
    return out


def _group_lines(tokens: list, timestamps: list, max_gap: float = 0.9,
                 max_dur: float = 7.0) -> list:
    """Group subword tokens into utterance lines by time gaps / duration.\n    Tokens like '▁word' are de-glued into text; subsequent tokens join."""
    lines, cur = [], []
    cur_start = None
    for tok, t in zip(tokens, timestamps):
        if cur and cur_start is not None:
            prev_t = cur[-1][1]
            if (t - prev_t > max_gap) or (t - cur_start > max_dur):
                lines.append({"start": round(cur_start, 3), "end": round(prev_t, 3),
                              "text": _detok([x[0] for x in cur])})
                cur, cur_start = [], None
        if cur_start is None:
            cur_start = float(t)
        cur.append((tok, float(t)))
    if cur:
        lines.append({"start": round(cur_start, 3),
                      "end": round(cur[-1][1], 3),
                      "text": _detok([x[0] for x in cur])})
    return lines


def _detok(tokens: list) -> str:
    """▁ marks word boundary (sentencepiece); glue subwords into words."""
    out = ""
    for t in tokens:
        if t.startswith("▁"):
            out += " " + t[1:]
        else:
            out += t
    return out.strip()


def context_at(lines: list, t: float, radius: float = 8.0) -> dict:
    """Transcript lines within ±radius of time t, marked which window t falls into."""
    before, after, overlay = [], [], []
    for ln in lines:
        if ln["end"] < t - 0.1:
            if ln["start"] >= t - radius:
                before.append(ln)
        elif ln["start"] > t + 0.1:
            if ln["start"] <= t + radius:
                after.append(ln)
        else:
            overlay.append(ln)
    return {"before": before, "overlapping": overlay, "after": after,
            "window_start": round(t - radius, 1), "window_end": round(t + radius, 1)}
