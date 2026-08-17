import json
from copy import deepcopy
from uuid import uuid4
from typing import (
    Any,
    Mapping,
    Optional,
    Sequence,
    Union,
    cast,
)

from services.prediction_service import PredictionService
from services.session_parser import SessionParser
from services.session_service import (
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_IDENTIFICATION,
    STATUS_PENDING_PREDICTION,
    STATUS_PREDICTED,
    SessionService,
)

BatchInput = Union[
    str,
    bytes,
    Mapping[str, Any],
    Sequence[Mapping[str, Any]],
]


class ImportService:

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        predictor=None,
        prediction_service: Optional[PredictionService] = None,
    ):
        self.session_service = session_service or SessionService()
        self.prediction_service = prediction_service or PredictionService(predictor)

    # =========================================================
    # IMPORTACIÓN PRINCIPAL
    # =========================================================

    def import_batch(self, payload: BatchInput) -> dict:
        sessions = self._extract_sessions(payload)

        result = {
            "total": len(sessions),
            "new": [],
            "predicted": [],
            "pending_identification": [],
            "pending_prediction": [],
            "import_errors": [],
            "skipped_existing": [],
            "retried_pending": [],
        }

        # Evita el patrón N+1: una sincronización consulta las sesiones
        # existentes una vez y reutiliza ese índice para todo el lote AWS.
        existing_by_source_key, existing_by_session_id = self.session_service.build_import_index()

        def _track_saved(sid: str, skey: Optional[str], st: str):
            saved = {"session_id": sid, "source_key": skey, "status": st}
            if skey:
                existing_by_source_key[skey] = saved
            existing_by_session_id[sid] = saved

        for record in sessions:
            source_key = record.get("s3_key")
            unity_session_id = self._get_unity_session_id(record)

            # -------------------------------------------------
            # 1. Buscar por s3_key
            # -------------------------------------------------
            existing = existing_by_source_key.get(source_key) if source_key else None

            # -------------------------------------------------
            # 2. Si no encontramos por s3_key,
            #    buscar por session_id
            # -------------------------------------------------
            if existing is None and unity_session_id:
                existing = existing_by_session_id.get(unity_session_id)

            # -------------------------------------------------
            # 3. SESIÓN YA EXISTENTE
            # -------------------------------------------------
            if existing:
                existing_status = existing.get("status") or existing.get("estado")
                session_id = existing.get("session_id") or existing.get("id") or unity_session_id

                # ---------------------------------------------
                # Si está pendiente de predicción,
                # volver a intentar
                # ---------------------------------------------
                if existing_status == STATUS_PENDING_PREDICTION:
                    retry_result = self._retry_prediction(session_id, record)

                    if retry_result == "predicted":
                        result["retried_pending"].append(session_id)
                        result["predicted"].append(session_id)
                    elif retry_result == "pending":
                        result["pending_prediction"].append(session_id)
                    else:
                        result["import_errors"].append(session_id)
                    continue

                # ---------------------------------------------
                # Si ya existe y no está pendiente,
                # NO tocarla
                # ---------------------------------------------
                result["skipped_existing"].append(session_id)
                continue

            # -------------------------------------------------
            # 4. NUEVA SESIÓN
            # -------------------------------------------------
            session_id = self._session_id(record)
            result["new"].append(session_id)

            try:
                parser = SessionParser(record)
                prediction_status = parser.get_prediction_status()

                # -------------------------------------------------
                # 5. Sesión incompleta
                # -------------------------------------------------
                if not prediction_status["can_predict"]:
                    self._create_pending_prediction(
                        session_id, record, result, parser, prediction_status
                    )
                    _track_saved(session_id, source_key, STATUS_PENDING_PREDICTION)
                    continue

                # -------------------------------------------------
                # 6. Obtener features
                # -------------------------------------------------
                features = parser.get_prediction_features()

                # -------------------------------------------------
                # 7. Ejecutar modelo
                # -------------------------------------------------
                prediction = self._predict(features)

                # -------------------------------------------------
                # 8. Datos del dashboard
                # -------------------------------------------------
                summary = parser.get_dashboard_summary()

                # -------------------------------------------------
                # 9. Guardar sesión
                # -------------------------------------------------
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
                    duracion_estimada=summary.get("duracion_estimada", False),
                    source_key=source_key,
                )

                _track_saved(session_id, source_key, STATUS_PENDING_IDENTIFICATION)
                result["pending_identification"].append(session_id)
                result["predicted"].append(session_id)

            except ValueError as error:
                self._create_pending_prediction(
                    session_id,
                    record,
                    result,
                    parser if "parser" in locals() else None,
                    {
                        "can_predict": False,
                        "missing_fields": [],
                        "reason": str(error),
                    },
                )

            except Exception as error:
                print("\n==============================")
                print("ERROR IMPORTANDO SESIÓN")
                print("session_id:", session_id)
                print("s3_key:", source_key)
                print("ERROR:", error)
                print("==============================\n")

                self.session_service.create_session(
                    session_id,
                    record,
                    status=STATUS_IMPORT_ERROR,
                    source_key=source_key,
                )
                result["import_errors"].append(session_id)

        return result

    # =========================================================
    # REINTENTAR PREDICCIÓN
    # =========================================================

    def _retry_prediction(self, session_id: str, record: Mapping[str, Any]) -> str:
        try:
            parser = SessionParser(record)
            prediction_status = parser.get_prediction_status()

            # -------------------------------------------------
            # Sigue incompleta
            # -------------------------------------------------
            if not prediction_status["can_predict"]:
                self.session_service.update_document_if_supported(
                    session_id,
                    {
                        "prediction_error": prediction_status["reason"],
                        "prediction_missing_fields": prediction_status["missing_fields"],
                    },
                )
                return "pending"

            # -------------------------------------------------
            # Ya está completa
            # -------------------------------------------------
            features = parser.get_prediction_features()
            prediction = self._predict(features)
            summary = parser.get_dashboard_summary()

            self.session_service.update_prediction(session_id, prediction)
            self.session_service.update_session_fields(
                session_id,
                {
                    "fecha": summary.get("fecha"),
                    "hora": summary.get("hora"),
                    "edad": summary.get("edad"),
                    "genero": summary.get("genero"),
                    "difficulty": summary.get("difficulty"),
                    "duracion_estimada": summary.get("duracion_estimada", False),
                    "prediction_error": None,
                    "prediction_missing_fields": [],
                },
            )
            return "predicted"

        except Exception as error:
            print(f"Error reintentando predicción {session_id}: {error}")
            return "error"

    # =========================================================
    # CREAR PENDIENTE
    # =========================================================

    def _create_pending_prediction(
        self,
        session_id: str,
        record: Mapping[str, Any],
        result: dict,
        parser: Optional[SessionParser] = None,
        prediction_status: Optional[dict] = None,
    ) -> None:
        try:
            parser = parser or SessionParser(record)
            summary = parser.get_dashboard_summary()

            if prediction_status is None:
                prediction_status = parser.get_prediction_status()

            self.session_service.create_session(
                session_id,
                record,
                status=STATUS_PENDING_PREDICTION,
                fecha=summary.get("fecha"),
                hora=summary.get("hora"),
                edad=summary.get("edad"),
                genero=summary.get("genero"),
                difficulty=summary.get("difficulty"),
                duracion_estimada=summary.get("duracion_estimada", False),
                source_key=record.get("s3_key"),
            )

            self.session_service.update_prediction_pending_info(
                session_id,
                prediction_status.get("missing_fields", []),
            )
            result["pending_prediction"].append(session_id)

        except Exception as error:
            print(f"Error creando sesión pendiente: {error}")
            self.session_service.create_session(
                session_id,
                record,
                status=STATUS_IMPORT_ERROR,
                source_key=record.get("s3_key"),
            )
            result["import_errors"].append(session_id)

    # =========================================================
    # PREDICCIÓN
    # =========================================================

    def _predict(self, features: dict) -> dict:
        return self.prediction_service.predict(features)

    # =========================================================
    # AWS
    # =========================================================

    def import_from_aws(self, aws_service) -> dict:
        payload = aws_service.fetch_sessions()
        return self.import_batch(payload)

    # =========================================================
    # EXTRAER SESSION_ID DE UNITY
    # =========================================================

    @staticmethod
    def _get_unity_session_id(record: Mapping[str, Any]) -> Optional[str]:
        session_data = record.get("session_data")
        if isinstance(session_data, Mapping) and (sid := session_data.get("session_id")):
            return str(sid)

        if sid := record.get("session_id"):
            return str(sid)

        return None

    # =========================================================
    # EXTRAER LOTE
    # =========================================================

    @staticmethod
    def _extract_sessions(payload: BatchInput) -> list[dict]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as error:
                raise ValueError("El lote de AWS no contiene JSON válido") from error

        if isinstance(payload, Mapping):
            sessions = payload.get("sesiones")
            if not isinstance(sessions, list):
                raise ValueError("El lote debe contener una lista 'sesiones'")
        elif isinstance(payload, Sequence):
            sessions = payload
        else:
            raise ValueError("El lote debe ser un objeto JSON o una lista de sesiones")

        normalized_sessions = []
        for session in sessions:
            if not isinstance(session, Mapping):
                raise ValueError("Cada entrada del lote debe ser un objeto JSON")
            normalized_sessions.append(deepcopy(dict(cast(Mapping[str, Any], session))))

        return normalized_sessions

    # =========================================================
    # ID INTERNO
    # =========================================================

    @staticmethod
    def _session_id(record: Mapping[str, Any]) -> str:
        # Primero conservar el ID de Unity
        if unity_id := ImportService._get_unity_session_id(record):
            return unity_id

        # Solo si Unity no entregó ID, generar uno nuevo.
        return f"session_{uuid4().hex}"