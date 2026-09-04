#!/usr/bin/env python3
"""Start the local mst3k-anything WebUI without shell-specific activation."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def web_python(root: Path) -> Path:
    configured = os.environ.get("MST3K_WEB_PYTHON")
    env_path = root / ".env"
    if not configured and env_path.exists():
        for raw in env_path.read_text().splitlines():
            if raw.strip().startswith("MST3K_WEB_PYTHON="):
                configured = raw.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_absolute() else root / candidate
    folder = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return root / "web-venv" / folder / executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=os.environ.get("MST3K_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MST3K_PORT", "8000")))
    args = parser.parse_args()
    root = args.root.resolve()
    python = web_python(root)
    if not python.is_file():
        print(f"Missing {python}. Run the installer first.", file=sys.stderr)
        return 1
    doctor = root / "scripts" / "doctor.py"
    if doctor.is_file():
        check = subprocess.run([sys.executable, str(doctor), "--root", str(root)],
                               cwd=root, check=False)
        if check.returncode:
            print("Installation check failed; fix the doctor output above.", file=sys.stderr)
            return check.returncode
    env = dict(os.environ)
    source = str(root / "src")
    env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    print(f"WebUI: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        return subprocess.call([
            str(python), "-m", "uvicorn", "app.api:app", "--app-dir", str(root),
            "--host", args.host, "--port", str(args.port),
        ], cwd=root, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
