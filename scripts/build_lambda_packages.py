"""Build deterministic Lambda ZIP artifacts for Terraform."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infrastructure" / "terraform" / "packages"
SOURCE_DIRS = ("core", "middleware", "routes", "services", "utils")
SOURCE_FILES = ("app.py", "lambda_handler.py")


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="emotionlab-lambda-") as temp:
        stage = Path(temp) / "backend"
        stage.mkdir()
        for name in SOURCE_FILES:
            shutil.copy2(ROOT / name, stage / name)
        for name in SOURCE_DIRS:
            shutil.copytree(ROOT / name, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.13", "--abi", "cp313", "--only-binary=:all:", "-r", str(ROOT / "requirements-lambda.lock.txt"), "-t", str(stage)],
            check=True,
        )
        _zip_tree(stage, OUT / "backend.zip")

        sync_stage = Path(temp) / "sync"
        shutil.copytree(stage, sync_stage)
        (sync_stage / "app.py").unlink()
        (sync_stage / "lambda_handler.py").unlink()
        shutil.copy2(ROOT / "lambda-sync" / "lambda_function.py", sync_stage / "lambda_function.py")
        _zip_tree(sync_stage, OUT / "sync.zip")

        test_stage = Path(temp) / "test"
        test_stage.mkdir()
        (test_stage / "lambda_function.py").write_text(
            "import json\n\ndef lambda_handler(event, context):\n    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json'}, 'body': json.dumps({'ok': True, 'service': 'emotionlab-test'})}\n",
            encoding="utf-8",
        )
        _zip_tree(test_stage, OUT / "test.zip")


def _zip_tree(source: Path, artifact: Path) -> None:
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = zipfile.ZipInfo(path.relative_to(source).as_posix(), (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    print(f"Created {artifact} ({artifact.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
