#!/usr/bin/env python3
"""Download the pinned Parakeet model used by mst3k-anything."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8.tar.bz2"
)
MODEL_SHA256 = {
    "model.int8.onnx": "9177a9146cf32ee0cc8152276ef95116f312018d316be37ccf57f7efea81fc1a",
    "tokens.txt": "450e56bd2f036fe5b6aa821865838cc5aa9d8b0106134ce9a9ba0664abe6cd10",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid(model_dir: Path) -> bool:
    return all((model_dir / name).is_file() and
               _sha256(model_dir / name) == expected
               for name, expected in MODEL_SHA256.items())


def _download(url: str, output: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "mst3k-anything-installer"})
    with urllib.request.urlopen(request, timeout=600) as response, output.open("wb") as stream:
        total = int(response.headers.get("Content-Length", 0))
        done = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            stream.write(chunk)
            done += len(chunk)
            if total:
                print(f"    model download: {done / 1048576:.0f}/{total / 1048576:.0f} MB",
                      end="\r", flush=True)
    print()


def install(root: Path, force: bool = False) -> Path:
    model_dir = root / "models" / "parakeet-ctc"
    model_dir.mkdir(parents=True, exist_ok=True)
    if not force and _valid(model_dir):
        print(f"Parakeet model already present: {model_dir}")
        return model_dir

    with tempfile.TemporaryDirectory(prefix="mst3k-model-") as temp:
        archive = Path(temp) / "parakeet.tar.bz2"
        print("Downloading Parakeet TDT-CTC 110M INT8 (~100 MB)...")
        _download(MODEL_URL, archive)
        extracted = Path(temp) / "extracted"
        extracted.mkdir()
        wanted = set(MODEL_SHA256)
        found = set()
        with tarfile.open(archive, mode="r:bz2") as bundle:
            for member in bundle:
                name = Path(member.name).name
                if name not in wanted or not member.isfile() or name in found:
                    continue
                source = bundle.extractfile(member)
                if source is None:
                    continue
                target = extracted / name
                with target.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
                found.add(name)
        if found != wanted:
            raise RuntimeError(f"model archive did not contain {sorted(wanted - found)}")
        for name, expected in MODEL_SHA256.items():
            actual = _sha256(extracted / name)
            if actual != expected:
                raise RuntimeError(f"checksum mismatch for {name}: {actual}")
        for name in wanted:
            tmp = model_dir / f".{name}.tmp"
            shutil.copy2(extracted / name, tmp)
            tmp.replace(model_dir / name)
    print(f"Installed Parakeet model: {model_dir}")
    return model_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--force", action="store_true", help="redownload even when checksums match")
    args = parser.parse_args()
    try:
        install(args.root.resolve(), force=args.force)
    except Exception as exc:
        print(f"Model download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
