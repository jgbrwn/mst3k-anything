#!/usr/bin/env python3
"""Install mst3k-anything's CPU-first local environments and model."""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def python_path(root: Path, venv: str) -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return root / venv / folder / executable


def run(command: list[str | Path], *, dry_run: bool = False,
        cwd: Path | None = None) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"$ {printable}")
    if not dry_run:
        subprocess.run([str(part) for part in command], cwd=cwd, check=True)


def ensure_venv(root: Path, name: str, dry_run: bool) -> Path:
    path = root / name
    python = python_path(root, name)
    if python.exists():
        print(f"Using existing {name}")
        if not dry_run:
            try:
                run([str(python), "-m", "ensurepip", "--upgrade"], cwd=root)
            except subprocess.CalledProcessError:
                # Some system Python builds intentionally omit ensurepip; an
                # already-working pip is enough, and the next command reports
                # a useful error if it is not present.
                pass
        return python
    print(f"Creating {name}")
    run([sys.executable, "-m", "venv", str(path)], dry_run=dry_run, cwd=root)
    if not dry_run and not python.exists():
        raise RuntimeError(f"venv creation did not produce {python}")
    return python


def install_requirements(python: Path, requirements: Path, dry_run: bool) -> None:
    run([python, "-m", "pip", "install", "--upgrade", "pip"],
        dry_run=dry_run)
    run([python, "-m", "pip", "install", "-r", requirements],
        dry_run=dry_run)


def install_tts(python: Path, requirements: Path, dry_run: bool) -> None:
    run([python, "-m", "pip", "install", "--upgrade", "pip"],
        dry_run=dry_run)
    # The PyTorch CPU index prevents a Linux/Windows install from pulling a
    # large CUDA stack. macOS uses the normal PyPI wheel (CPU/MPS capable).
    if platform.system() in {"Linux", "Windows"}:
        run([python, "-m", "pip", "install", "torch",
             "--index-url", "https://download.pytorch.org/whl/cpu"],
            dry_run=dry_run)
    else:
        run([python, "-m", "pip", "install", "torch"], dry_run=dry_run)
    run([python, "-m", "pip", "install", "-r", requirements],
        dry_run=dry_run)


def ensure_env(root: Path, dry_run: bool) -> None:
    env = root / ".env"
    example = root / ".env.example"
    if env.exists():
        print(f"Keeping existing {env}")
    elif dry_run:
        print(f"Would copy {example} to {env}")
    else:
        if not example.exists():
            raise RuntimeError(f"missing {example}")
        shutil.copy2(example, env)
        try:
            os.chmod(env, 0o600)
        except OSError:
            pass
        print(f"Created {env}; configure it with scripts/configure.*")


def ffmpeg_hint() -> str:
    if platform.system() == "Windows":
        return "winget install --id Gyan.FFmpeg.Shared -e"
    if platform.system() == "Darwin":
        return "brew install ffmpeg"
    return "sudo apt-get update && sudo apt-get install -y ffmpeg"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="project root (mainly useful for testing)")
    parser.add_argument("--no-configure", action="store_true",
                        help="do not offer interactive provider setup")
    parser.add_argument("--skip-model", action="store_true",
                        help="do not download the Parakeet model")
    parser.add_argument("--skip-tts", action="store_true",
                        help="do not create/install tts-venv")
    parser.add_argument("--skip-asr", action="store_true",
                        help="do not create/install asr-venv")
    parser.add_argument("--dry-run", action="store_true",
                        help="show actions without changing files or installing packages")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if sys.version_info < (3, 10) or sys.version_info >= (3, 15):
            raise RuntimeError("Python 3.10 through 3.14 is required; Python 3.12 is recommended")
        print(f"Installing from {root}")
        web = ensure_venv(root, "web-venv", args.dry_run)
        install_requirements(web, root / "requirements-web.txt", args.dry_run)
        if not args.skip_asr:
            asr = ensure_venv(root, "asr-venv", args.dry_run)
            install_requirements(asr, root / "requirements-asr.txt", args.dry_run)
        if not args.skip_tts:
            tts = ensure_venv(root, "tts-venv", args.dry_run)
            install_tts(tts, root / "requirements-tts.txt", args.dry_run)
        ensure_env(root, args.dry_run)
        if not args.skip_model:
            run([sys.executable, root / "scripts" / "download_model.py",
                 "--root", root], dry_run=args.dry_run, cwd=root)

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            print(f"Found ffmpeg: {ffmpeg}")
        else:
            print("WARNING: ffmpeg/ffprobe were not found on PATH.")
            print(f"Install them before the first render: {ffmpeg_hint()}")

        if not args.no_configure and not args.dry_run and sys.stdin.isatty():
            answer = input("Configure an LLM provider now? [Y/n] ").strip().lower()
            if answer not in {"n", "no"}:
                run([sys.executable, root / "scripts" / "configure.py", "--root", root], cwd=root)
        elif not args.no_configure:
            print(f"Next: {sys.executable} scripts/configure.py")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("Installation steps complete.")
    print("Run `python scripts/doctor.py` to see any remaining prerequisites.")
    print("Run `scripts/start.sh` (Linux/macOS) or `scripts\\start.ps1` (Windows) to start the WebUI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
