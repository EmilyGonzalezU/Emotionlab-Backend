"""Check the expected files exist in built Lambda ZIP artifacts."""
from __future__ import annotations

import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "backend.zip": {"lambda_handler.py", "app.py", "flask/__init__.py"},
    "sync.zip": {"lambda_function.py", "services/import_service.py", "httpx/__init__.py"},
    "test.zip": {"lambda_function.py"},
}


def main() -> None:
    package_dir = ROOT / "infrastructure" / "terraform" / "packages"
    for filename, required in ARTIFACTS.items():
        path = package_dir / filename
        with zipfile.ZipFile(path) as archive:
            missing = required.difference(archive.namelist())
            if missing:
                raise SystemExit(f"{filename}: missing {sorted(missing)}")
            if archive.testzip() is not None:
                raise SystemExit(f"{filename}: corrupt ZIP member")
        print(f"{filename}: OK ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
