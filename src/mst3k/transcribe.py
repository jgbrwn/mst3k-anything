"""Stage 2b: transcribe audio via sherpa-onnx Parakeet CTC 110M INT8.

Runs in the asr-venv (has sherpa-onnx + numpy). Emits transcript.json:
{"lines": [{"start": s, "end": e, "text": "..."}], "words": [...]}
"""
import json
from pathlib import Path
import subprocess
import sys

ASR_VENV = Path(__file__).resolve().parents[2] / "asr-venv"
MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "parakeet-ctc"

_RUNNER = r'''
import sherpa_onnx, wave, numpy as np, json, sys
audio, out = sys.argv[1], sys.argv[2]
rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
    model="MODEL/model.int8.onnx", tokens="MODEL/tokens.txt",
    num_threads=2, sample_rate=16000, feature_dim=80,
    decoding_method="greedy_search", debug=False)
wf = wave.open(audio, "rb")
samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
s = rec.create_stream()
s.accept_waveform(16000, samples)
rec.decode_stream(s)
r = s.result
tokens = list(getattr(r, "tokens", []))
json.dump({"text": r.text,
           "tokens": tokens,
           "words": tokens,           # parity: CTC tokens are subword pieces
           "timestamps": list(getattr(r, "timestamps", []))},
          open(out, "w"))
'''


def transcribe(job: dict) -> dict:
    """Return {"lines": [...], "text": "..."} with word timestamps."""
    cache = job["dir"] / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from . import analyze
    audio = analyze.extract_audio(job)
    tmp = job["dir"] / "transcript_raw.json"
    script = _RUNNER.replace("MODEL", str(MODEL_DIR))
    runner = job["dir"] / "_asr_run.py"
    runner.write_text(script)
    subprocess.run([str(ASR_VENV / "bin" / "python"), str(runner),
                    str(audio), str(tmp)], check=True)
    raw = json.loads(tmp.read_text())
    lines = _group_lines(raw.get("words", []), raw.get("timestamps", []))
    out = {"text": raw.get("text", ""), "lines": lines,
           "words": raw.get("words", []), "timestamps": raw.get("timestamps", [])}
    cache.write_text(json.dumps(out, indent=2))
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
