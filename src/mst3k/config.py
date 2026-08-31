"""Config: defaults + .env overrides. All LLM/TTS/media knobs in one place."""
import os
from pathlib import Path

from .llm import load_env

BASE = Path(__file__).resolve().parents[2]

DEFAULTS = {
    # paths
    "jobs_dir": BASE / "jobs",
    "pocket_tts": BASE / "tts-venv/bin/pocket-tts",
    # LLM (swappable via .env)
    "llm_base": "https://hyper.charm.land/v1",
    "llm_key": "",
    "llm_model": "qwen3.8-flash",
    "llm_understand_model": "qwen3.8-flash",
    # voice lineup (ensemble configuration)
    "voice_ref": None,          # solo: optional public-domain wav to clone
    "voice_rate": 1.0,
    "voice_pitch": 0.0,         # semitones; e.g. +2 for puppet-y
    "voices": [                 # ensemble: Alba default + Jane sidekick
        {"name": "alba", "pitch": 0.0, "rate": 1.0, "weight": 0.7},
        {"name": "jane", "pitch": 2.0, "rate": 1.0, "weight": 0.3},
    ],
    "riff_gain": 1.4,
    "duck_amount": 0.65,
    # timing/comedy
    "min_gap": 0.8,
    "margin": 0.2,
    "lead_in_sec": 3.0,              # don't riff in the first N sec (titles/logos)
    "max_riff_seconds": 9.0,
    "words_per_second": 2.0,         # Pocket TTS measured rate (~2.0 wps actual)
    "max_tempo_stretch": 2.0,
    "max_riffs": 400,
    # riff-window detection
    "target_riff_count": 6,          # final output cap
    "window_pool_size": 16,           # candidates offered to the writer (~target * 2)
    "silence_ratio_ok": 0.04,
    "min_silence_frac": 0.5,
    "moment_win_sec": 1.6,
    "moment_hop_sec": 1.2,
    "silence_gate_db": "-32dB",       # primary riff-window detection
    "speech_noise_db": "-42dB",       # audible-speech gate for moment rejection
    "speech_dur": 0.2,                # even 200ms at this level = audible
    "min_riff_space_sec": 10.0,
    "moment_relax_db": 3.0,          # moments must be *quieter* than median, not just not-loud
    "max_speech_frac": 0.30,          # reject moment windows >30% spoken         # riff at most (median + this) dB
    # media
    "frame_width": 640,
    "crf": 22,
    "animated_overlay": False,        # re-encode cost; static by default; opt-in
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    env = load_env()

    # provider-specific (survives reboot; pdb precedence over tmp files)
    prov_env = [
        ("hyper",   "HYPER_BASE_URL",   "HYPER_API_KEY",   "HYPER_WRITER_MODEL"),
        ("neuralwatt", "NEURALWATT_BASE_URL", "NEURALWATT_API_KEY", "NEURALWATT_WRITER_MODEL"),
        ("openrouter", "OPENROUTER_BASE_URL", "OPENROUTER_API_KEY", "OPENROUTER_WRITER_MODEL"),
    ]
    for pid, burl, bkey, bmodel in prov_env:
        if env.get(burl): cfg[f"{pid}_base"] = env[burl]
        if env.get(bkey): cfg[f"{pid}_key"] = env[bkey]
        if env.get(bmodel): cfg[f"{pid}_model"] = env[bmodel]

    # legacy aliases (existing .env's still work)
    if env.get("LLM_BASE_URL"): cfg["llm_base"] = env["LLM_BASE_URL"]
    if env.get("LLM_API_KEY"): cfg["llm_key"] = env["LLM_API_KEY"]
    if env.get("LLM_MODEL"): cfg["llm_model"] = env["LLM_MODEL"]
    if env.get("LLM_UNDERSTAND_MODEL"): cfg["llm_understand_model"] = env["LLM_UNDERSTAND_MODEL"]
    if env.get("VOICE_REF"): cfg["voice_ref"] = env["VOICE_REF"]
    if env.get("VOICE_PITCH"):
        try: cfg["voice_pitch"] = float(env["VOICE_PITCH"])
        except ValueError: pass
    if env.get("RIFT_GAIN"):
        try: cfg["riff_gain"] = float(env["RIFT_GAIN"])
        except ValueError: pass
    return cfg
