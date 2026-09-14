import hashlib
import json
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
    SessionService,
)


BatchInput = Union[
    str,
    bytes,
    Mapping[str, Any],
    Sequence[Mapping[str, Any]],
]


class ImportService:
    """
    Servicio encargado de importar sesiones provenientes de AWS/Unity.

    Flujo:

        AWS Unity
            ↓
        ImportService
            ↓
        SessionParser
            ↓
        Modelo IA
            ↓
        SessionService
            ↓
        DynamoDB

    Las sesiones que todavía no tienen un participante EmotionLab
    asignado quedan en STATUS_PENDING_IDENTIFICATION.
    """

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        predictor=None,
        prediction_service: Optional[PredictionService] = None,
    ):
        self.session_service = session_service or SessionService()
        self.prediction_service = (
            prediction_service
            or PredictionService(predictor)
        )

    # =========================================================
    # IMPORTACIÓN PRINCIPAL
    # =========================================================

    def import_batch(self, payload: BatchInput) -> dict:
        """
        Importa un lote de sesiones.

        El proceso:

        1. Extrae y valida las sesiones.
        2. Comprueba cuáles ya existen.
        3. Procesa las nuevas.
        4. Ejecuta el modelo IA cuando existen suficientes datos.
        5. Guarda el resultado en DynamoDB.
        6. Deja la sesión pendiente de identificación de participante.
        """

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

        # -----------------------------------------------------
        # Índice de sesiones existentes
        # -----------------------------------------------------

        existing_by_source_key, existing_by_session_id = (
            self.session_service.build_import_index()
        )

        def _track_saved(
            sid: str,
            source_key: Optional[str],
            status: str,
        ) -> None:
            saved = {
                "session_id": sid,
                "source_key": source_key,
                "status": status,
            }

            if source_key:
                existing_by_source_key[source_key] = saved

            existing_by_session_id[sid] = saved

        # -----------------------------------------------------
        # Procesar cada sesión
        # -----------------------------------------------------

        for record in sessions:
            source_key = record.get("s3_key")

            unity_session_id = self._get_unity_session_id(record)

            # -------------------------------------------------
            # 1. Buscar por s3_key
            # -------------------------------------------------

            existing = None

            if source_key:
                existing = existing_by_source_key.get(
                    str(source_key)
                )

            # -------------------------------------------------
            # 2. Si no existe, buscar por session_id
            # -------------------------------------------------

            if existing is None and unity_session_id:
                existing = existing_by_session_id.get(
                    unity_session_id
                )

            # -------------------------------------------------
            # 3. SESIÓN YA EXISTENTE
            # -------------------------------------------------

            if existing:
                existing_status = (
                    existing.get("status")
                    or existing.get("estado")
                )

                session_id = (
                    existing.get("session_id")
                    or existing.get("id")
                    or unity_session_id
                )

                if not session_id:
                    session_id = self._session_id(record)

                # ---------------------------------------------
                # Reintentar una predicción pendiente
                # ---------------------------------------------

                if existing_status == STATUS_PENDING_PREDICTION:
                    retry_result = self._retry_prediction(
                        session_id,
                        record,
                    )

                    if retry_result == "predicted":
                        result["retried_pending"].append(
                            session_id
                        )
                        result["predicted"].append(
                            session_id
                        )

                    elif retry_result == "pending":
                        result["pending_prediction"].append(
                            session_id
                        )

                    else:
                        result["import_errors"].append(
                            session_id
                        )

                    continue

                # ---------------------------------------------
                # Ya procesada
                # ---------------------------------------------

                result["skipped_existing"].append(
                    session_id
                )

                continue

            # -------------------------------------------------
            # 4. NUEVA SESIÓN
            # -------------------------------------------------

            session_id = self._session_id(record)

            result["new"].append(session_id)

            parser = None

            try:
                # ---------------------------------------------
                # Crear parser
                # ---------------------------------------------

                parser = SessionParser(record)

                prediction_status = (
                    parser.get_prediction_status()
                )

                # ---------------------------------------------
                # 5. Datos insuficientes para IA
                # ---------------------------------------------

                if not prediction_status["can_predict"]:
                    self._create_pending_prediction(
                        session_id,
                        record,
                        result,
                        parser,
                        prediction_status,
                    )

                    _track_saved(
                        session_id,
                        source_key,
                        STATUS_PENDING_PREDICTION,
                    )

                    continue

                # ---------------------------------------------
                # 6. Obtener features
                # ---------------------------------------------

                features = parser.get_prediction_features()

                # ---------------------------------------------
                # 7. Ejecutar modelo IA
                # ---------------------------------------------

                prediction = self._predict(features)

                # ---------------------------------------------
                # 8. Obtener información para dashboard
                # ---------------------------------------------

                summary = parser.get_dashboard_summary()

                # ---------------------------------------------
                # 9. Guardar sesión
                #
                # IMPORTANTE:
                #
                # No utilizamos idParticipante de Unity como
                # participant_id de EmotionLab.
                #
                # La sesión queda pendiente de identificación
                # hasta que el especialista seleccione el
                # participante correcto.
                # ---------------------------------------------

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
                    duracion_estimada=summary.get(
                        "duracion_estimada",
                        False,
                    ),
                    source_key=source_key,
                )

                _track_saved(
                    session_id,
                    source_key,
                    STATUS_PENDING_IDENTIFICATION,
                )

                result["pending_identification"].append(
                    session_id
                )

                result["predicted"].append(
                    session_id
                )

            # -------------------------------------------------
            # Error controlado de datos
            # -------------------------------------------------

            except ValueError as error:
                self._create_pending_prediction(
                    session_id,
                    record,
                    result,
                    parser,
                    {
                        "can_predict": False,
                        "missing_fields": [],
                        "reason": str(error),
                    },
                )

            # -------------------------------------------------
            # Error inesperado
            # -------------------------------------------------

            except Exception as error:
                print()
                print("==============================")
                print("ERROR IMPORTANDO SESIÓN")
                print("session_id:", session_id)
                print("s3_key:", source_key)
                print("ERROR:", error)
                print("==============================")
                print()

                try:
                    self.session_service.create_session(
                        session_id,
                        record,
                        status=STATUS_IMPORT_ERROR,
                        source_key=source_key,
                    )
                except Exception as save_error:
                    print(
                        "ERROR GUARDANDO SESIÓN CON ERROR:",
                        save_error,
                    )

                result["import_errors"].append(
                    session_id
                )

        return result

    # =========================================================
    # REINTENTAR PREDICCIÓN
    # =========================================================

    def _retry_prediction(
        self,
        session_id: str,
        record: Mapping[str, Any],
    ) -> str:
        """
        Reintenta la predicción de una sesión que quedó
        pendiente.
        """

        try:
            parser = SessionParser(record)

            prediction_status = (
                parser.get_prediction_status()
            )

            # ---------------------------------------------
            # Todavía faltan datos
            # ---------------------------------------------

            if not prediction_status["can_predict"]:
                self.session_service.update_document_if_supported(
                    session_id,
                    {
                        "prediction_error": (
                            prediction_status["reason"]
                        ),
                        "prediction_missing_fields": (
                            prediction_status[
                                "missing_fields"
                            ]
                        ),
                    },
                )

                return "pending"

            # ---------------------------------------------
            # Obtener features
            # ---------------------------------------------

            features = parser.get_prediction_features()

            # ---------------------------------------------
            # Ejecutar IA
            # ---------------------------------------------

            prediction = self._predict(features)

            # ---------------------------------------------
            # Información dashboard
            # ---------------------------------------------

            summary = parser.get_dashboard_summary()

            # ---------------------------------------------
            # Guardar predicción
            # ---------------------------------------------

            self.session_service.update_prediction(
                session_id,
                prediction,
            )

            self.session_service.update_session_fields(
                session_id,
                {
                    "fecha": summary.get("fecha"),
                    "hora": summary.get("hora"),
                    "edad": summary.get("edad"),
                    "genero": summary.get("genero"),
                    "difficulty": summary.get(
                        "difficulty"
                    ),
                    "duracion_estimada": summary.get(
                        "duracion_estimada",
                        False,
                    ),
                    "prediction_error": None,
                    "prediction_missing_fields": [],
                },
            )

            return "predicted"

        except Exception as error:
            print(
                f"Error reintentando predicción "
                f"{session_id}: {error}"
            )

            return "error"

    # =========================================================
    # CREAR SESIÓN PENDIENTE DE PREDICCIÓN
    # =========================================================

    def _create_pending_prediction(
        self,
        session_id: str,
        record: Mapping[str, Any],
        result: dict,
        parser: Optional[SessionParser] = None,
        prediction_status: Optional[dict] = None,
    ) -> None:
        """
        Guarda una sesión cuando todavía no existen suficientes
        datos para ejecutar el modelo IA.
        """

        try:
            active_parser = (
                parser
                or SessionParser(record)
            )

            summary = (
                active_parser.get_dashboard_summary()
            )

            if prediction_status is None:
                prediction_status = (
                    active_parser.get_prediction_status()
                )

            # ---------------------------------------------
            # Crear sesión pendiente
            # ---------------------------------------------

            self.session_service.create_session(
                session_id,
                record,
                status=STATUS_PENDING_PREDICTION,
                fecha=summary.get("fecha"),
                hora=summary.get("hora"),
                edad=summary.get("edad"),
                genero=summary.get("genero"),
                difficulty=summary.get("difficulty"),
                duracion_estimada=summary.get(
                    "duracion_estimada",
                    False,
                ),
                source_key=record.get("s3_key"),
            )

            # ---------------------------------------------
            # Guardar información faltante
            # ---------------------------------------------

            self.session_service.update_prediction_pending_info(
                session_id,
                prediction_status.get(
                    "missing_fields",
                    [],
                ),
            )

            result["pending_prediction"].append(
                session_id
            )

        except Exception as error:
            print(
                f"Error creando sesión pendiente: {error}"
            )

            # ---------------------------------------------
            # Intentar registrar el error de importación
            # ---------------------------------------------

            try:
                self.session_service.create_session(
                    session_id,
                    record,
                    status=STATUS_IMPORT_ERROR,
                    source_key=record.get("s3_key"),
                )
            except Exception as save_error:
                print(
                    "Error guardando estado de importación:",
                    save_error,
                )

            result["import_errors"].append(
                session_id
            )

    # =========================================================
    # PREDICCIÓN
    # =========================================================

    def _predict(self, features: dict) -> dict:
        """
        Ejecuta el servicio de predicción.
        """

        return self.prediction_service.predict(
            features
        )

    # =========================================================
    # AWS
    # =========================================================

    def import_from_aws(self, aws_service) -> dict:
        """
        Obtiene las sesiones desde AWS y las procesa.
        """

        payload = aws_service.fetch_sessions()

        return self.import_batch(payload)

    # =========================================================
    # EXTRAER SESSION_ID DE UNITY
    # =========================================================

    @staticmethod
    def _get_unity_session_id(
        record: Mapping[str, Any],
    ) -> Optional[str]:
        """
        Obtiene un session_id proporcionado explícitamente
        por Unity, si existe.

        NO utiliza idParticipante como session_id.
        """

        session_data = record.get("session_data")

        if isinstance(session_data, Mapping):
            sid = session_data.get("session_id")

            if sid:
                return str(sid)

        sid = record.get("session_id")

        if sid:
            return str(sid)

        return None

    # =========================================================
    # EXTRAER LOTE
    # =========================================================

    @staticmethod
    def _extract_sessions(
        payload: BatchInput,
    ) -> list[dict]:
        """
        Convierte las diferentes formas de entrada en una
        lista normalizada de diccionarios.
        """

        # -----------------------------------------------------
        # Bytes → string
        # -----------------------------------------------------

        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")

        # -----------------------------------------------------
        # String → JSON
        # -----------------------------------------------------

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)

            except json.JSONDecodeError as error:
                raise ValueError(
                    "El lote de AWS no contiene JSON válido"
                ) from error

        # -----------------------------------------------------
        # Diccionario
        # -----------------------------------------------------

        if isinstance(payload, Mapping):
            sessions = payload.get("sesiones")

            if not isinstance(sessions, list):
                raise ValueError(
                    "El lote debe contener una lista "
                    "'sesiones'"
                )

        # -----------------------------------------------------
        # Lista
        # -----------------------------------------------------

        elif isinstance(payload, Sequence):
            sessions = payload

        # -----------------------------------------------------
        # Tipo inválido
        # -----------------------------------------------------

        else:
            raise ValueError(
                "El lote debe ser un objeto JSON "
                "o una lista de sesiones"
            )

        # -----------------------------------------------------
        # Normalizar sesiones
        # -----------------------------------------------------

        normalized_sessions: list[dict] = []

        for session in sessions:
            if not isinstance(session, Mapping):
                raise ValueError(
                    "Cada entrada del lote debe ser "
                    "un objeto JSON"
                )

            normalized_sessions.append(
                dict(
                    cast(
                        Mapping[str, Any],
                        session,
                    )
                )
            )

        return normalized_sessions

    # =========================================================
    # ID INTERNO
    # =========================================================

    @staticmethod
    def _session_id(
        record: Mapping[str, Any],
    ) -> str:
        """
        Genera un identificador estable para la sesión.

        Prioridad:

        1. session_id entregado por Unity.
        2. Hash estable del s3_key.
        3. Hash estable del contenido de la sesión.

        Esto evita que la misma sesión reciba un UUID diferente
        cada vez que se ejecute la importación.
        """

        # -----------------------------------------------------
        # 1. Unity ya entrega session_id
        # -----------------------------------------------------

        unity_id = ImportService._get_unity_session_id(
            record
        )

        if unity_id:
            return unity_id

        # -----------------------------------------------------
        # 2. Usar s3_key como identificador estable
        # -----------------------------------------------------

        source_key = record.get("s3_key")

        if source_key:
            source_key_text = str(source_key)

            digest = hashlib.sha256(
                source_key_text.encode("utf-8")
            ).hexdigest()[:24]

            return f"session_{digest}"

        # -----------------------------------------------------
        # 3. Último recurso: hash del contenido
        # -----------------------------------------------------

        try:
            canonical = json.dumps(
                record,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )

            digest = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()[:24]

            return f"session_{digest}"

        except Exception:
            # Este caso debería ser extremadamente raro.
            # No usamos uuid4 porque queremos mantener el ID
            # estable siempre que sea posible.
            raise ValueError(
                "No fue posible generar un session_id "
                "estable para la sesión"
            )