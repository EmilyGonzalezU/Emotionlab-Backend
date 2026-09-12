"""Procesa sesiones legacy con el pipeline IA existente.

Uso seguro:
    python scripts/process_existing_sessions.py --dry-run
    python scripts/process_existing_sessions.py --execute

El modo dry-run nunca escribe en DynamoDB ni ejecuta el modelo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.aws_service import AWSService  # noqa: E402
from services.prediction_service import PredictionService  # noqa: E402
from services.session_processing_service import SessionProcessingService  # noqa: E402
from services.session_service import SessionService  # noqa: E402
from services.storage_repository import DynamoDBRepository, S3Repository  # noqa: E402


def _storage_repository():
    backend = __import__("os").getenv("STORAGE_BACKEND", "dynamodb").strip().lower()
    if backend == "s3":
        return S3Repository()
    if backend == "dynamodb":
        from services.dynamodb_service import DynamoDBService
        return DynamoDBRepository(DynamoDBService())
    raise RuntimeError("STORAGE_BACKEND debe ser 'dynamodb' o 's3'")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    aws_service = AWSService()
    payload = aws_service.fetch_sessions()
    records = payload["sesiones"]

    session_service = SessionService(dynamodb_service=_storage_repository())
    processor = SessionProcessingService(
        session_service=session_service,
        prediction_service=PredictionService(),
    )
    if args.preview:
        summary = processor.preview(records, progress=True)
        details = summary.pop("details")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("\nDETALLE:")
        for detail in details:
            print(json.dumps(detail, ensure_ascii=False))
        print("PREVIEW: se ejecutó parser/modelo en memoria; no se escribieron datos.")
        return 0

    summary = processor.process(
        records,
        execute=args.execute,
        progress=args.execute,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("DRY-RUN: no se ejecutó el modelo y no se escribieron datos.")
    else:
        print("EXECUTE: procesamiento finalizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
