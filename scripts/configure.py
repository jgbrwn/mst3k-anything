#!/usr/bin/env python3
"""Interactively configure one LLM provider without echoing its API key."""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
from pathlib import Path

PROVIDERS = {
    "hyper": {
        "label": "Hyper",
        "prefix": "HYPER",
        "default_model": "qwen3.8-flash",
        "model_note": "blank uses qwen3.8-flash",
    },
    "neuralwatt": {
        "label": "Neuralwatt",
        "prefix": "NEURALWATT",
        "default_model": "kimi-k3-fast",
        "model_note": "blank uses kimi-k3-fast",
    },
    "openrouter": {
        "label": "OpenRouter",
        "prefix": "OPENROUTER",
        "default_model": "",
        "model_note": "enter a provider/model ID, for example google/gemma-4-31b-it",
    },
}


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _set_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines()
    replacement = f"{key}={value}"
    for index, raw in enumerate(lines):
        stripped = raw.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[index] = replacement
            path.write_text("\n".join(lines) + "\n")
            return
    lines.append(replacement)
    path.write_text("\n".join(lines) + "\n")


def configure(root: Path, provider: str | None = None, noninteractive: bool = False) -> int:
    env_path = root / ".env"
    example = root / ".env.example"
    if not env_path.exists():
        if not example.exists():
            raise RuntimeError(f"missing {example}")
        shutil.copy2(example, env_path)
        print(f"Created {env_path} from .env.example")

    current = _read_env(env_path)
    existing_provider = current.get("MST3K_PROVIDER") or "hyper"
    if provider is None:
        if noninteractive:
            provider = existing_provider
        else:
            choices = "/".join(PROVIDERS)
            answer = input(f"Default provider [{choices}] ({existing_provider}): ").strip().lower()
            provider = answer or existing_provider
    if provider not in PROVIDERS:
        raise RuntimeError(f"provider must be one of: {', '.join(PROVIDERS)}")

    spec = PROVIDERS[provider]
    prefix = spec["prefix"]
    _set_value(env_path, "MST3K_PROVIDER", provider)

    if noninteractive:
        key = ""
        model = ""
    else:
        key = getpass.getpass(
            f"{spec['label']} API key (leave blank to keep the existing key): ")
        model = input(
            f"{spec['label']} writer model ({spec['model_note']}; blank keeps current): "
        ).strip()
    if key:
        _set_value(env_path, f"{prefix}_API_KEY", key)
    if model:
        _set_value(env_path, f"{prefix}_WRITER_MODEL", model)
    elif provider == "openrouter" and not current.get("OPENROUTER_WRITER_MODEL") and not key:
        print("OpenRouter still needs a model and API key before a job can run.")

    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    print(f"Saved provider settings to {env_path} (the API key was not displayed).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--provider", choices=sorted(PROVIDERS))
    parser.add_argument("--noninteractive", action="store_true",
                        help="only set the default provider; never prompt or change keys")
    args = parser.parse_args()
    try:
        return configure(args.root.resolve(), args.provider, args.noninteractive)
    except (OSError, RuntimeError, EOFError) as exc:
        print(f"Configuration failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
