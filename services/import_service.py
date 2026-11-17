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

from services.session_parser import SessionParser
from services.session_service import (
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_IDENTIFICATION,
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
        SessionService
            ↓
        DynamoDB

    El único modelo de IA vigente es V2 (K-Means congelado). La inferencia
    NO se ejecuta durante la importación: este servicio conserva las
    funcionalidades genéricas de ingesta y persistencia (JSON íntegro,
    metadatos normalizados y deduplicación por `session_id`).

    Las sesiones que todavía no tienen un participante EmotionLab
    asignado quedan en STATUS_PENDING_IDENTIFICATION.

    IMPORTANTE:
    La importación desde AWS NO ejecuta el modelo sobre registros
    históricos. El análisis V2 se realiza en el ciclo de captura
    (`CaptureService`) o mediante un reprocesamiento expresamente
    solicitado.
    """

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
    ):
        self.session_service = session_service or SessionService()

    # =========================================================
    # IMPORTACIÓN PRINCIPAL
    # =========================================================

    def import_batch(self, payload: BatchInput) -> dict:
        """
        Importa un lote de sesiones.

        El proceso:

        1. Extrae y valida las sesiones.
        2. Comprueba cuáles ya existen.
        3. Procesa únicamente las nuevas.
        4. Guarda el JSON íntegro y los metadatos normalizados.
        5. Deja la sesión pendiente de identificación de participante.

        IMPORTANTE:

        Las sesiones que ya existen en DynamoDB se omiten, y las que
        llegan como un conjunto nuevo de datos de una experiencia ya
        conocida se agregan a la MISMA sesión.
        """

        sessions = self._extract_sessions(payload)

        result = {
            "total": len(sessions),
            "new": [],
            "pending_identification": [],
            "import_errors": [],
            "skipped_existing": [],
            "aggregated": [],
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

            unity_session_id = self._get_unity_session_id(
                record
            )

            # -------------------------------------------------
            # 1. Buscar por s3_key
            # -------------------------------------------------

            existing_by_source = None

            if source_key:
                existing_by_source = existing_by_source_key.get(
                    str(source_key)
                )

            # -------------------------------------------------
            # 2. Buscar por session_id
            # -------------------------------------------------

            existing_by_session = None

            if unity_session_id:
                existing_by_session = existing_by_session_id.get(
                    unity_session_id
                )

            # -------------------------------------------------
            # 3a. MISMO ARCHIVO FUENTE
            #
            # El mismo `s3_key` ya fue importado: es un duplicado
            # real y no aporta información nueva. Se conserva el
            # comportamiento previo de omitirlo.
            # -------------------------------------------------

            if existing_by_source:

                session_id = (
                    existing_by_source.get("session_id")
                    or existing_by_source.get("id")
                    or unity_session_id
                )

                if not session_id:
                    session_id = self._session_id(
                        record
                    )

                result["skipped_existing"].append(
                    session_id
                )

                continue

            # -------------------------------------------------
            # 3b. MISMO session_id, DISTINTO CONJUNTO DE DATOS
            #
            # La experiencia ya existe pero llega un conjunto de
            # datos nuevo (otra captura de la MISMA sesión del
            # lente). Se agrega a la sesión original; nunca se crea
            # una segunda sesión EmotionLab.
            # -------------------------------------------------

            if existing_by_session:

                session_id = (
                    existing_by_session.get("session_id")
                    or existing_by_session.get("id")
                    or unity_session_id
                )

                if not session_id:
                    session_id = self._session_id(
                        record
                    )

                self._aggregate_into_session(
                    session_id,
                    record,
                    source_key,
                    result,
                )

                _track_saved(
                    session_id,
                    source_key,
                    existing_by_session.get("status")
                    or STATUS_PENDING_IDENTIFICATION,
                )

                continue

            # -------------------------------------------------
            # 4. NUEVA SESIÓN
            # -------------------------------------------------

            session_id = self._session_id(
                record
            )

            result["new"].append(
                session_id
            )

            try:

                summary = SessionParser(
                    record
                ).get_dashboard_summary()

                # ---------------------------------------------
                # 5. Guardar sesión
                #
                # IMPORTANTE:
                #
                # No utilizamos idParticipante de Unity como
                # participant_id de EmotionLab. La sesión queda
                # pendiente de identificación hasta que el
                # especialista seleccione el participante.
                # ---------------------------------------------

                self.session_service.create_session(
                    session_id,
                    record,
                    status=STATUS_PENDING_IDENTIFICATION,
                    fecha=summary.get(
                        "fecha"
                    ),
                    hora=summary.get(
                        "hora"
                    ),
                    edad=summary.get(
                        "edad"
                    ),
                    genero=summary.get(
                        "genero"
                    ),
                    difficulty=summary.get(
                        "difficulty"
                    ),
                    duracion_estimada=summary.get(
                        "duracion_estimada",
                        False,
                    ),
                    duracion=summary.get(
                        "duracion"
                    ),
                    source_key=source_key,
                )

                _track_saved(
                    session_id,
                    source_key,
                    STATUS_PENDING_IDENTIFICATION,
                )

                result[
                    "pending_identification"
                ].append(
                    session_id
                )

            # -------------------------------------------------
            # Error de importación
            # -------------------------------------------------

            except Exception as error:

                print()
                print(
                    "=============================="
                )
                print(
                    "ERROR IMPORTANDO SESIÓN"
                )
                print(
                    "Error interno de importación "
                    f"(tipo={type(error).__name__})",
                )
                print(
                    "=============================="
                )
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
                        "Error guardando sesión de importación "
                        f"(tipo={type(save_error).__name__})"
                    )

                result[
                    "import_errors"
                ].append(
                    session_id
                )

        return result

    # =========================================================
    # AGREGAR CONJUNTO DE DATOS A UNA EXPERIENCIA EXISTENTE
    # =========================================================

    # Listas de `session_data` que representan conjuntos de datos
    # acumulables dentro de la MISMA experiencia.
    DATA_SET_KEYS = (
        "respuestas",
        "eventos",
        "actividades",
    )

    @staticmethod
    def _is_blank(value: Any) -> bool:
        return (
            value is None
            or value == ""
            or value == []
            or value == {}
        )

    @staticmethod
    def _merge_session_records(
        base: Optional[Mapping[str, Any]],
        extra: Mapping[str, Any],
    ) -> dict:
        """
        Combina dos conjuntos de datos de la MISMA experiencia.

        La identidad la determina quien invoca (el `session_id` original);
        esta función solo acumula el contenido según el contrato de la
        fuente: las listas `respuestas`, `eventos` y `actividades` se
        concatenan y el resto de los campos conserva el primer valor no
        vacío. No se infieren relaciones por tiempo, orden de llegada ni
        similitud de contenido.
        """

        merged: dict[str, Any] = (
            dict(base) if isinstance(base, Mapping) else {}
        )

        incoming: dict[str, Any] = (
            dict(extra) if isinstance(extra, Mapping) else {}
        )

        base_data = merged.get("session_data")
        base_data = (
            dict(base_data)
            if isinstance(base_data, Mapping)
            else {}
        )

        new_data = incoming.get("session_data")
        new_data = (
            dict(new_data)
            if isinstance(new_data, Mapping)
            else {}
        )

        merged_data = dict(base_data)

        for key, value in new_data.items():

            if (
                key in ImportService.DATA_SET_KEYS
                and isinstance(value, list)
            ):

                current = merged_data.get(key)

                merged_data[key] = (
                    list(current)
                    if isinstance(current, list)
                    else []
                ) + list(value)

            elif ImportService._is_blank(
                merged_data.get(key)
            ):

                merged_data[key] = value

        merged["session_data"] = merged_data

        for key, value in incoming.items():

            if key == "session_data":
                continue

            if ImportService._is_blank(
                merged.get(key)
            ):

                merged[key] = value

        return merged

    def _aggregate_into_session(
        self,
        session_id: str,
        record: Mapping[str, Any],
        source_key: Optional[str],
        result: dict,
    ) -> None:
        """
        Incorpora un conjunto de datos adicional a una experiencia ya
        importada, sin crear una segunda sesión.

        La experiencia conserva su `session_id` original: el nuevo
        conjunto se fusiona con el `raw_json` almacenado. La inferencia
        NO se recalcula aquí: el análisis V2 se gestiona en el ciclo de
        captura o en un reprocesamiento solicitado.
        """

        existing = None

        try:
            existing = self.session_service.get_session(
                session_id
            )

        except Exception as error:

            print(
                "Error recuperando la experiencia "
                "para agregar datos "
                f"(tipo={type(error).__name__})"
            )

        if not existing:
            # No se pudo confirmar la sesión existente: no se altera
            # ninguna identidad y el registro se reporta como existente.
            result["skipped_existing"].append(
                session_id
            )

            return

        merged_record = self._merge_session_records(
            existing.get("raw_json"),
            record,
        )

        fields: dict[str, Any] = {}

        try:

            summary = SessionParser(
                merged_record
            ).get_dashboard_summary()

            fields.update(
                {
                    "fecha": summary.get("fecha"),
                    "hora": summary.get("hora"),
                    "edad": summary.get("edad"),
                    "genero": summary.get("genero"),
                    "difficulty": summary.get("difficulty"),
                    "duracion_estimada": summary.get(
                        "duracion_estimada",
                        False,
                    ),
                    "duracion": summary.get("duracion"),
                }
            )

        except Exception as error:

            print(
                "Error recalculando la experiencia agregada "
                f"(tipo={type(error).__name__})"
            )

        try:

            self.session_service.merge_session_data_set(
                session_id,
                merged_record,
                fields=fields,
            )

            result["aggregated"].append(
                session_id
            )

        except Exception as error:

            print(
                "Error guardando la experiencia agregada "
                f"(tipo={type(error).__name__})"
            )

            result["import_errors"].append(
                session_id
            )

    # =========================================================
    # AWS
    # =========================================================

    def import_from_aws(
        self,
        aws_service,
    ) -> dict:
        """
        Obtiene las sesiones desde AWS y las procesa.
        """

        payload = aws_service.fetch_sessions()

        return self.import_batch(
            payload
        )

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

        session_data = record.get(
            "session_data"
        )

        if isinstance(
            session_data,
            Mapping,
        ):

            sid = session_data.get(
                "session_id"
            )

            if sid:
                return str(sid)

        sid = record.get(
            "session_id"
        )

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

        if isinstance(
            payload,
            bytes,
        ):

            payload = payload.decode(
                "utf-8"
            )

        # -----------------------------------------------------
        # String → JSON
        # -----------------------------------------------------

        if isinstance(
            payload,
            str,
        ):

            try:

                payload = json.loads(
                    payload
                )

            except json.JSONDecodeError as error:

                raise ValueError(
                    "El lote de AWS no contiene JSON válido"
                ) from error

        # -----------------------------------------------------
        # Diccionario
        # -----------------------------------------------------

        if isinstance(
            payload,
            Mapping,
        ):

            sessions = payload.get(
                "sesiones"
            )

            if not isinstance(
                sessions,
                list,
            ):

                raise ValueError(
                    "El lote debe contener una lista "
                    "'sesiones'"
                )

        # -----------------------------------------------------
        # Lista
        # -----------------------------------------------------

        elif isinstance(
            payload,
            Sequence,
        ):

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

            if not isinstance(
                session,
                Mapping,
            ):

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

        source_key = record.get(
            "s3_key"
        )

        if source_key:

            source_key_text = str(
                source_key
            )

            digest = hashlib.sha256(
                source_key_text.encode(
                    "utf-8"
                )
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
                separators=(
                    ",",
                    ":",
                ),
                default=str,
            )

            digest = hashlib.sha256(
                canonical.encode(
                    "utf-8"
                )
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
