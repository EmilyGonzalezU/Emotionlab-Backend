"""Procesamiento y reprocesamiento de sesiones con el modelo único V2 (K-Means).

Este servicio opera sobre registros de Unity/AWS sin alterar la fuente cruda.
Persiste el JSON íntegro y los metadatos normalizados de cada experiencia y,
cuando el registro contiene respuestas del contrato V2, aplica el análisis
K-Means congelado de forma ADITIVA (campo ``model_processing``).

No existe ninguna ruta hacia el modelo V1: el único motor de IA vigente es V2.
El análisis se ejecuta únicamente en procesamientos nuevos o en
reprocesamientos expresamente solicitados.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from botocore.exceptions import ClientError

from services.emotion_classification import enriquecer_analisis
from services.emotion_model_v2_service import (
    ESTADO_ANALIZADO,
    ESTADO_DATOS_INSUFICIENTES,
    MODEL_VERSION_ESPERADA,
    EmotionModelV2Service,
)
from services.import_service import ImportService
from services.parsers.emotion_v2_parser import EmotionV2Parser
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
        emotion_v2_service=None,
    ):
        self.session_service = session_service
        # Motor V2 (K-Means congelado): el único motor de IA vigente.
        self.emotion_v2 = emotion_v2_service

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
                if self._has_analysis(existing_item):
                    summary["skipped_duplicates"] += 1
                else:
                    summary["would_update"] += 1

            if self._has_model_input(record):
                summary["features_complete"] += 1
                summary["likely_processable"] += 1
            else:
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
        """Ejecuta el análisis V2 en memoria sin tocar persistencia."""
        details: list[dict[str, Any]] = []
        summary = {
            "total": len(records),
            "processable": 0,
            "not_processable": 0,
            "analyzed": 0,
            "insufficient_data": 0,
            "errors": 0,
            "groups": {"0": 0, "1": 0, "2": 0},
            "details": details,
        }

        for index, record in enumerate(records, start=1):
            session_id = self._session_id(record)
            if progress:
                print(f"Preview {index}/{len(records)}", flush=True)
            detail = {"index": index, "session_id": session_id}
            try:
                if self.emotion_v2 is None:
                    detail["processing_error"] = "modelo_v2_no_configurado"
                    summary["errors"] += 1
                    details.append(detail)
                    continue

                valores = EmotionV2Parser(record).get_model_values()
                analisis = self.emotion_v2.analyze(session_id, valores)
                status = analisis.get("status")
                detail["status"] = status

                if status == ESTADO_ANALIZADO:
                    grupo = int(analisis["prediction"]["group"])
                    detail["group"] = grupo
                    summary["analyzed"] += 1
                    summary["processable"] += 1
                    summary["groups"][str(grupo)] = (
                        summary["groups"].get(str(grupo), 0) + 1
                    )
                elif status == ESTADO_DATOS_INSUFICIENTES:
                    summary["insufficient_data"] += 1
                    summary["not_processable"] += 1
                else:
                    summary["not_processable"] += 1
                    detail["processing_error"] = status
            except Exception as error:
                summary["not_processable"] += 1
                summary["errors"] += 1
                detail["processing_error"] = f"{type(error).__name__}: {error}"
            details.append(detail)

        return summary

    def _process_one(
        self,
        record: Mapping[str, Any],
        existing: dict[str, dict],
        summary: dict[str, Any],
    ) -> None:
        session_id = self._session_id(record)
        source_key = record.get("s3_key")

        # Distinguir un duplicado real (mismo archivo fuente) de un nuevo
        # conjunto de datos de la MISMA experiencia (`session_id`).
        matched_by_source = bool(source_key) and source_key in existing

        existing_item = existing.get(session_id)

        if existing_item is None and matched_by_source:
            existing_item = existing.get(source_key)

        if existing_item is not None:
            session_id = str(existing_item.get("session_id") or session_id)
            summary["already_existing"] += 1

            if matched_by_source:
                # El mismo archivo fuente ya fue procesado: duplicado real.
                summary["skipped_duplicates"] += 1
                return

            # La experiencia ya existe y llega información nueva: se
            # enriquece la MISMA sesión sin cambiar su identidad.
            self._aggregate_existing(
                record,
                session_id,
                existing_item,
                summary,
            )
            return

        try:
            parser = SessionParser(record)
            summary_data = parser.get_dashboard_summary()

            if self._has_model_input(record):
                summary["features_complete"] += 1
                summary["likely_processable"] += 1
            else:
                summary["insufficient_data"] += 1

            fields = {
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
            self._apply_v2(record, session_id)
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            self._save_error(record, session_id, source_key, existing_item, reason)
            summary["errors"] += 1
            summary["saved"] += 1

    def _aggregate_existing(
        self,
        record: Mapping[str, Any],
        session_id: str,
        existing_item: Mapping[str, Any],
        summary: dict[str, Any],
    ) -> None:
        """
        Agrega un conjunto de datos nuevo a una sesión ya persistida.

        La identidad (`session_id`) no cambia. Se fusiona el `raw_json` y se
        recalcula el análisis V2 con todos los datos disponibles.
        """

        merged_record = ImportService._merge_session_records(
            existing_item.get("raw_json"),
            record,
        )

        fields: dict[str, Any] = {"raw_json": merged_record}

        try:
            parser_summary = SessionParser(merged_record).get_dashboard_summary()
            fields.update(
                {
                    "fecha": parser_summary.get("fecha"),
                    "hora": parser_summary.get("hora"),
                    "edad": parser_summary.get("edad"),
                    "genero": parser_summary.get("genero"),
                    "difficulty": parser_summary.get("difficulty"),
                    "duracion_estimada": parser_summary.get(
                        "duracion_estimada",
                        False,
                    ),
                    "duracion": parser_summary.get("duracion"),
                }
            )
        except Exception:
            pass

        try:
            self.session_service.dynamo.update_item(
                self.session_service.table,
                {"session_id": session_id},
                fields,
            )
            summary["processed_ok"] += 1
            summary["saved"] += 1
            self._apply_v2(merged_record, session_id)
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            self._save_error(record, session_id, record.get("s3_key"), existing_item, reason)
            summary["errors"] += 1

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
        fields = {
            "status": STATUS_PROCESSING_ERROR,
            "estado": STATUS_PROCESSING_ERROR,
            "processing_error": reason[:500],
        }
        if existing_item:
            self.session_service.dynamo.update_item(
                self.session_service.table,
                {"session_id": session_id},
                fields,
            )
            return
        try:
            summary = SessionParser(record).get_dashboard_summary()
        except Exception:
            summary = {}
        self.session_service.create_session(
            session_id,
            record,
            source_key=source_key,
            status=STATUS_PROCESSING_ERROR,
            fecha=summary.get("fecha"),
            hora=summary.get("hora"),
            edad=summary.get("edad"),
            genero=summary.get("genero"),
            difficulty=summary.get("difficulty"),
            duracion_estimada=summary.get("duracion_estimada", False),
            duracion=summary.get("duracion"),
            participant_id=self._participant_id(record),
            fecha_inicio=self._session_data(record).get("fechaInicio"),
            fecha_fin=self._session_data(record).get("fechaFin"),
        )
        self.session_service.dynamo.update_item(
            self.session_service.table,
            {"session_id": session_id},
            {
                "processing_error": reason[:500],
            },
        )

    def _apply_v2(self, record: Mapping[str, Any], session_id: str) -> None:
        """Añade el análisis V2 a la sesión ya persistida.

        Es ADITIVO: nunca altera la identidad de la sesión. Un fallo del
        motor V2 se registra como `processing_error` sin revertir lo
        persistido ni invalidar la captura.
        """

        if self.emotion_v2 is None:
            return

        v2_parser = EmotionV2Parser(record)
        student_data = v2_parser.get_student_data()
        valores = v2_parser.get_model_values()
        extraccion = v2_parser.get_extraction_report()

        try:
            analisis = self.emotion_v2.analyze(session_id, valores)
        except Exception as error:
            analisis = EmotionModelV2Service.error_block(error)
            analisis["session_id"] = session_id
            analisis["measured_values"] = valores

        analisis["extraction"] = extraccion

        # Enmascaramiento binario + bienestar promedio (post-predicción).
        enriquecer_analisis(analisis)

        self.session_service.dynamo.update_item(
            self.session_service.table,
            {"session_id": session_id},
            {
                "model_processing": analisis,
                "ai_status": analisis.get("status"),
                "model_version": MODEL_VERSION_ESPERADA,
                "student_data": student_data,
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
    def _has_model_input(cls, record: Mapping[str, Any]) -> bool:
        """Indica si el registro contiene al menos una variable del contrato V2."""
        try:
            valores = EmotionV2Parser(record).get_model_values()
        except Exception:
            return False
        return any(valor is not None for valor in valores.values())

    @classmethod
    def _participant_id(cls, record: Mapping[str, Any]) -> Optional[str]:
        participant_id = cls._session_data(record).get("idParticipante")
        return str(participant_id) if participant_id else None

    @staticmethod
    def _has_analysis(item: Mapping[str, Any]) -> bool:
        model_processing = item.get("model_processing")
        return (
            isinstance(model_processing, Mapping)
            and model_processing.get("status") == ESTADO_ANALIZADO
        )

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
