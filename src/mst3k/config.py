"""Config: defaults, .env overrides, and cross-platform tool resolution."""
import os
import shutil
import sys
from pathlib import Path

from .llm import load_env

BASE = Path(__file__).resolve().parents[2]


def _is_placeholder(value: str | None) -> bool:
    return not value or (value.startswith("<") and value.endswith(">"))


def _path(value: str | Path | None, default: Path) -> Path:
    raw = str(value).strip() if value is not None else ""
    if _is_placeholder(raw):
        return default
    result = Path(raw).expanduser()
    return result if result.is_absolute() else BASE / result


def _executable_value(value: str) -> str:
    """Resolve a configured path relative to the project, preserving bare commands."""
    raw = str(value).strip()
    if "/" not in raw and "\\" not in raw:
        return raw
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else BASE / path)


def _venv_bin(venv_name: str) -> Path:
    return BASE / venv_name / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv_name: str) -> str:
    """Return a platform-correct Python executable from a project venv."""
    override_keys = {
        "asr-venv": ("MST3K_ASR_PYTHON", "ASR_PYTHON"),
        "tts-venv": ("MST3K_TTS_PYTHON", "TTS_PYTHON"),
        "web-venv": ("MST3K_WEB_PYTHON", "WEB_PYTHON"),
        ".venv": ("MST3K_PYTHON",),
    }
    env = load_env()
    for key in override_keys.get(venv_name, ()):
        value = os.environ.get(key) or env.get(key)
        if not _is_placeholder(value):
            return _executable_value(value)
    exe = "python.exe" if os.name == "nt" else "python"
    candidate = _venv_bin(venv_name) / exe
    if candidate.exists():
        return str(candidate)
    return sys.executable


_TOOL_ENV = {
    "ffmpeg": ("MST3K_FFMPEG", "FFMPEG_BIN"),
    "ffprobe": ("MST3K_FFPROBE", "FFPROBE_BIN"),
    "yt-dlp": ("MST3K_YTDLP", "YT_DLP_BIN"),
    "pocket-tts": ("MST3K_POCKET_TTS", "POCKET_TTS_BIN"),
}


def tool(name: str) -> str:
    """Resolve a media/tool executable without assuming Unix paths.

    Explicit environment values win, followed by project-local tools/ and the
    appropriate venv scripts directory, then the user's PATH.
    """
    env = load_env()
    for key in _TOOL_ENV.get(name, ()):
        value = os.environ.get(key) or env.get(key)
        if not _is_placeholder(value):
            return _executable_value(value)

    exe = name + ".exe" if os.name == "nt" else name
    candidates = [BASE / "tools" / "bin" / exe]
    if name == "yt-dlp":
        candidates.append(Path(venv_python("web-venv")).parent / exe)
        for venv in (".venv", "web-venv"):
            candidates.append(_venv_bin(venv) / exe)
    elif name == "pocket-tts":
        candidates.append(Path(venv_python("tts-venv")).parent / exe)
        for venv in (".venv", "tts-venv"):
            candidates.append(_venv_bin(venv) / exe)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which(name) or name


def cache_dir() -> Path:
    """Return the conventional per-user cache location for this OS."""
    override = os.environ.get("MST3K_VOICE_CACHE_DIR") or load_env().get(
        "MST3K_VOICE_CACHE_DIR")
    if override:
        return _path(override, BASE / "voice-cache")
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or
                    (Path.home() / "AppData" / "Local"))
        return root / "mst3k-anything" / "voices"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "mst3k-anything" / "voices"
    return Path.home() / ".cache" / "mst3k-anything" / "voices"


def model_dir() -> Path:
    env = load_env()
    return _path(os.environ.get("MST3K_MODEL_DIR") or env.get("MST3K_MODEL_DIR"),
                 BASE / "models" / "parakeet-ctc")


DEFAULTS = {
    # paths
    "jobs_dir": BASE / "jobs",
    "model_dir": BASE / "models" / "parakeet-ctc",
    "pocket_tts": tool("pocket-tts"),
    "asr_python": venv_python("asr-venv"),
    "voice_cache_dir": cache_dir(),
    # LLM (swappable via .env). Model fields are EMPTY by default so per-provider
    # defaults kick in when the user picks a non-Hyper provider; setting
    # HYPER/NEURALWATT/OPENROUTER_WRITER_MODEL in .env overrides.
    "llm_base": "https://api.neuralwatt.com/v1",
    "llm_key": "",
    "llm_model": "",
    "llm_understand_model": "",
    "default_provider": "hyper",
    # voice lineup (ensemble configuration)
    "voice_ref": None,          # optional local wav or .safetensors conditioning state
    "voice_rate": 1.0,          # global multiplier on built-in/custom delivery rate
    "voice_pitch": 0.0,         # global semitone offset; pool pitches are added to it
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
    "reaction_delay_sec": 0.35,     # natural audience reaction after the anchor beat
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
    "writer_batch_size": 6,
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

    # Re-resolve paths/tools at runtime so installers and platform overrides take effect.
    cfg["jobs_dir"] = _path(os.environ.get("MST3K_JOBS_DIR") or
                             env.get("MST3K_JOBS_DIR") or env.get("JOBS_DIR"),
                             DEFAULTS["jobs_dir"])
    cfg["model_dir"] = model_dir()
    cfg["pocket_tts"] = tool("pocket-tts")
    cfg["asr_python"] = venv_python("asr-venv")
    cfg["voice_cache_dir"] = _path(os.environ.get("MST3K_VOICE_CACHE_DIR") or
                                    env.get("MST3K_VOICE_CACHE_DIR"),
                                    DEFAULTS["voice_cache_dir"])
    cfg["default_provider"] = (os.environ.get("MST3K_PROVIDER") or
                                env.get("MST3K_PROVIDER") or "hyper").strip() or "hyper"

    # provider-specific (survives reboot; per-provider env takes precedence)
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
    if env.get("VOICE_RATE"):
        try: cfg["voice_rate"] = float(env["VOICE_RATE"])
        except ValueError: pass
    if env.get("VOICE_PITCH"):
        try: cfg["voice_pitch"] = float(env["VOICE_PITCH"])
        except ValueError: pass
    return cfg
