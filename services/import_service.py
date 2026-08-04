"""Importacion del lote de sesiones de Unity almacenado en AWS.

Orquesta parser, modelo y SessionService sin acceder directamente a Firestore.
La descarga desde AWS puede entregar el contenido a ``import_batch``; asi el
servicio es independiente del mecanismo concreto de transporte.
"""

import json
from uuid import uuid4
from copy import deepcopy
from typing import Any, Mapping, Optional, Sequence, Union, cast

from services.prediction_service import PredictionService
from services.session_parser import SessionParser
from services.session_service import (
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_IDENTIFICATION,
    STATUS_PENDING_PREDICTION,
    SessionService,
)


BatchInput = Union[str, bytes, Mapping[str, Any], Sequence[Mapping[str, Any]]]


class ImportService:
    """Convierte registros de AWS en sesiones persistidas y clasificadas."""

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        predictor=None,
        prediction_service: Optional[PredictionService] = None,
    ):
        self.session_service = session_service or SessionService()
        # ``predictor`` conserva compatibilidad con los llamadores existentes;
        # el resto del servicio solo depende de PredictionService.
        self.prediction_service = prediction_service or PredictionService(predictor)

    def import_batch(self, payload: BatchInput) -> dict:
        """Importa todas las entradas del lote y devuelve un resumen del proceso."""
        sessions = self._extract_sessions(payload)
        result = {
            "total": len(sessions),
            "pending_identification": [],
            "pending_prediction": [],
            "import_errors": [],
            "skipped_existing": [],
        }

        for record in sessions:
            source_key = record.get("s3_key")
            if source_key and self.session_service.get_session_by_source_key(source_key):
                result["skipped_existing"].append(source_key)
                continue
            session_id = self._session_id(record)
            parser = None
            try:
                parser = SessionParser(record)

                if not parser.can_predict():
                    self._create_pending_prediction(session_id, record, result, parser)
                    continue

                prediction = self._predict(parser.get_prediction_features())
                summary = parser.get_dashboard_summary()
                self.session_service.create_session(
                    session_id,
                    record,
                    prediction=prediction,
                    status=STATUS_PENDING_IDENTIFICATION,
                    fecha=summary.get("fecha"),
                    hora=summary.get("hora"),
                    edad=summary.get("edad"),
                    genero=summary.get("genero"),
                    difficulty=summary.get("difficulty"),
                    source_key=source_key,
                )
                result["pending_identification"].append(session_id)
            except ValueError as e:
                print("\n==============================")
                print("s3_key:", source_key)
                print("ERROR EN LA PREDICCION")
                print(e)
                if parser is not None:
                    print("answers:", parser.get_answers())
                print("==============================")

                self._create_pending_prediction(session_id, record, result)
            except Exception:
                self.session_service.create_session(
                    session_id,
                    record,
                    status=STATUS_IMPORT_ERROR,
                    source_key=source_key,
                )
                result["import_errors"].append(session_id)
        return result

    def import_from_aws(self, aws_service) -> dict:
        """Descarga el lote publicado por AWS y procesa cada JSON recibido."""
        return self.import_batch(aws_service.fetch_sessions())

    def _create_pending_prediction(
        self,
        session_id: str,
        record: Mapping[str, Any],
        result: dict,
        parser: SessionParser | None = None,
    ) -> None:
        """Persiste una sesion legible aunque sus features aun no sean suficientes."""
        try:
            parser = parser or SessionParser(record)
            summary = parser.get_dashboard_summary()
            self.session_service.create_session(
                session_id,
                record,
                status=STATUS_PENDING_PREDICTION,
                fecha=summary.get("fecha"),
                hora=summary.get("hora"),
                edad=summary.get("edad"),
                genero=summary.get("genero"),
                difficulty=summary.get("difficulty"),
                source_key=record.get("s3_key"),
            )
            result["pending_prediction"].append(session_id)
        except Exception:
            self.session_service.create_session(
                session_id,
                record,
                status=STATUS_IMPORT_ERROR,
                source_key=record.get("s3_key"),
            )
            result["import_errors"].append(session_id)

    def _predict(self, features: dict) -> dict:
        return self.prediction_service.predict(features)

    @staticmethod
    def _extract_sessions(payload: BatchInput) -> list[dict]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as error:
                raise ValueError("El lote de AWS no contiene JSON valido") from error

        if isinstance(payload, Mapping):
            sessions = payload.get("sesiones")
            if not isinstance(sessions, list):
                raise ValueError("El lote debe contener una lista 'sesiones'")
        elif isinstance(payload, Sequence):
            sessions = payload
        else:
            raise ValueError("El lote debe ser un objeto JSON o una lista de sesiones")

        normalized_sessions: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, Mapping):
                raise ValueError("Cada entrada del lote debe ser un objeto JSON")
            normalized_sessions.append(deepcopy(dict(cast(Mapping[str, Any], session))))
        return normalized_sessions

    @staticmethod
    def _session_id(record: Mapping[str, Any]) -> str:
        """Genera un ID estable y valido para Firestore a partir del origen AWS."""
        # Cada archivo recibido representa una sesion independiente y nunca se
        # reutiliza un ID: importar no puede sobrescribir documentos existentes.
        return f"session_{uuid4().hex}"
