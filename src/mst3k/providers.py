"""Multi-provider LLM routing.

Configuration source order:
1. request-time override {"provider": ..., "model": ...} -> job["llm"]
2. config.llm_provider / llm_model
3. per-provider defaults table

Each provider row carries base URL + model. The UI pulls this for the picker;
openrouter additionally exposes a live model list (high-context, multimodal)
that the picker merges with its own model.
"""
import json
import os
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]


def _read(path: str):
    p = Path(path)
    return p.read_text().strip() if p.exists() else None


def load_providers() -> dict:
    """api keys sourced from .env or /tmp/*_api_key (temporary demo)."""
    env = {}
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return {
        "hyper": {
            "base_url": env.get("LLM_BASE", "https://hyper.charm.land/v1"),
            "key": env.get("LLM_API_KEY") or _read("/tmp/hyper_api_key"),
            "default_model": env.get("LLM_MODEL", "qwen3.8-flash"),
            "supports_vision": True,
        },
        "neuralwatt": {
            "base_url": "https://api.neuralwatt.com/v1",
            "key": _read("/tmp/neuralwatt_api_key"),
            "default_model": "kimi-k3-fast",
            "supports_vision": True,
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key": _read("/tmp/openrouter_api_key"),
            "default_model": env.get("OR_MODEL"),   # UI replaces
            "supports_vision": True,
        },
    }


_CACHE = ROOT / "app" / "data" / "or_models.json"


def openrouter_models(ttl_sec: int = 3600, min_context: int = 128_000,
                      need_vision: bool = True) -> list:
    """Live OpenRouter model list filtered for high-context multimodal."""
    import time
    if _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < ttl_sec:
        try:
            return json.loads(_CACHE.read_text())
        except Exception:
            pass
    key = _read("/tmp/openrouter_api_key")
    req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)["data"]
    kept = []
    for m in data:
        ctx = m.get("context_length", 0)
        modality = (m.get("architecture") or {}).get("modality", "")
        if ctx >= min_context and (not need_vision or ("image" in modality or "multimodal" in modality)):
            kept.append({
                "id": m["id"],
                "name": m.get("name") or m["id"],
                "context_length": ctx,
                "modality": modality,
                "pricing_prompt": (m.get("pricing") or {}).get("prompt"),
            })
    kept.sort(key=lambda m: -m["context_length"])
    _CACHE.parent.mkdir(exist_ok=True)
    _CACHE.write_text(json.dumps(kept, indent=2))
    return kept


def resolve(job: dict) -> dict:
    """Return the {base_url, key, model} for this job.

    Explicit process environment overrides are used by the API worker for a
    single render, then request/job fields, then provider defaults.
    """
    prov = (os.environ.get("MST3K_PROVIDER") or
            job.get("llm_provider") or job.get("provider") or "hyper")
    override_mod = (os.environ.get("MST3K_MODEL") or
                    job.get("llm_model") or job.get("model"))
    table = load_providers()
    row = table.get(prov)
    if not row or not row.get("key"):
        prov, row = "hyper", table["hyper"]  # safe default
    return {"provider": prov, "base_url": row["base_url"], "key": row["key"],
            "model": override_mod or row.get("default_model")}
