"""Ensure the serialized model copied into the container matches the source model."""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ia" / "modelo_pipeline.pkl"
IMAGE = ROOT / "model-image" / "ia" / "modelo_pipeline.pkl"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    source_hash = digest(SOURCE)
    image_hash = digest(IMAGE)
    if source_hash != image_hash:
        raise SystemExit("The model-image artifact differs from the tested model")
    print(f"Model artifacts match: SHA-256 {source_hash}")


if __name__ == "__main__":
    main()
