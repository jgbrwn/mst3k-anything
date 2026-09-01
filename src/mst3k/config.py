"""Config: defaults + .env overrides. All LLM/TTS/media knobs in one place."""
import os
from pathlib import Path

from .llm import load_env

BASE = Path(__file__).resolve().parents[2]

DEFAULTS = {
    # paths
    "jobs_dir": BASE / "jobs",
    "pocket_tts": BASE / "tts-venv/bin/pocket-tts",
    # LLM (swappable via .env). Model fields are EMPTY by default so per-provider
    # defaults kick in when the user picks a non-Hyper provider; setting
    # HYPER/NEURALWATT/OPENROUTER_WRITER_MODEL in .env overrides.
    "llm_base": "https://api.neuralwatt.com/v1",
    "llm_key": "",
    "llm_model": "",
    "llm_understand_model": "",
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
    "min_gap": 0.35,              # only the minimum pause signal; not a riff gate
    "margin": 0.15,               # preferred landing margin, not a hard boundary
    "lead_in_sec": 3.0,            # avoid title cards/logos by default
    "short_clip_lead_ratio": 0.1,   # explicit policy for clips shorter than the lead-in
    "max_riff_seconds": 9.0,       # preferred spoken length; overtalk may exceed it
    "words_per_second": 2.0,       # Pocket TTS measured rate (~2.0 wps actual)
    "max_tempo_stretch": 1.18,     # modest speed-up; long riffs may overlap dialogue
    "max_riffs": 400,              # emergency safety ceiling only
    # riff-window detection: lo/hi are seconds between *baseline* cues. Silence
    # and quietness improve ranking, but cadence cues guarantee coverage.
    "riff_pace_per_kind": {
        "movie":      {"lo": 18.0, "hi": 30.0},
        "tv":         {"lo": 16.0, "hi": 27.0},
        "vlog":       {"lo": 14.0, "hi": 24.0},
        "tutorial":   {"lo": 15.0, "hi": 26.0},
        "gaming":     {"lo": 10.0, "hi": 18.0},
        "music":      {"lo": 12.0, "hi": 22.0},
        "home":       {"lo": 15.0, "hi": 26.0},
        "commercial": {"lo": 10.0, "hi": 20.0},
        "other":      {"lo": 16.0, "hi": 27.0},
    },
    "window_pool_factor": 1.0,
    "context_radius_sec": 18.0,
    "writer_batch_size": 8,
    "max_line_chars": 240,
    "moment_win_sec": 1.6,
    "moment_hop_sec": 1.2,
    "silence_gate_db": "-25dB",     # ranking signal, not an eligibility gate
    "silence_min_dur": 0.35,
    "min_riff_space_sec": 2.5,       # only dedupes near-identical cues

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
    if env.get("TARGET_RIFF_COUNT"):
        try: cfg["target_riff_count"] = int(env["TARGET_RIFF_COUNT"])
        except ValueError: pass
    if env.get("CONTEXT_RADIUS_SEC"):
        try: cfg["context_radius_sec"] = float(env["CONTEXT_RADIUS_SEC"])
        except ValueError: pass
    if env.get("SHORT_CLIP_LEAD_RATIO"):
        try: cfg["short_clip_lead_ratio"] = float(env["SHORT_CLIP_LEAD_RATIO"])
        except ValueError: pass
    if env.get("MIN_RIFF_SPACE_SEC"):
        try: cfg["min_riff_space_sec"] = float(env["MIN_RIFF_SPACE_SEC"])
        except ValueError: pass
    if env.get("MAX_RIFFS"):
        try: cfg["max_riffs"] = int(env["MAX_RIFFS"])
        except ValueError: pass
    if env.get("RIFF_GAIN") or env.get("RIFT_GAIN"):
        try: cfg["riff_gain"] = float(env.get("RIFF_GAIN") or env.get("RIFT_GAIN"))
        except ValueError: pass
    if env.get("VOICE_REF"): cfg["voice_ref"] = env["VOICE_REF"]
    if env.get("VOICE_PITCH"):
        try: cfg["voice_pitch"] = float(env["VOICE_PITCH"])
        except ValueError: pass
    return cfg
