"""Procesamiento idempotente de sesiones legacy mediante el pipeline IA existente."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from botocore.exceptions import ClientError

from services.import_service import ImportService
from services.prediction_service import PredictionService
from services.session_parser import SessionParser
from services.session_service import (
    STATUS_PENDING_IDENTIFICATION,
    STATUS_PROCESSING_ERROR,
    SessionService,
)


class SessionProcessingService:
    """Procesa sesiones sin modificar la fuente cruda de AWS/S3."""

    def __init__(
        self,
        session_service: SessionService,
        prediction_service: Optional[PredictionService] = None,
    ):
        self.session_service = session_service
        self.prediction_service = prediction_service or PredictionService()

    def inspect(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Calcula el plan sin ejecutar el modelo ni escribir datos."""
        existing = self._existing_index()
        summary = self._empty_summary(len(records))

        for record in records:
            session_id = self._session_id(record)
            existing_item = existing.get(session_id) or existing.get(
                record.get("s3_key")
            )
            if existing_item:
                summary["already_existing"] += 1
                if self._has_prediction(existing_item):
                    summary["skipped_duplicates"] += 1
                else:
                    summary["would_update"] += 1

            try:
                SessionParser(record).get_prediction_features()
                summary["features_complete"] += 1
                summary["likely_processable"] += 1
            except Exception:
                summary["insufficient_data"] += 1

            if not existing_item:
                summary["would_create"] += 1

        return summary

    def process(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        execute: bool,
        progress: bool = False,
    ) -> dict[str, Any]:
        """Procesa registros; con execute=False solo devuelve el plan."""
        if not execute:
            return self.inspect(records)

        existing = self._existing_index()
        summary = self._empty_summary(len(records))
        for index, record in enumerate(records, start=1):
            if progress:
                print(f"Procesando {index}/{len(records)}", flush=True)
            self._process_one(record, existing, summary)
        return summary

    def preview(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        progress: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta parser y modelo en memoria sin tocar persistencia."""
        details: list[dict[str, Any]] = []
        probabilities: list[float] = []
        summary = {
            "total": len(records),
            "processable": 0,
            "not_processable": 0,
            "bienestar_si": 0,
            "bienestar_no": 0,
            "errors": 0,
            "probability_min": None,
            "probability_max": None,
            "probability_average": None,
            "above_threshold": 0,
            "below_threshold": 0,
            "threshold": 0.5,
            "details": details,
        }

        for index, record in enumerate(records, start=1):
            session_id = self._session_id(record)
            if progress:
                print(f"Preview {index}/{len(records)}", flush=True)
            detail = {"index": index, "session_id": session_id}
            try:
                parser = SessionParser(record)
                features = parser.get_prediction_features()
                prediction = self.prediction_service.predict(features)
                probability = float(prediction["probabilidad"])
                detail.update(
                    {
                        "bienestar": prediction["bienestar"],
                        "probabilidad": probability,
                        "confianza": float(prediction["confianza"]),
                    }
                )
                probabilities.append(probability)
                summary["processable"] += 1
                if prediction["bienestar"] == "SI":
                    summary["bienestar_si"] += 1
                else:
                    summary["bienestar_no"] += 1
                if probability >= summary["threshold"]:
                    summary["above_threshold"] += 1
                else:
                    summary["below_threshold"] += 1
            except Exception as error:
                summary["not_processable"] += 1
                summary["errors"] += 1
                detail["processing_error"] = f"{type(error).__name__}: {error}"
            details.append(detail)

        if probabilities:
            summary["probability_min"] = min(probabilities)
            summary["probability_max"] = max(probabilities)
            summary["probability_average"] = sum(probabilities) / len(probabilities)
        return summary

    def _process_one(
        self,
        record: Mapping[str, Any],
        existing: dict[str, dict],
        summary: dict[str, Any],
    ) -> None:
        session_id = self._session_id(record)
        source_key = record.get("s3_key")
        existing_item = existing.get(session_id) or existing.get(source_key)

        if existing_item:
            session_id = str(existing_item.get("session_id") or session_id)
            summary["already_existing"] += 1
            if self._has_prediction(existing_item):
                summary["skipped_duplicates"] += 1
                return

        try:
            parser = SessionParser(record)
            features = parser.get_prediction_features()
            summary["features_complete"] += 1
            summary["likely_processable"] += 1
            prediction = self.prediction_service.predict(features)
            summary_data = parser.get_dashboard_summary()
            fields = {
                "prediction": prediction,
                "status": STATUS_PENDING_IDENTIFICATION,
                "estado": STATUS_PENDING_IDENTIFICATION,
                "fecha": summary_data.get("fecha"),
                "hora": summary_data.get("hora"),
                "edad": summary_data.get("edad"),
                "genero": summary_data.get("genero"),
                "difficulty": summary_data.get("difficulty"),
                "duracion_estimada": summary_data.get("duracion_estimada", False),
                "duracion": summary_data.get("duracion"),
                "participant_id": self._participant_id(record),
                "prediction_error": None,
                "prediction_missing_fields": [],
            }
            self._save_result(
                record,
                session_id,
                source_key,
                existing_item,
                fields,
            )
            summary["processed_ok"] += 1
            summary["saved"] += 1
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            self._save_error(record, session_id, source_key, existing_item, reason)
            summary["errors"] += 1
            summary["saved"] += 1

    def _save_result(
        self,
        record: Mapping[str, Any],
        session_id: str,
        source_key: Optional[str],
        existing_item: Optional[Mapping[str, Any]],
        fields: dict[str, Any],
    ) -> None:
        if existing_item:
            self.session_service.dynamo.update_item(
                self.session_service.table,
                {"session_id": session_id},
                fields,
            )
            return
        self.session_service.create_session(
            session_id,
            record,
            source_key=source_key,
            prediction=fields["prediction"],
            status=fields["status"],
            fecha=fields["fecha"],
            hora=fields["hora"],
            edad=fields["edad"],
            genero=fields["genero"],
            difficulty=fields["difficulty"],
            duracion_estimada=fields["duracion_estimada"],
            duracion=fields["duracion"],
            participant_id=fields["participant_id"],
            fecha_inicio=self._session_data(record).get("fechaInicio"),
            fecha_fin=self._session_data(record).get("fechaFin"),
        )

    def _save_error(
        self,
        record: Mapping[str, Any],
        session_id: str,
        source_key: Optional[str],
        existing_item: Optional[Mapping[str, Any]],
        reason: str,
    ) -> None:
        parser = SessionParser(record)
        status = parser.get_prediction_status()
        fields = {
            "prediction": None,
            "status": STATUS_PROCESSING_ERROR,
            "estado": STATUS_PROCESSING_ERROR,
            "prediction_error": reason[:500],
            "prediction_missing_fields": status.get("missing_fields", []),
        }
        if existing_item:
            self.session_service.dynamo.update_item(
                self.session_service.table,
                {"session_id": session_id},
                fields,
            )
            return
        summary = parser.get_dashboard_summary()
        self.session_service.create_session(
            session_id,
            record,
            source_key=source_key,
            prediction=None,
            status=STATUS_PROCESSING_ERROR,
            fecha=summary.get("fecha"),
            hora=summary.get("hora"),
            edad=summary.get("edad"),
            genero=summary.get("genero"),
            difficulty=summary.get("difficulty"),
            duracion_estimada=summary.get("duracion_estimada", False),
            participant_id=self._participant_id(record),
            fecha_inicio=self._session_data(record).get("fechaInicio"),
            fecha_fin=self._session_data(record).get("fechaFin"),
        )
        self.session_service.dynamo.update_item(
            self.session_service.table,
            {"session_id": session_id},
            {
                "prediction": None,
                "prediction_error": reason[:500],
                "prediction_missing_fields": status.get("missing_fields", []),
            },
        )

    def _existing_index(self) -> dict[str, dict]:
        """
        Obtiene un índice de sesiones ya persistidas.

        En AWS Learner Lab, Scan y DescribeTable pueden estar bloqueados
        explícitamente por la política voc-cancel-cred. En ese escenario
        devolvemos un índice vacío y dejamos que el procesador trabaje con
        las sesiones obtenidas directamente desde la API de Unity.

        Esto permite procesar las sesiones históricas aunque DynamoDB no
        permita consultar la tabla completa.
        """
        try:
            items = self.session_service.dynamo.scan_all(
                self.session_service.table
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")

            if code != "AccessDeniedException":
                raise

            print(
                "[DYNAMODB] Scan denegado por la política del Learner Lab; "
                "se continúa sin índice de sesiones existentes."
            )

            return {}

        index: dict[str, dict] = {}

        for item in items:
            session_id = item.get("session_id") or item.get("id")
            source_key = item.get("source_key")

            if session_id:
                index[str(session_id)] = item

            if source_key:
                index[str(source_key)] = item

        return index
    @staticmethod
    def _session_id(record: Mapping[str, Any]) -> str:
        return ImportService._session_id(record)

    @staticmethod
    def _session_data(record: Mapping[str, Any]) -> dict[str, Any]:
        data = record.get("session_data")
        return dict(data) if isinstance(data, Mapping) else {}

    @classmethod
    def _participant_id(cls, record: Mapping[str, Any]) -> Optional[str]:
        participant_id = cls._session_data(record).get("idParticipante")
        return str(participant_id) if participant_id else None

    @staticmethod
    def _has_prediction(item: Mapping[str, Any]) -> bool:
        return isinstance(item.get("prediction"), Mapping) and bool(item["prediction"])

    @staticmethod
    def _empty_summary(total: int) -> dict[str, Any]:
        return {
            "total": total,
            "features_complete": 0,
            "likely_processable": 0,
            "insufficient_data": 0,
            "already_existing": 0,
            "would_create": 0,
            "would_update": 0,
            "processed_ok": 0,
            "errors": 0,
            "saved": 0,
            "skipped_duplicates": 0,
        }
