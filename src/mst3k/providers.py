"""Multi-provider LLM routing.

Configuration source order:
1. process environment MST3K_PROVIDER / MST3K_MODEL (per-render API override)
2. request-time override {"provider": ..., "model": ...} -> job["llm"]
3. per-provider defaults table

Each provider row carries base URL + model. The UI pulls a normalized high-context
multimodal catalog from `/models` when the provider exposes one; OpenRouter retains its
specialized live filter. Users can always type a model ID when discovery is unavailable.
"""
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _setting(env: dict, key: str) -> str | None:
    """Return a process override or .env value, ignoring template placeholders."""
    value = os.environ[key] if key in os.environ else env.get(key)
    if value is None:
        return None
    value = str(value).strip().strip('"').strip("'")
    if not value or (value.startswith("<") and value.endswith(">")):
        return None
    return value


def load_providers() -> dict:
    """Map provider id -> {base_url, key, default_model, supports_vision}.

    Provider settings are read from the process environment first and then the
    project .env. Legacy LLM_* aliases remain supported; no Unix-only secret
    file fallback is used, so this works consistently on Windows and macOS too.
    """
    from .llm import load_env
    env = load_env()
    return {
        "hyper": {
            "base_url": (_setting(env, "HYPER_BASE_URL") or
                          _setting(env, "LLM_BASE") or "https://hyper.charm.land/v1"),
            "key": _setting(env, "HYPER_API_KEY") or _setting(env, "LLM_API_KEY"),
            "default_model": (_setting(env, "HYPER_WRITER_MODEL") or
                              _setting(env, "LLM_MODEL") or "qwen3.8-flash"),
            "supports_vision": True,
        },
        "neuralwatt": {
            "base_url": (_setting(env, "NEURALWATT_BASE_URL") or
                          "https://api.neuralwatt.com/v1"),
            "key": _setting(env, "NEURALWATT_API_KEY"),
            "default_model": (_setting(env, "NEURALWATT_WRITER_MODEL") or
                              "kimi-k3-fast"),
            "supports_vision": True,
        },
        "openrouter": {
            "base_url": (_setting(env, "OPENROUTER_BASE_URL") or
                          "https://openrouter.ai/api/v1"),
            "key": _setting(env, "OPENROUTER_API_KEY"),
            "default_model": _setting(env, "OPENROUTER_WRITER_MODEL"),
            "supports_vision": True,
        },
    }


_CACHE = ROOT / "app" / "data" / "or_models.json"
_MODEL_CACHE_DIR = ROOT / "app" / "data"


def _as_context_length(model: dict, metadata: dict) -> int:
    for key in ("context_length", "context_window", "max_context_length",
                "max_model_len"):
        value = model.get(key, metadata.get(key))
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return 0


def _as_vision_flag(model: dict, metadata: dict):
    """Read common OpenAI/vLLM/provider capability shapes."""
    capability_sources = [
        model.get("capabilities"),
        metadata.get("capabilities"),
        model.get("architecture"),
        metadata,
    ]
    for source in capability_sources:
        if not isinstance(source, dict):
            continue
        for key in ("vision", "supports_vision", "multimodal", "image_input"):
            if key in source and isinstance(source[key], bool):
                return source[key]
        for key in ("input_modalities", "modalities", "modality"):
            value = source.get(key)
            if isinstance(value, str):
                value = value.lower()
                if "image" in value or "vision" in value or "multimodal" in value:
                    return True
            elif isinstance(value, (list, tuple)):
                values = {str(item).lower() for item in value}
                if values & {"image", "vision", "multimodal"}:
                    return True
    return None


def _normalize_model(model: dict) -> dict | None:
    if not isinstance(model, dict):
        return None
    metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    model_id = model.get("id") or model.get("name")
    if not model_id:
        return None
    return {
        "id": str(model_id),
        "name": str(model.get("display_name") or metadata.get("display_name") or
                         model.get("name") or model_id),
        "context_length": _as_context_length(model, metadata),
        "supports_vision": _as_vision_flag(model, metadata),
    }


def _filter_multimodal(models: list[dict], min_context: int = 128_000) -> list[dict]:
    eligible = [m for m in models
                if not m.get("context_length") or m["context_length"] >= min_context]
    known_flags = [m.get("supports_vision") for m in eligible
                   if m.get("supports_vision") is not None]
    # If the provider publishes capability metadata, show only models that can
    # consume the frames used by understand/write/judge. If it does not, keep
    # the catalog usable and let the user type/select a model to try.
    if known_flags:
        eligible = [m for m in eligible if m.get("supports_vision") is True]
    return sorted(eligible, key=lambda m: (-m.get("context_length", 0), m["id"]))


def provider_models(provider: str, ttl_sec: int = 3600,
                    min_context: int = 128_000) -> list[dict]:
    """Return a provider's high-context multimodal models when discoverable."""
    if provider == "openrouter":
        return [{**model, "supports_vision": True}
                for model in openrouter_models(min_context=min_context)]
    table = load_providers()
    row = table.get(provider)
    if not row:
        raise RuntimeError(f"unknown LLM provider {provider!r}")
    if not row.get("key"):
        raise RuntimeError(f"LLM provider {provider!r} has no API key configured")
    cache = _MODEL_CACHE_DIR / f"{provider}_models.json"
    import time
    if cache.exists() and (time.time() - cache.stat().st_mtime) < ttl_sec:
        try:
            return json.loads(cache.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    req = urllib.request.Request(row["base_url"].rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {row['key']}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        raise RuntimeError(f"provider {provider!r} returned no model list")
    models = _filter_multimodal([m for m in (_normalize_model(item) for item in raw_models)
                                 if m is not None], min_context=min_context)
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(models, indent=2))
    return models


def openrouter_models(ttl_sec: int = 3600, min_context: int = 128_000,
                      need_vision: bool = True) -> list:
    """Live OpenRouter model list filtered for high-context multimodal."""
    import time
    if _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < ttl_sec:
        try:
            return json.loads(_CACHE.read_text())
        except Exception:
            pass
    row = load_providers().get("openrouter", {})
    if not row.get("key"):
        raise RuntimeError("OpenRouter API key is not configured")
    req = urllib.request.Request(row["base_url"].rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {row['key']}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)["data"]
    kept = []
    for model in data:
        ctx = model.get("context_length", 0)
        modality = (model.get("architecture") or {}).get("modality", "")
        if ctx >= min_context and (not need_vision or
                                    ("image" in modality or "multimodal" in modality)):
            kept.append({
                "id": model["id"],
                "name": model.get("name") or model["id"],
                "context_length": ctx,
                "modality": modality,
                "pricing_prompt": (model.get("pricing") or {}).get("prompt"),
            })
    kept.sort(key=lambda model: -model["context_length"])
    _CACHE.parent.mkdir(exist_ok=True)
    _CACHE.write_text(json.dumps(kept, indent=2))
    return kept


def resolve(job: dict, role: str = "write") -> dict:
    """Return the {base_url, key, model} for this job and role.

    Roles: "write" (default), "judge" (QA pass), "understand", "transcribe".
    Per-role environment values override job values; job values override the
    main provider/model; the role then falls back to its provider default.
    """
    from .llm import load_env
    env = load_env()

    def setting(key: str) -> str | None:
        return os.environ[key] if key in os.environ else env.get(key)

    prov = (setting(f"MST3K_{role.upper()}_PROVIDER") or
            setting("MST3K_PROVIDER") or
            job.get(f"{role}_provider") or job.get("llm_provider") or
            job.get("provider") or "hyper")
    override_mod = (setting(f"MST3K_{role.upper()}_MODEL") or
                    setting("MST3K_MODEL") or
                    job.get(f"{role}_model") or
                    job.get("model"))
    table = load_providers()
    row = table.get(prov)
    if not row:
        raise RuntimeError(f"unknown LLM provider {prov!r}")
    if not row.get("key"):
        raise RuntimeError(f"LLM provider {prov!r} has no API key configured")
    model = override_mod or row.get("default_model")
    if not model:
        raise RuntimeError(f"LLM provider {prov!r} has no model configured")
    return {"provider": prov, "base_url": row["base_url"], "key": row["key"],
            "model": model}
