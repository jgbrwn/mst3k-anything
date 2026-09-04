#!/usr/bin/env python3
"""Check a local mst3k-anything installation and print actionable fixes."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_path(root: Path, name: str) -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return root / name / folder / executable


def _configured(value: str | None) -> bool:
    return bool(value and not (value.startswith("<") and value.endswith(">")))


def _tool_value(root: Path, value: str) -> str:
    if "/" not in value and "\\" not in value:
        return value
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else root / path)


def _tool(root: Path, name: str) -> str | None:
    # Keep this script stdlib-only but mirror the resolver's important search paths.
    values = _env(root)
    env_names = {
        "ffmpeg": ("MST3K_FFMPEG", "FFMPEG_BIN"),
        "ffprobe": ("MST3K_FFPROBE", "FFPROBE_BIN"),
        "yt-dlp": ("MST3K_YTDLP", "YT_DLP_BIN"),
        "pocket-tts": ("MST3K_POCKET_TTS", "POCKET_TTS_BIN"),
    }
    for key in env_names.get(name, ()):
        value = os.environ.get(key, values.get(key))
        if _configured(value):
            return _tool_value(root, value)
    exe = name + ".exe" if os.name == "nt" else name
    candidates = [root / "tools" / "bin" / exe]
    if name == "yt-dlp":
        candidates += [_python_path(root, ".venv").parent / exe,
                       _python_path(root, "web-venv").parent / exe]
    elif name == "pocket-tts":
        candidates += [_python_path(root, ".venv").parent / exe,
                       _python_path(root, "tts-venv").parent / exe]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _run(command: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode == 0, output[0] if output else f"exit code {result.returncode}"


def _env(root: Path) -> dict[str, str]:
    values = {}
    path = root / ".env"
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--strict", action="store_true",
                        help="also fail when no provider key/model is configured")
    args = parser.parse_args()
    root = args.root.resolve()
    env = _env(root)
    errors: list[str] = []
    warnings: list[str] = []

    def good(message: str) -> None:
        print(f"[OK]   {message}")

    def warn(message: str) -> None:
        warnings.append(message)
        print(f"[WARN] {message}")

    def fail(message: str) -> None:
        errors.append(message)
        print(f"[FAIL] {message}")

    version = sys.version_info
    if (3, 10) <= version < (3, 15):
        good(f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        fail("Python 3.10–3.14 is required; Python 3.12 is the recommended version")

    for venv, packages in (
        ("web-venv", ("fastapi", "uvicorn")),
        ("asr-venv", ("numpy", "sherpa_onnx")),
        ("tts-venv", ("pocket_tts", "torch")),
    ):
        python = _python_path(root, venv)
        if not python.is_file():
            fail(f"{venv} is missing; run the installer")
            continue
        ok, detail = _run([str(python), "-c", "import " + ",".join(packages)], root)
        if ok:
            good(f"{venv} imports {', '.join(packages)}")
        else:
            fail(f"{venv} dependency check failed: {detail}")

    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        path = _tool(root, name)
        if not path:
            if name == "ffmpeg":
                hint = "install ffmpeg with your OS package manager"
            elif name == "ffprobe":
                hint = "install ffmpeg (ffprobe is included)"
            else:
                hint = "rerun the installer so yt-dlp is installed in web-venv"
            fail(f"{name} was not found; {hint}")
            continue
        ok, detail = _run([path, "-version" if name != "yt-dlp" else "--version"])
        if ok:
            good(f"{name}: {detail}")
        else:
            fail(f"{name} is not executable: {detail}")

    model_raw = os.environ.get("MST3K_MODEL_DIR") or env.get("MST3K_MODEL_DIR")
    model_dir = Path(model_raw).expanduser() if model_raw else root / "models" / "parakeet-ctc"
    if not model_dir.is_absolute():
        model_dir = root / model_dir
    model = model_dir / "model.int8.onnx"
    tokens = model_dir / "tokens.txt"
    if model.is_file() and model.stat().st_size > 1_000_000 and tokens.is_file():
        good(f"Parakeet model present ({model.stat().st_size / 1048576:.0f} MB)")
    else:
        fail(f"Parakeet model missing from {model_dir}; run the installer")

    pocket = _tool(root, "pocket-tts")
    if pocket:
        ok, detail = _run([pocket, "--help"])
        if ok:
            good(f"PocketTTS command: {pocket}")
        else:
            fail(f"PocketTTS command failed: {detail}")
    else:
        fail("PocketTTS executable was not found; rerun the installer")

    configured = []
    for provider, key_name in (
        ("Hyper", "HYPER_API_KEY"),
        ("Neuralwatt", "NEURALWATT_API_KEY"),
        ("OpenRouter", "OPENROUTER_API_KEY"),
    ):
        if _configured(os.environ.get(key_name) or env.get(key_name)):
            configured.append(provider)
    if configured:
        good(f"LLM key configured for {', '.join(configured)}")
    else:
        warn("no LLM API key configured; run `python scripts/configure.py`")

    default_provider = os.environ.get("MST3K_PROVIDER") or env.get("MST3K_PROVIDER") or "hyper"
    if default_provider == "openrouter" and not _configured(
            os.environ.get("OPENROUTER_WRITER_MODEL") or env.get("OPENROUTER_WRITER_MODEL")):
        warn("OpenRouter is the default but OPENROUTER_WRITER_MODEL is blank; choose a model in the UI")

    try:
        jobs_raw = (os.environ.get("MST3K_JOBS_DIR") or env.get("MST3K_JOBS_DIR") or
                    env.get("JOBS_DIR"))
        jobs = Path(jobs_raw).expanduser() if jobs_raw else root / "jobs"
        if not jobs.is_absolute():
            jobs = root / jobs
        jobs.mkdir(parents=True, exist_ok=True)
        probe = jobs / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        good(f"jobs directory is writable: {jobs}")
    except OSError as exc:
        fail(f"jobs directory is not writable: {exc}")

    print()
    if errors:
        print("Fix the FAIL items above, then run this check again.")
    elif warnings:
        print("Installation is ready; configure an LLM key before submitting a job.")
    else:
        print("Installation is ready. Start the WebUI with scripts/start.*.")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
