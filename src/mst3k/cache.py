"""Small cache-signature helpers shared by media and LLM stages."""

import hashlib
import json
from pathlib import Path


def file_signature(path: Path | str) -> dict | None:
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"path": str(path.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def value_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
