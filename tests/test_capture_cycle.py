"""Pruebas del ciclo de vida de una experiencia de captura de EmotionLab.

Cubre las verificaciones obligatorias del flujo de captura:

    - inicio y generación de identificadores (una única experiencia activa
      global, `capture_started_at` en UTC, idempotencia por token)
    - validación del PIN contra la fuente autorizada del lente
    - finalización y bloqueo de capturas (`capture_ended_at` fijo)
    - extracción desde AWS al finalizar, con filtro temporal
      (`started_at <= timestamp <= ended_at`), paginación de `ListObjectsV2`,
      aislamiento por prefijo y descarte de archivos no JSON
    - centralización de múltiples registros de Unity bajo un `session_id`
    - idempotencia por `s3_key`, registro de errores y reintentos
    - privacidad, seudonimización y autorización por propietario

El `session_id` es un UUID generado por EmotionLab. El PIN (`display_code`)
lo genera la Lambda del lente y llega dentro de los registros; el backend
solo lo valida contra la fuente autorizada y nunca lo genera ni lo usa como
identidad permanente.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask, jsonify

from core.constants import (
    AUDIT_CAPTURE_FINALIZING,
    AUDIT_CAPTURE_IMPORTED,
    AUDIT_CAPTURE_IMPORT_FAILED,
    AUDIT_CAPTURE_RECORD_REJECTED,
    AUDIT_CAPTURE_STARTED,
    DYNAMODB_TABLE_SESSIONS,
    PK_TYPE_CAPTURE_LOCK,
    PK_TYPE_SESSION,
)
from core.exceptions import AuthenticationError, AuthorizationError
from core.roles import ROLE_TEACHER
from routes.capture_routes import captures_bp
from services.capture_service import (
    CaptureConfirmationPending,
    CaptureNotAuthorized,
    CaptureService,
    DeviceBusy,
    InvalidDisplayCode,
    SessionClosed,
)
from services.emotion_model_v8_service import EmotionModelV8Service
from services.parsers.emotion_v8_parser import EmotionV8Parser
from services.lens_session_service import (
    LensApiConfirmationAmbiguous,
    LensApiUnavailable,
    LensSessionExpired,
    LensSessionNotFound,
    LensSessionService,
)

STATUS_ANALYZED = "analyzed"
STATUS_PROCESSING_ERROR = "processing_error"
from services.s3_service import S3Service
from services.unity_data_service import (
    STATUS_AMBIGUOUS as UNITY_STATUS_AMBIGUOUS,
    STATUS_MATCHED as UNITY_STATUS_MATCHED,
    UnityDataService,
)
from services.session_service import (
    PENDING_PARTICIPANT_ID,
    STATUS_CAPTURED,
    STATUS_CAPTURE_IMPORT_ERROR,
    STATUS_CAPTURING,
    STATUS_FINALIZING,
    STATUS_IMPORTING,
    SessionService,
)


# PIN que genera la Lambda del lente. Puede repetirse.
VALID_CODE = "482731"
# `sessionId` original que genera la Lambda `createSession`. Es la identidad
# ORIGINAL de la captura del lente; nunca reemplaza al `session_id`.
LENS_SESSION_ID = "6f1c0b9e-0000-4000-8000-000000000001"
# Base del API Gateway de los lentes (se sustituye por un doble HTTP).
LENS_BASE_URL = "https://lens.example/prod"


# ==========================================================
# DOBLES DE PRUEBA
# ==========================================================


class FakeRepository:
    """Persistencia en memoria compatible con `StorageRepository`."""

    @staticmethod
    def _condition_parts(expression):
        values = getattr(expression, "_values", ())
        if len(values) == 2:
            attribute, expected = values
            name = getattr(attribute, "name", None)
            if name:
                return name, expected
        return None, None

    def __init__(self):
        self.items = {}

    def _store_key(self, table_name, item):
        return (table_name, item.get("participant_id"), item.get("sk"))

    def put_item(self, table_name, item, condition_expression=None, **kwargs):
        key = self._store_key(table_name, item)
        if condition_expression is not None and key in self.items:
            # Escritura condicional fallida: el item ya existe.
            return False
        self.items[key] = dict(item)
        return True

    def get_item(self, table_name, key, consistent_read=False):
        composite = (
            table_name,
            key.get("participant_id"),
            key.get("sk"),
        )
        item = self.items.get(composite)
        return dict(item) if item else None

    def update_item(
        self,
        table_name,
        key,
        update_data=None,
        condition_expression=None,
        expression_attribute_names=None,
        expression_attribute_values=None,
        **kwargs,
    ):
        composite = (
            table_name,
            key.get("participant_id"),
            key.get("sk"),
        )
        item = self.items.get(composite)
        if item is None:
            return False
        if condition_expression is not None:
            values = expression_attribute_values or {}
            # Condición sobre el estado de la sesión (`status = :cap_expected`).
            if (
                ":cap_expected" in values
                and item.get("status") != values[":cap_expected"]
            ):
                return False
            # Condición sobre el cerrojo (`active_session_id = :expected`).
            if (
                ":expected" in values
                and item.get("active_session_id") != values[":expected"]
            ):
                return False
        item.update(dict(update_data or {}))
        return True

    def delete_item(
        self,
        table_name,
        key,
        condition_expression=None,
        expression_attribute_values=None,
        expression_attribute_names=None,
        **kwargs,
    ):
        composite = self._store_key(table_name, key)
        item = self.items.get(composite)
        if condition_expression is not None:
            values = expression_attribute_values or {}
            names = expression_attribute_names or {}
            expected = values.get(":expected")
            if item is None:
                return False
            if expected is not None and item.get("active_session_id") != expected:
                return False
            if ":owner" in values and item.get(names.get("#owner", "owner_id")) != values[":owner"]:
                return False
            if ":kind" in values and item.get(names.get("#kind", "pk_type")) != values[":kind"]:
                return False
            for token, attribute in names.items():
                if f"attribute_not_exists({token})" in condition_expression and attribute in item:
                    return False
        self.items.pop(composite, None)
        return True

    def scan_all(self, table_name, **kwargs):
        return [
            dict(value)
            for (table, _p, _s), value in self.items.items()
            if table == table_name
        ]

    def query_all(
        self,
        table_name,
        key_condition_expression,
        index_name=None,
        max_items=None,
        **kwargs,
    ):
        name, expected = self._condition_parts(key_condition_expression)
        results = []
        for (table, _p, _s), value in self.items.items():
            if table != table_name:
                continue
            if name is not None and value.get(name) != expected:
                continue
            results.append(dict(value))
        return results[:max_items] if max_items else results


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_session_raw_json(self, key, data):
        self.objects[key] = data

    def get_session_raw_json(self, key):
        return self.objects.get(key)


class FakeAWS:
    """Simula el mecanismo real de recolección (`AWS_UNITY_DATA_URL`).

    `AWSService.fetch_sessions()` devuelve `{"sesiones": [...]}` con los
    registros/objetos disponibles tras finalizar la experiencia.
    """

    def __init__(self, sessions=None, error=None):
        self.sessions = list(sessions or [])
        self.error = error
        self.calls = 0

    def fetch_sessions(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"sesiones": [dict(item) for item in self.sessions]}


def aws_record(unity_session_id="AWS-001", captured_at=None, s3_key=None):
    """Registro tal como lo entrega la extracción desde AWS."""

    item = {"unity_session_id": unity_session_id}
    if captured_at is not None:
        item["captured_at"] = captured_at
    if s3_key is not None:
        item["s3_key"] = s3_key
    item["session_data"] = {
        "respuestas": [{"idPregunta": "pregunta_demo", "respuestaLabel": "5"}]
    }
    return item


class FakeUnityData:
    """Doble de `UnityDataService` para el flujo de captura.

    `fetch_records` devuelve los registros de `GET /data` (o lanza el error
    configurado). La correlación se delega en la implementación REAL de
    `select_candidate`, de modo que las reglas (participante / intersección
    temporal) se ejercen de verdad y no se duplican en el test.
    """

    def __init__(self, records=None, error=None, local_timezone="America/Santiago"):
        self.records = list(records or [])
        self.error = error
        self.calls = 0
        self._logic = UnityDataService(
            base_url="https://lens.example/prod",
            local_timezone=local_timezone,
            http_client=object(),
        )

    def fetch_records(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.records]

    def select_candidate(self, records, **kwargs):
        return self._logic.select_candidate(records, **kwargs)


def lens_data_record(
    *,
    participant="participant-test-001",
    s3_key="lens/participante_20300115_220926_participant-test-001.json",
    subida="2030-01-16T01:09:26Z",
    fecha_inicio="2030-01-15 22:09:26",
    fecha_fin="2030-01-15 22:20:00",
    respuestas=None,
    eventos=None,
):
    """Registro tal como lo entrega `GET /data` del API Gateway del lente."""

    if respuestas is None:
        respuestas = [
            {"idPregunta": f"p{i}", "respuestaLabel": str(i)}
            for i in range(14)
        ]
    if eventos is None:
        eventos = [{"tipo": f"evento_{i}"} for i in range(11)]

    return {
        "s3_key": s3_key,
        "timestamp_subida": subida,
        "session_data": {
            "idParticipante": participant,
            "fechaInicio": fecha_inicio,
            "fechaFin": fecha_fin,
            "respuestas": respuestas,
            "eventos": eventos,
        },
    }


class FakeS3Bucket:
    """Bucket S3 en memoria para el listado y la descarga directa."""

    def __init__(self, objects=None):
        # objects: {key: {"body": bytes, "last_modified": Any, "etag": str}}
        self.objects = dict(objects or {})
        self.list_calls = []
        self.get_calls = []

    def add(self, key, body, last_modified=None, etag=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.objects[key] = {
            "body": body,
            "last_modified": last_modified,
            "etag": etag,
        }

    def list_objects_v2(self, bucket, prefix="", *, suffix=None):
        self.list_calls.append({"bucket": bucket, "prefix": prefix})
        results = []
        for key, payload in self.objects.items():
            if prefix and not key.startswith(prefix):
                continue
            if suffix and not key.lower().endswith(suffix.lower()):
                continue
            results.append(
                {
                    "key": key,
                    "last_modified": payload.get("last_modified"),
                    "size": len(payload.get("body", b"")),
                    "etag": payload.get("etag"),
                }
            )
        return results

    def get_object(self, bucket, key):
        self.get_calls.append(key)
        payload = self.objects.get(key)
        if payload is None:
            raise KeyError(f"Objeto inexistente: {key}")
        if payload.get("error"):
            raise RuntimeError("Fallo simulado de descarga")
        return payload["body"]


class FakeListClient:
    """Cliente boto3 S3 simulado que devuelve páginas predefinidas."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list_objects_v2(self, **params):
        self.calls.append(params)
        index = int(params.get("ContinuationToken", 0) or 0)
        return self.pages[index]


class FakeAudit:
    def __init__(self):
        self.events = []

    def log(self, action, **kwargs):
        self.events.append({"action": action, **kwargs})

    def actions(self):
        return [event["action"] for event in self.events]


class FakeLens:
    """Doble del API Gateway del lente (`/session/confirm`).

    El mapping PIN -> captura es mutable para simular que el PIN se reutiliza
    en una captura nueva. Registra las validaciones (`validateOnly = true`) y
    las confirmaciones (`validateOnly = false`).
    """

    def __init__(self, mapping=None, participants=None):
        self.mapping = dict(mapping or {VALID_CODE: LENS_SESSION_ID})
        # display_code -> idParticipante emitido por la Lambda al confirmar.
        self.participants = dict(participants or {})
        self.lookups = []
        self.confirmations = []
        self.failed_attempts = {}
        self.failed_requests = {}
        self.rate_check_error = None
        self.rate_register_error = None
        # Si se define, `confirm_display_code` la lanza (p. ej. para simular
        # un timeout ambiguo).
        self.confirm_error = None

    def _view(self, display_code, lens_session_id, status):
        view = {
            "session_id": lens_session_id,
            "display_code": display_code,
            "capture_status": status,
            "created_at": "2030-01-15T10:00:00",
        }
        participant = self.participants.get(display_code)
        if participant is not None:
            view["participant_id"] = participant
        return view

    def _resolve(self, display_code):
        lens_session_id = self.mapping.get(display_code)
        if not lens_session_id:
            raise LensSessionNotFound(
                "El código no corresponde a una sesión vigente."
            )
        return lens_session_id

    def validate_display_code(self, display_code):
        self.lookups.append(display_code)
        return self._view(
            display_code,
            self._resolve(display_code),
            "pending",
        )

    def confirm_display_code(
        self,
        display_code,
        specialist_id,
        confirmation_request_id=None,
        patient_id="",
    ):
        self.confirmations.append(
            {
                "display_code": display_code,
                "specialist_id": specialist_id,
                "confirmation_request_id": confirmation_request_id,
            }
        )
        if self.confirm_error is not None:
            raise self.confirm_error
        return self._view(
            display_code,
            self._resolve(display_code),
            "confirmed",
        )

    def lookup_by_display_code(self, display_code):
        return self.validate_display_code(display_code)

    def check_capture_attempt(self, *, client_ip, actor_uid, request_token):
        if self.rate_check_error is not None:
            raise self.rate_check_error
        request_key = (actor_uid, request_token)
        if request_token and request_key in self.failed_requests:
            return self.failed_requests[request_key]
        if self.failed_attempts.get(client_ip, 0) >= 5:
            from services.lens_session_service import SessionCodeRateLimitExceeded
            raise SessionCodeRateLimitExceeded("límite")
        return None

    def register_capture_failure(
        self, *, client_ip, actor_uid, request_token, failure_code
    ):
        if self.rate_register_error is not None:
            raise self.rate_register_error
        request_key = (actor_uid, request_token)
        if request_token and request_key in self.failed_requests:
            return False
        self.failed_attempts[client_ip] = self.failed_attempts.get(client_ip, 0) + 1
        if request_token:
            self.failed_requests[request_key] = failure_code
        return True

    def reset_capture_attempts(self, client_ip):
        self.failed_attempts.pop(client_ip, None)


class FakeAuthService:
    def __init__(self, uid="teacher-1", rol=ROLE_TEACHER):
        self.uid = uid
        self.rol = rol

    def verify_token(self, token):
        return {
            "sub": self.uid,
            "email": f"{self.uid}@example.com",
            "rol": self.rol,
        }

    def get_current_user(self, token):
        return {
            "uid": self.uid,
            "email": f"{self.uid}@example.com",
            "rol": self.rol,
        }


# ==========================================================
# HELPERS
# ==========================================================


def actor(uid="teacher-1", rol=ROLE_TEACHER):
    return {
        "actor_uid": uid,
        "actor_email": f"{uid}@example.com",
        "actor_rol": rol,
    }


def record(unity_session_id, display_code=None, s3_key=None, session_data=None):
    """Registro tal como lo entrega Unity/el sistema de los lentes."""

    item = {"unity_session_id": unity_session_id}
    if display_code is not None:
        item["display_code"] = display_code
    if s3_key is not None:
        item["s3_key"] = s3_key
    item["session_data"] = session_data or {
        "respuestas": [{"idPregunta": "pregunta_demo", "respuestaLabel": "5"}]
    }
    return item


def build(
    aws_sessions=None,
    emotion_v2_service=None,
    s3_service=None,
    unity_bucket=None,
    unity_prefix=None,
    lens=None,
    unity_data_service=None,
):
    repo = FakeRepository()
    audit = FakeAudit()
    sessions = SessionService(
        dynamodb_service=repo,
        s3_service=FakeS3(),
        audit_svc=FakeAudit(),
        legacy_data_service=None,
    )
    if lens is None:
        lens = FakeLens()
    # Por defecto la extracción encuentra un archivo distinto de los
    # registros ingeridos manualmente, de modo que `complete_experience`
    # cierra la experiencia en `captured`.
    if aws_sessions is None:
        aws_sessions = [aws_record("AWS-DEFAULT-1")]
    aws = FakeAWS(sessions=aws_sessions)
    service = CaptureService(
        session_service=sessions,
        lens_session_service=lens,
        audit_svc=audit,
        aws_service=aws,
        s3_service=s3_service,
        emotion_v2_service=emotion_v2_service,
        unity_bucket=unity_bucket,
        unity_prefix=unity_prefix,
        unity_data_service=unity_data_service,
    )
    return service, repo, lens, audit


def stored_sessions(repo):
    """Sesiones reales almacenadas (excluye la fila técnica del slot)."""

    return [
        dict(value)
        for (table, _p, _s), value in repo.items.items()
        if table == DYNAMODB_TABLE_SESSIONS
        and value.get("pk_type") == "SESSION"
    ]


def start(service, uid="teacher-1", **kwargs):
    return service.start_experience(actor_ctx=actor(uid), **kwargs)


def finish(service, session_id, uid="teacher-1"):
    """Cierra una experiencia hasta `captured`, liberando el cerrojo global."""

    service.finalize_experience(session_id, actor_ctx=actor(uid))
    return service.complete_experience(session_id, actor_ctx=actor(uid))


HEADERS = {"Authorization": "Bearer token"}
ROUTE_RECORD = {"records": [record("U-001", VALID_CODE)]}


def route_client(service, auth=None):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": auth or FakeAuthService(),
        "capture_service": service,
    }

    @app.errorhandler(AuthorizationError)
    def _forbidden(_error):
        return jsonify({"error": "No tiene permisos para esta acción."}), 403

    @app.errorhandler(AuthenticationError)
    def _unauthorized(_error):
        return jsonify({"error": "Credenciales inválidas."}), 401

    app.register_blueprint(captures_bp)
    return app.test_client()


# ==========================================================
# 1. INICIO Y GENERACIÓN DE IDENTIFICADORES
# ==========================================================


def test_start_generates_new_session_id():
    service, *_ = build()

    result = start(service)

    uuid.UUID(result["session_id"])
    assert result["status"] == STATUS_CAPTURING
    assert result["created"] is True


def test_two_starts_generate_different_ids():
    service, *_ = build()

    first = start(service, device_id="dev-A")["session_id"]
    finish(service, first)

    second = start(service, device_id="dev-B")["session_id"]

    assert first != second


def test_start_does_not_generate_pin():
    service, _repo, lens, _audit = build()

    result = start(service)

    assert "display_code" not in result
    assert result["display_codes"] == []
    assert result["lens_session_ids"] == []
    # EmotionLab no consultó ninguna fuente de PIN para iniciar.
    assert lens.lookups == []


def test_repeated_request_with_same_token_is_idempotent():
    service, repo, _lens, _audit = build()

    first = start(service, request_token="tok-reintento")
    second = start(service, request_token="tok-reintento")

    assert first["session_id"] == second["session_id"]
    assert second["created"] is False
    assert len(stored_sessions(repo)) == 1


# ==========================================================
# 2. PIN Y VALIDACIÓN DE ORIGEN
# ==========================================================


def test_pin_comes_from_lens_source():
    service, _repo, lens, _audit = build()

    session_id = start(service)["session_id"]
    result = service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )

    assert len(result["accepted"]) == 1
    assert lens.lookups == [VALID_CODE]


def test_invalid_pin_is_rejected():
    service, *_ = build()

    session_id = start(service)["session_id"]
    result = service.ingest_records(
        session_id,
        {"records": [record("U-001", "000000")]},
        actor_ctx=actor(),
    )

    assert result["accepted"] == []
    assert result["rejected"][0]["reason"] == "CODIGO_INVALIDO"


def test_record_without_display_code_is_rejected():
    service, *_ = build()

    session_id = start(service)["session_id"]
    result = service.ingest_records(
        session_id,
        {"records": [record("U-001", None)]},
        actor_ctx=actor(),
    )

    assert result["accepted"] == []
    assert result["rejected"][0]["reason"] == "SIN_CODIGO"


def test_same_pin_can_appear_in_multiple_records():
    service, *_ = build()

    session_id = start(service)["session_id"]
    result = service.ingest_records(
        session_id,
        {
            "records": [
                record("U-001", VALID_CODE),
                record("U-002", VALID_CODE),
                record("U-003", VALID_CODE),
            ]
        },
        actor_ctx=actor(),
    )

    assert len(result["accepted"]) == 3
    assert result["rejected"] == []
    assert result["unity_session_ids"] == ["U-001", "U-002", "U-003"]


def test_receiving_valid_pin_again_is_not_rejected():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    result = service.ingest_records(
        session_id,
        {"records": [record("U-004", VALID_CODE)]},
        actor_ctx=actor(),
    )

    assert len(result["accepted"]) == 1
    assert result["rejected"] == []


def test_no_different_pin_required_per_unity_record():
    """Registros independientes comparten el mismo PIN sin rechazo."""

    service, _repo, lens, _audit = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.ingest_records(
        session_id,
        {"records": [record("U-002", VALID_CODE)]},
        actor_ctx=actor(),
    )

    # El PIN se validó dos veces contra la misma fuente: no es de un solo uso.
    assert lens.lookups == [VALID_CODE, VALID_CODE]


def test_lens_credentials_are_not_exposed():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )

    records = service.list_original_records(session_id, actor_ctx=actor())
    view = service.get_experience(session_id, actor_ctx=actor())

    assert "sessionToken" not in str(records)
    assert "sessionToken" not in str(view)


class _LensHttpResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _LensHttp:
    """Doble HTTP del API Gateway del lente (`POST /session/confirm`)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": dict(json or {})})
        return _LensHttpResponse(self.payload)


def test_lookup_respects_lens_api_contract_without_rate_limit():
    """La validación automática no añade una política de un solo uso: el
    mismo PIN se resuelve repetidamente contra el API Gateway del lente sin
    aplicar el limitador del flujo manual ni requerir el secreto HMAC."""

    http = _LensHttp(
        {
            "sessionId": LENS_SESSION_ID,
            "displayCode": VALID_CODE,
            "sessionToken": "private-token",
            "status": "pending",
            "createdAt": "2030-01-15T10:00:00",
        }
    )
    lens = LensSessionService(
        dynamodb_service=object(),
        audit_svc=None,
        ssm_service=None,
        base_url=LENS_BASE_URL,
        http_client=http,
    )

    first = lens.lookup_by_display_code(VALID_CODE)
    second = lens.lookup_by_display_code(VALID_CODE)

    assert first["session_id"] == LENS_SESSION_ID
    assert second["session_id"] == LENS_SESSION_ID
    assert "sessionToken" not in first
    # La validación usa `validateOnly` y no confirma la sesión del lente.
    assert http.calls[0]["json"] == {
        "displayCode": VALID_CODE,
        "validateOnly": True,
    }


# ==========================================================
# 2.B INICIO DE EXPERIENCIA CON PIN
# ==========================================================


def test_start_sends_exactly_one_http_request_to_lens_gateway():
    """Un inicio correcto realiza EXACTAMENTE una petición HTTP al API Gateway.

    Usa un `LensSessionService` real contra un doble HTTP para comprobar el
    contrato de red: una única `POST /session/confirm` de confirmación y
    NINGUNA validación previa (`validateOnly`).
    """

    http = _LensHttp(
        {
            "sessionId": LENS_SESSION_ID,
            "displayCode": VALID_CODE,
            "sessionToken": "private-token",
            "status": "confirmed",
            "createdAt": "2030-01-15T10:00:00",
        }
    )
    lens = LensSessionService(
        dynamodb_service=object(),
        audit_svc=None,
        ssm_service=None,
        base_url=LENS_BASE_URL,
        http_client=http,
    )
    service, repo, _lens, _audit = build(lens=lens)

    view = start(service, display_code=VALID_CODE, request_token="tok-http")

    assert view["status"] == STATUS_CAPTURING
    assert view["lens_session_ids"] == [LENS_SESSION_ID]

    # Exactamente UNA petición HTTP al API Gateway.
    assert len(http.calls) == 1

    url = http.calls[0]["url"]
    payload = http.calls[0]["json"]

    assert url.endswith("/session/confirm")
    # Es una CONFIRMACIÓN (`validateOnly = false`), nunca una validación.
    assert payload["validateOnly"] is False
    assert payload["displayCode"] == VALID_CODE
    assert payload["specialistId"] == "teacher-1"
    assert payload["patientId"] == ""

    # Se creó una sola experiencia y se conservó la identidad del lente.
    assert len(stored_sessions(repo)) == 1


def test_start_with_valid_pin_binds_lens_session():
    service, _repo, lens, _audit = build()

    view = start(service, display_code=VALID_CODE, request_token="tok-pin")

    assert view["status"] == STATUS_CAPTURING
    assert view["lens_session_ids"] == [LENS_SESSION_ID]
    # El `session_id` lo genera EmotionLab (UUID) y es distinto del lente.
    assert view["session_id"] != LENS_SESSION_ID
    uuid.UUID(view["session_id"])
    # El inicio NO realiza una validación previa (`validateOnly`): la Lambda
    # desplegada no lo implementa y confirmaría la sesión por efecto
    # secundario.
    assert lens.lookups == []
    # Se envía UNA ÚNICA petición de confirmación (validateOnly = false) con
    # el `confirmationRequestId` derivado del token.
    assert lens.confirmations == [
        {
            "display_code": VALID_CODE,
            "specialist_id": "teacher-1",
            "confirmation_request_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL,
                          "emotionlab-confirm:teacher-1:tok-pin")
            ),
        }
    ]
    # El PIN no se conserva como identidad: no se persiste en la sesión.
    assert view["display_codes"] == []


def test_start_with_invalid_pin_is_rejected():
    service, *_ = build()

    with pytest.raises(InvalidDisplayCode):
        start(service, display_code="000000")


def test_start_with_malformed_pin_is_rejected():
    service, *_ = build()

    with pytest.raises(InvalidDisplayCode):
        start(service, display_code="abc")


def test_repeated_start_with_same_token_and_pin_is_idempotent():
    service, repo, *_ = build()

    first = start(service, display_code=VALID_CODE, request_token="tok-idem")
    second = start(service, display_code=VALID_CODE, request_token="tok-idem")

    assert first["session_id"] == second["session_id"]
    assert second["created"] is False
    assert len(stored_sessions(repo)) == 1


def test_reused_token_does_not_confirm_pin_again():
    """Un `request_token` repetido NO vuelve a confirmar el PIN."""

    service, _repo, lens, _audit = build()

    first = start(service, display_code=VALID_CODE, request_token="tok-reuso")

    # El mismo token se reintenta con otro PIN: no se confirma de nuevo ni se
    # crea otra experiencia; se devuelve la MISMA ya iniciada.
    lens.mapping["999999"] = "lens-session-ajena"

    second = start(service, display_code="999999", request_token="tok-reuso")

    assert second["session_id"] == first["session_id"]
    assert second["created"] is False
    # Solo hubo UNA confirmación: la del primer inicio.
    assert len(lens.confirmations) == 1
    # La experiencia original no se reemplaza ni se fusiona.
    assert service.get_experience(
        first["session_id"], actor_ctx=actor()
    )["lens_session_ids"] == [LENS_SESSION_ID]


def test_start_without_pin_does_not_bind_lens_session():
    service, _repo, lens, _audit = build()

    view = start(service)

    assert view["lens_session_ids"] == []
    assert lens.lookups == []


def test_ambiguous_confirm_keeps_slot_and_does_not_create_session():
    """Un timeout ambiguo al confirmar NO libera el cerrojo ni reintenta."""

    service, repo, lens, _audit = build()
    lens.confirm_error = LensApiConfirmationAmbiguous(
        "El lente no respondió a la confirmación."
    )

    with pytest.raises(CaptureConfirmationPending):
        start(service, display_code=VALID_CODE, request_token="tok-timeout")

    # La confirmación se intentó UNA sola vez: no hubo reintento automático.
    assert len(lens.confirmations) == 1

    # No se creó una experiencia de EmotionLab (estado recuperable).
    assert stored_sessions(repo) == []

    # El cerrojo global sigue retenido por el MISMO session_id derivado del
    # token y queda marcado como confirmación indeterminada.
    expected_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "emotionlab-capture:teacher-1:tok-timeout")
    )
    slot = service.session.get_capture_slot()
    assert slot.get("active_session_id") == expected_id
    assert slot.get("confirmation_state") == "confirmation_unknown"


def test_retry_after_ambiguous_confirm_does_not_confirm_again():
    """Tras el timeout, reintentar con el mismo token NO vuelve a confirmar.

    La operación quedó indeterminada: el cerrojo permanece retenido y no se
    repite la llamada ni se inicia otra experiencia hasta resolverla.
    """

    service, repo, lens, _audit = build()
    lens.confirm_error = LensApiConfirmationAmbiguous("indeterminado")

    with pytest.raises(CaptureConfirmationPending):
        start(service, display_code=VALID_CODE, request_token="tok-retry")

    assert len(lens.confirmations) == 1

    # El reintento con el mismo token NO confirma de nuevo: vuelve a
    # informar el estado indeterminado.
    with pytest.raises(CaptureConfirmationPending):
        start(service, display_code=VALID_CODE, request_token="tok-retry")

    # No hubo una segunda petición de confirmación ni una experiencia nueva.
    assert len(lens.confirmations) == 1
    assert stored_sessions(repo) == []

    # El cerrojo sigue retenido por el mismo intento.
    expected_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "emotionlab-capture:teacher-1:tok-retry")
    )
    assert service.session.get_capture_slot().get(
        "active_session_id"
    ) == expected_id


def test_route_ambiguous_confirm_returns_409_indeterminada():
    service, _repo, lens, _audit = build()
    lens.confirm_error = LensApiConfirmationAmbiguous("indeterminado")
    client = route_client(service)

    response = client.post(
        "/captures",
        json={"display_code": VALID_CODE, "request_token": "tok-route-timeout"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "CONFIRMACION_INDETERMINADA"
    assert lens.failed_attempts == {}
    assert service.session.get_capture_slot()["confirmation_state"] == "confirmation_unknown"


def test_route_start_with_pin_returns_bound_lens_session():
    service, *_ = build()
    client = route_client(service)

    response = client.post(
        "/captures",
        json={"display_code": VALID_CODE, "request_token": "tok-route"},
        headers=HEADERS,
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["lens_session_ids"] == [LENS_SESSION_ID]


def test_route_start_with_invalid_pin_returns_400():
    service, *_ = build()
    client = route_client(service)

    response = client.post(
        "/captures",
        json={"display_code": "000000"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "PIN_INVALIDO"


def test_route_rate_limits_failed_pin_without_counting_same_token_twice():
    service, _repo, lens, audit = build()
    client = route_client(service)
    payload = {"display_code": "000000", "request_token": "tok-bad-pin"}

    first = client.post("/captures", json=payload, headers=HEADERS)
    retry = client.post("/captures", json=payload, headers=HEADERS)

    assert first.status_code == retry.status_code == 400
    assert len(lens.confirmations) == 1
    assert lens.failed_attempts["127.0.0.1"] == 1
    assert "000000" not in repr(audit.events)


def test_route_returns_expired_pin_and_counts_only_the_failed_confirmation():
    service, _repo, lens, _audit = build()
    lens.confirm_error = LensSessionExpired("expirado")
    response = route_client(service).post(
        "/captures",
        json={"display_code": VALID_CODE, "request_token": "tok-expired"},
        headers=HEADERS,
    )

    assert response.status_code == 410
    assert response.get_json()["error"] == "PIN_EXPIRADO"
    assert len(lens.confirmations) == 1
    assert lens.failed_attempts["127.0.0.1"] == 1


def test_route_blocks_sixth_failed_pin_before_contacting_lens():
    service, _repo, lens, _audit = build()
    client = route_client(service)
    for index in range(5):
        response = client.post(
            "/captures",
            json={"display_code": "000000", "request_token": f"tok-{index}"},
            headers=HEADERS,
        )
        assert response.status_code == 400

    blocked = client.post(
        "/captures",
        json={"display_code": "000000", "request_token": "tok-sixth"},
        headers=HEADERS,
    )
    assert blocked.status_code == 429
    assert blocked.get_json()["error"] == "LIMITE_INTENTOS_PIN"
    assert len(lens.confirmations) == 5


def test_rate_limit_storage_failure_is_service_unavailable_not_invalid_pin():
    service, _repo, lens, _audit = build()
    from services.lens_session_service import SessionCodeRateLimitUnavailable
    lens.rate_check_error = SessionCodeRateLimitUnavailable("tabla ausente")

    response = route_client(service).post(
        "/captures",
        json={"display_code": VALID_CODE, "request_token": "tok-ddb-error"},
        headers=HEADERS,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "LIMITADOR_NO_DISPONIBLE"
    assert lens.confirmations == []


def test_rate_limit_write_failure_after_pin_rejection_is_not_reported_as_bad_pin():
    service, _repo, lens, _audit = build()
    from services.lens_session_service import SessionCodeRateLimitUnavailable
    lens.rate_register_error = SessionCodeRateLimitUnavailable("falló transacción")

    response = route_client(service).post(
        "/captures",
        json={"display_code": "000000", "request_token": "tok-ddb-write-error"},
        headers=HEADERS,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "LIMITADOR_NO_DISPONIBLE"
    assert len(lens.confirmations) == 1


def test_concurrent_requests_cannot_confirm_against_an_in_progress_slot():
    service, _repo, lens, _audit = build()
    entered = threading.Event()
    proceed = threading.Event()

    def blocked_confirm(display_code, specialist_id, confirmation_request_id=None, patient_id=""):
        lens.confirmations.append({
            "display_code": display_code,
            "specialist_id": specialist_id,
            "confirmation_request_id": confirmation_request_id,
        })
        entered.set()
        assert proceed.wait(5)
        return lens._view(display_code, lens._resolve(display_code), "confirmed")

    lens.confirm_display_code = blocked_confirm
    results = []

    def first_request():
        results.append(start(
            service,
            display_code=VALID_CODE,
            request_token="tok-concurrent",
            client_ip="203.0.113.21",
        ))

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(5)
    with pytest.raises(DeviceBusy):
        service.start_experience(
            actor_ctx=actor(),
            display_code=VALID_CODE,
            request_token="tok-concurrent",
            client_ip="203.0.113.21",
        )
    with pytest.raises(DeviceBusy):
        service.start_experience(
            actor_ctx=actor(),
            display_code=VALID_CODE,
            request_token="tok-concurrent-other",
            client_ip="203.0.113.21",
        )
    proceed.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(results) == 1
    assert len(lens.confirmations) == 1


def test_confirmed_pin_is_not_confirmed_again_if_session_persistence_fails():
    service, _repo, lens, _audit = build()
    original_create = service.session.create_capture_session
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("fallo DynamoDB temporal")
        return original_create(*args, **kwargs)

    service.session.create_capture_session = fail_once
    kwargs = {
        "display_code": VALID_CODE,
        "request_token": "tok-resume-confirmed",
        "client_ip": "203.0.113.22",
    }
    with pytest.raises(RuntimeError):
        start(service, **kwargs)
    resumed = start(service, **kwargs)

    assert resumed["lens_session_ids"] == [LENS_SESSION_ID]
    assert len(lens.confirmations) == 1


def test_route_reused_token_returns_same_experience():
    """El mismo `request_token` devuelve la MISMA experiencia (201) y no
    vuelve a confirmar el PIN contra el lente."""

    service, _repo, lens, _audit = build()
    client = route_client(service)

    first = client.post(
        "/captures",
        json={"display_code": VALID_CODE, "request_token": "tok-route-2"},
        headers=HEADERS,
    )
    assert first.status_code == 201

    # Otro PIN, mismo token: no se confirma de nuevo.
    lens.mapping["999999"] = "lens-session-ajena"

    response = client.post(
        "/captures",
        json={"display_code": "999999", "request_token": "tok-route-2"},
        headers=HEADERS,
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["session_id"] == first.get_json()["session_id"]
    assert data["lens_session_ids"] == [LENS_SESSION_ID]
    assert len(lens.confirmations) == 1


def test_simulated_full_capture_cycle_with_pin():
    """Ciclo completo simulado: PIN -> inicio -> finalizar -> extraer.

    No es una prueba real con los lentes: usa dobles de AWS/S3 autenticados
    contra el mismo backend que utiliza el cliente de Unity.
    """

    service, _repo, lens, audit = build(
        aws_sessions=[
            aws_record("U-001", s3_key="lens/a.json"),
            aws_record("U-002", s3_key="lens/b.json"),
        ]
    )

    started = start(
        service, display_code=VALID_CODE, request_token="tok-ciclo"
    )
    session_id = started["session_id"]
    assert started["lens_session_ids"] == [LENS_SESSION_ID]
    # El inicio confirma la sesión del lente contra el API Gateway con UNA
    # sola petición y sin validación previa (`validateOnly`).
    assert lens.lookups == []
    assert len(lens.confirmations) == 1
    assert lens.confirmations[0]["display_code"] == VALID_CODE

    service.finalize_experience(session_id, actor_ctx=actor())
    result = service.complete_experience(session_id, actor_ctx=actor())

    assert result["status"] == STATUS_CAPTURED
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert sorted(r["unity_session_id"] for r in records) == ["U-001", "U-002"]
    # El cerrojo global se libera únicamente al alcanzar `captured`.
    assert service.session.get_capture_slot() is None
    assert AUDIT_CAPTURE_IMPORTED in audit.actions()


# ==========================================================
# 3. CENTRALIZACIÓN DE DATOS
# ==========================================================


def test_multiple_unity_ids_under_one_session():
    service, *_ = build()

    session_id = start(service)["session_id"]
    result = service.ingest_records(
        session_id,
        {
            "records": [
                record("U-001", VALID_CODE),
                record("U-002", VALID_CODE),
                record("U-003", VALID_CODE),
            ]
        },
        actor_ctx=actor(),
    )

    assert result["record_count"] == 3
    assert result["unity_session_ids"] == ["U-001", "U-002", "U-003"]
    view = service.get_experience(session_id, actor_ctx=actor())
    assert view["lens_session_ids"] == [LENS_SESSION_ID]


def test_unity_original_ids_are_preserved():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {
            "records": [
                record(
                    "U-001",
                    VALID_CODE,
                    session_data={
                        "respuestas": [
                            {"idPregunta": "pregunta_demo", "respuestaLabel": "5"}
                        ]
                    },
                )
            ]
        },
        actor_ctx=actor(),
    )

    records = service.list_original_records(session_id, actor_ctx=actor())

    assert records[0]["unity_session_id"] == "U-001"
    assert records[0]["lens_session_id"] == LENS_SESSION_ID
    assert records[0]["record"]["session_data"]["respuestas"][0][
        "respuestaLabel"
    ] == "16"
    # Los datos originales de Unity no se sustituyen por el `session_id`.
    assert records[0]["record"]["unity_session_id"] == "U-001"


def test_duplicate_records_do_not_duplicate():
    service, *_ = build()

    session_id = start(service)["session_id"]
    original = record("U-001", VALID_CODE, s3_key="raw/U-001.json")

    service.ingest_records(
        session_id,
        {"records": [original]},
        actor_ctx=actor(),
    )
    result = service.ingest_records(
        session_id,
        {"records": [dict(original)]},
        actor_ctx=actor(),
    )

    assert result["accepted"] == []
    assert result["record_count"] == 1


def test_records_of_different_lens_sessions_are_not_merged():
    """Una captura del lente ya vinculada a otra experiencia no se fusiona.

    MVP: solo hay una experiencia activa a la vez, por lo que la segunda se
    inicia una vez cerrada la primera.
    """

    service, *_ = build()

    first = start(service, device_id="dev-A")["session_id"]

    service.ingest_records(
        first,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    finish(service, first)

    second = start(service, device_id="dev-B")["session_id"]
    result = service.ingest_records(
        second,
        {"records": [record("U-002", VALID_CODE)]},
        actor_ctx=actor(),
    )

    assert result["accepted"] == []
    assert result["rejected"][0]["reason"] == "EXPERIENCIA_DISTINTA"
    assert service.list_original_records(second, actor_ctx=actor()) == []


def test_same_pin_in_independent_experiences_is_not_merged():
    """El PIN puede repetirse: dos capturas distintas con el mismo PIN no se
    fusionan porque la vinculación permanente usa la identidad del lente.

    MVP: la segunda experiencia se inicia tras cerrar la primera.
    """

    service, _repo, lens, _audit = build()

    first = start(service, device_id="dev-A")["session_id"]
    service.ingest_records(
        first,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    finish(service, first)

    # El lente reutiliza el mismo PIN en una captura nueva.
    lens.mapping[VALID_CODE] = "lens-session-B"
    second = start(service, device_id="dev-B")["session_id"]
    result = service.ingest_records(
        second,
        {"records": [record("U-002", VALID_CODE)]},
        actor_ctx=actor(),
    )

    assert len(result["accepted"]) == 1

    first_ids = [
        r["unity_session_id"]
        for r in service.list_original_records(first, actor_ctx=actor())
    ]
    second_ids = [
        r["unity_session_id"]
        for r in service.list_original_records(second, actor_ctx=actor())
    ]

    # Cada captura conserva sus propios registros, sin fusionarse.
    assert "U-001" in first_ids
    assert "U-002" not in first_ids
    assert second_ids == ["U-002"]


def test_ingestion_preserves_previously_received_records():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.ingest_records(
        session_id,
        {"records": [record("U-002", VALID_CODE)]},
        actor_ctx=actor(),
    )

    records = service.list_original_records(session_id, actor_ctx=actor())

    assert [r["unity_session_id"] for r in records] == ["U-001", "U-002"]


def test_capture_does_not_group_by_participant_or_name():
    service, repo, _lens, _audit = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )

    stored = stored_sessions(repo)[0]

    # Sin alumno no hay identidad personal: solo la partición técnica.
    assert stored["participant_id"] == "__PENDING__"
    assert stored.get("nombre") is None


# ==========================================================
# 4. FINALIZACIÓN
# ==========================================================


def test_finalize_requests_device_stop():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    view = service.finalize_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_FINALIZING
    assert view["device_stop"]["requested"] is True
    # La orden física de detención es una integración externa pendiente.
    assert view["device_stop"]["integration_pending"] is True


def test_pending_data_of_known_capture_is_accepted_while_finalizing():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.finalize_experience(session_id, actor_ctx=actor())

    result = service.ingest_records(
        session_id,
        {"records": [record("U-002", VALID_CODE)]},
        actor_ctx=actor(),
    )

    assert len(result["accepted"]) == 1
    assert result["status"] == STATUS_FINALIZING


def test_new_capture_is_rejected_while_finalizing():
    service, _repo, lens, _audit = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.finalize_experience(session_id, actor_ctx=actor())

    lens.mapping["999999"] = "lens-session-Z"
    result = service.ingest_records(
        session_id,
        {"records": [record("U-002", "999999")]},
        actor_ctx=actor(),
    )

    assert result["accepted"] == []
    assert result["rejected"][0]["reason"] == "EXPERIENCIA_CERRANDO"


def test_completed_session_rejects_new_records():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.finalize_experience(session_id, actor_ctx=actor())
    service.complete_experience(session_id, actor_ctx=actor())

    with pytest.raises(SessionClosed):
        service.ingest_records(
            session_id,
            {"records": [record("U-002", VALID_CODE)]},
            actor_ctx=actor(),
        )


def test_finalized_session_cannot_be_reopened():
    service, *_ = build()

    session_id = start(service)["session_id"]
    service.finalize_experience(session_id, actor_ctx=actor())
    service.complete_experience(session_id, actor_ctx=actor())

    assert service.finalize_experience(
        session_id, actor_ctx=actor()
    )["status"] == STATUS_CAPTURED
    assert service.complete_experience(
        session_id, actor_ctx=actor()
    )["status"] == STATUS_CAPTURED


def test_new_experience_uses_new_session_id():
    service, *_ = build()

    first = start(service)["session_id"]
    service.finalize_experience(first, actor_ctx=actor())
    service.complete_experience(first, actor_ctx=actor())

    second = start(service)["session_id"]

    assert second != first
    assert service.list_original_records(second, actor_ctx=actor()) == []


def test_repeated_finalization_is_idempotent():
    service, _repo, _lens, audit = build()

    session_id = start(service)["session_id"]

    service.finalize_experience(session_id, actor_ctx=actor())
    again = service.finalize_experience(session_id, actor_ctx=actor())

    assert again["status"] == STATUS_FINALIZING
    assert audit.actions().count(AUDIT_CAPTURE_FINALIZING) == 1


# ==========================================================
# 5. PRIVACIDAD
# ==========================================================


def test_start_does_not_generate_tracking_id():
    service, repo, _lens, _audit = build()

    session_id = start(service)["session_id"]

    stored = stored_sessions(repo)[0]
    view = service.get_experience(session_id, actor_ctx=actor())

    assert "tracking_id" not in stored
    assert "tracking_id" not in view


def test_start_does_not_generate_student_participant_id():
    service, repo, *_ = build()

    start(service)

    assert stored_sessions(repo)[0]["participant_id"] == "__PENDING__"


def test_does_not_store_private_excel_correspondence():
    service, repo, *_ = build()

    start(service)

    stored = stored_sessions(repo)[0]

    for field in (
        "alumno",
        "student",
        "persona",
        "excel",
        "participant_name",
        "nombre_alumno",
    ):
        assert field not in stored
    assert stored.get("nombre") is None


def test_audit_and_records_do_not_include_real_names():
    service, _repo, _lens, audit = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )
    service.finalize_experience(session_id, actor_ctx=actor())

    for event in audit.events:
        blob = str(event).lower()
        assert "alumno" not in blob
        assert "nombre" not in blob

    records = service.list_original_records(session_id, actor_ctx=actor())
    assert "nombre" not in str(records).lower()


def test_capture_started_is_audited_without_personal_data():
    service, _repo, _lens, audit = build()

    start(service)

    started = [e for e in audit.events if e["action"] == AUDIT_CAPTURE_STARTED]
    assert len(started) == 1
    assert "nombre" not in str(started[0]).lower()


# ==========================================================
# 6. AUTORIZACIÓN POR PROPIETARIO
# ==========================================================


def test_other_specialist_cannot_operate_on_experience():
    service, *_ = build()

    session_id = start(service, uid="teacher-1")["session_id"]

    with pytest.raises(CaptureNotAuthorized):
        service.get_experience(session_id, actor_ctx=actor("teacher-2"))


def test_admin_can_operate_on_any_experience():
    service, *_ = build()

    session_id = start(service, uid="teacher-1")["session_id"]

    view = service.get_experience(session_id, actor_ctx=actor("admin-1", "admin"))

    assert view["session_id"] == session_id


# ==========================================================
# 7. RUTAS HTTP
# ==========================================================


def test_route_start_returns_201_and_session_id():
    service, *_ = build()
    client = route_client(service)

    response = client.post("/captures", json={}, headers=HEADERS)

    assert response.status_code == 201
    data = response.get_json()
    uuid.UUID(data["session_id"])
    assert data["status"] == STATUS_CAPTURING


def test_route_full_cycle():
    service, *_ = build()
    client = route_client(service)

    session_id = client.post(
        "/captures", json={}, headers=HEADERS
    ).get_json()["session_id"]

    ingest = client.post(
        f"/captures/{session_id}/records",
        json=dict(ROUTE_RECORD),
        headers=HEADERS,
    )
    assert ingest.status_code == 202

    assert client.post(
        f"/captures/{session_id}/finalize", headers=HEADERS
    ).status_code == 200

    completed = client.post(
        f"/captures/{session_id}/complete", headers=HEADERS
    )
    assert completed.status_code == 200
    assert completed.get_json()["status"] == STATUS_CAPTURED

    status = client.get(f"/captures/{session_id}", headers=HEADERS)
    assert status.status_code == 200

    records = client.get(
        f"/captures/{session_id}/records", headers=HEADERS
    )
    assert records.status_code == 200
    assert records.get_json()["records"][0]["unity_session_id"] == "U-001"


def test_route_rejects_ingest_on_completed_session():
    service, *_ = build()
    client = route_client(service)

    session_id = client.post(
        "/captures", json={}, headers=HEADERS
    ).get_json()["session_id"]
    client.post(
        f"/captures/{session_id}/records",
        json=dict(ROUTE_RECORD),
        headers=HEADERS,
    )
    client.post(f"/captures/{session_id}/finalize", headers=HEADERS)
    client.post(f"/captures/{session_id}/complete", headers=HEADERS)

    response = client.post(
        f"/captures/{session_id}/records",
        json={"records": [record("U-999", VALID_CODE, s3_key="raw/U-999.json")]},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "SESSION_CLOSED"


def test_route_rejects_other_owner():
    service, *_ = build()

    session_id = start(service, uid="teacher-1")["session_id"]

    client = route_client(service, auth=FakeAuthService(uid="teacher-2"))
    response = client.get(f"/captures/{session_id}", headers=HEADERS)

    assert response.status_code == 403


def test_legacy_capture_without_owner_is_denied():
    service, repo, *_ = build()
    session_id = start(service, uid="teacher-1")["session_id"]
    key = service.session._resolve_session_key(session_id)
    repo.items[(DYNAMODB_TABLE_SESSIONS, key["participant_id"], key["sk"])].pop("owner_id")

    with pytest.raises(CaptureNotAuthorized):
        service._load_owned(session_id, actor("teacher-1"))


def test_route_requires_teacher_role():
    service, *_ = build()
    client = route_client(service, auth=FakeAuthService(rol="viewer"))

    response = client.get("/captures/whatever", headers=HEADERS)

    assert response.status_code == 403


def test_route_unknown_session_returns_404():
    service, *_ = build()
    client = route_client(service)

    response = client.get("/captures/no-existe", headers=HEADERS)

    assert response.status_code == 404
    assert response.get_json()["error"] == "EXPERIENCIA_NO_ENCONTRADA"


def test_route_rejects_unknown_fields_on_start():
    service, *_ = build()
    client = route_client(service)

    response = client.post(
        "/captures",
        json={"campo_desconocido": "x"},
        headers=HEADERS,
    )

    assert response.status_code == 400


# ==========================================================
# 8. CONCURRENCIA / REINTENTOS
# ==========================================================


def test_double_finalize_race_keeps_single_transition():
    """Dos cierres concurrentes: la transición condicional solo aplica una vez."""

    service, *_ = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", VALID_CODE)]},
        actor_ctx=actor(),
    )

    first = service.finalize_experience(session_id, actor_ctx=actor())
    second = service.finalize_experience(session_id, actor_ctx=actor())

    assert first["status"] == STATUS_FINALIZING
    assert second["status"] == STATUS_FINALIZING


def test_record_rejection_is_audited():
    service, _repo, _lens, audit = build()

    session_id = start(service)["session_id"]
    service.ingest_records(
        session_id,
        {"records": [record("U-001", "000000")]},
        actor_ctx=actor(),
    )

    assert AUDIT_CAPTURE_RECORD_REJECTED in audit.actions()


# ==========================================================
# 9. EXTRACCIÓN DESDE AWS (NUEVO FLUJO DE RECOLECCIÓN)
# ==========================================================
#
# Marca de la experiencia usada por las pruebas temporales. Se fija en la
# sesión después de finalizar para comprobar el filtro
# `started_at <= timestamp <= ended_at` de forma determinista.
W_START = "2030-01-15T13:00:00Z"
W_END = "2030-01-15T13:45:00Z"


def open_window(
    service,
    *,
    device_id=None,
    window_start=W_START,
    window_end=W_END,
    ready=None,
):
    """Inicia y finaliza una experiencia con un intervalo temporal fijo."""

    session_id = start(service, device_id=device_id)["session_id"]
    service.finalize_experience(session_id, actor_ctx=actor())

    fields = {
        "capture_started_at": window_start,
        "capture_ended_at": window_end,
    }
    if ready is not None:
        fields["data_ready_at"] = ready
    service.session.update_session_fields(session_id, fields)

    return session_id


# ----------------------------------------------------------
# INICIO
# ----------------------------------------------------------


def test_start_registers_capture_started_at_in_utc():
    service, *_ = build()

    view = start(service)

    assert view["capture_started_at"].endswith("Z")
    assert view["capture_ended_at"] is None
    assert view["data_ready_at"] is None


def test_start_uses_utc_with_offset_zero():
    service, *_ = build()

    started = start(service)["capture_started_at"]

    parsed = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - parsed
    assert abs(delta.total_seconds()) < 120


def test_capture_timestamp_is_presented_as_santiago_local_date_and_time():
    session = {
        "capture_started_at": "2026-11-17T00:10:00Z",
        "fecha": "2026-11-17",
        "hora": "00:10",
    }
    assert SessionService._get_date(session) == "2030-01-16"
    assert SessionService._get_time(session) == "21:10"


def test_ambiguous_legacy_timestamp_is_not_assigned_a_timezone():
    session = {
        "capture_started_at": "2030-01-16T21:10:00",
        "fecha": "2030-01-16",
        "hora": "21:10",
    }
    assert SessionService._get_date(session) == "2030-01-16"
    assert SessionService._get_time(session) == "21:10"


def test_single_active_experience_globally():
    service, *_ = build()

    first = start(service, device_id="dev-1")["session_id"]

    # Mientras exista una experiencia activa, cualquier otro inicio se
    # bloquea, incluso desde otro dispositivo u otro especialista.
    with pytest.raises(DeviceBusy):
        start(service, device_id="dev-2")
    with pytest.raises(DeviceBusy):
        start(service, uid="teacher-2", device_id="dev-3")

    finish(service, first)

    # Al alcanzar `captured` el cerrojo se libera y puede iniciarse otra.
    assert start(service, device_id="dev-2")["created"] is True


def test_global_lock_held_through_finalizing_and_importing():
    service, *_ = build()

    first = start(service)["session_id"]
    service.finalize_experience(first, actor_ctx=actor())

    # En `finalizing` el cerrojo sigue retenido.
    with pytest.raises(DeviceBusy):
        start(service)

    service.complete_experience(first, actor_ctx=actor())

    # Tras `captured` se libera.
    assert start(service)["created"] is True


def test_global_lock_held_on_import_error_until_retry():
    bucket = _bucket_with_failing_download()
    service, *_ = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())
    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR

    # El fallo de extracción mantiene el cerrojo global retenido por esta
    # misma experiencia.
    assert (
        service.session.get_capture_slot()["active_session_id"] == session_id
    )
    with pytest.raises(DeviceBusy):
        start(service)

    bucket.objects["lens/b.json"]["error"] = False
    bucket.objects["lens/b.json"]["body"] = json.dumps(
        {"unity_session_id": "U-002", "session_data": {"respuestas": []}}
    ).encode("utf-8")

    view = service.complete_experience(session_id, actor_ctx=actor())
    assert view["status"] == STATUS_CAPTURED

    # Tras la extracción exitosa el cerrojo queda liberado.
    assert service.session.get_capture_slot() is None

    # Recién tras `captured` se permite una nueva experiencia.
    assert start(service)["created"] is True


def test_route_conflicts_while_experience_is_active():
    service, *_ = build()
    client = route_client(service)

    client.post("/captures", json={"device_id": "dev-1"}, headers=HEADERS)
    response = client.post(
        "/captures", json={"device_id": "dev-2"}, headers=HEADERS
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "DISPOSITIVO_OCUPADO"


def test_old_experience_cannot_release_current_lock():
    service, *_ = build()
    sessions = service.session

    assert sessions.acquire_capture_slot("session-old") is True
    assert sessions.release_capture_slot("session-old") is True

    # Otra experiencia toma el cerrojo global.
    assert sessions.acquire_capture_slot("session-new") is True

    # La experiencia antigua no puede liberar el cerrojo de la nueva.
    assert sessions.release_capture_slot("session-old") is False

    slot = sessions.get_capture_slot()
    assert slot is not None
    assert slot["active_session_id"] == "session-new"


def test_stale_cleanup_cannot_delete_replaced_lock():
    service, *_ = build()
    sessions = service.session

    assert sessions.acquire_capture_slot("session-old") is True
    # Un proceso lee el cerrojo antes de intentar limpiarlo por obsoleto.
    stale_value = sessions.get_capture_slot()["active_session_id"]

    # Otro proceso lo reemplaza concurrentemente por uno nuevo.
    assert sessions.release_capture_slot("session-old") is True
    assert sessions.acquire_capture_slot("session-new") is True

    # La limpieza con el valor leído no debe eliminar el cerrojo reemplazado.
    assert sessions.release_capture_slot(stale_value) is False
    assert sessions.get_capture_slot()["active_session_id"] == "session-new"


def test_metadata_only_lock_is_recovered_only_for_same_owner_and_no_open_sessions():
    service, repo, _, _ = build()
    lock_key = (DYNAMODB_TABLE_SESSIONS, PENDING_PARTICIPANT_ID, "capture-lock#global")
    repo.items[lock_key] = {
        "participant_id": PENDING_PARTICIPANT_ID,
        "sk": "capture-lock#global",
        "pk_type": PK_TYPE_CAPTURE_LOCK,
        "owner_id": "teacher-1",
        "created_at": "2026-11-23T10:00:00Z",
    }
    historical_key = (DYNAMODB_TABLE_SESSIONS, "student-1", "2026-11-20#10:00")
    repo.items[historical_key] = {
        "participant_id": "student-1",
        "sk": "2026-11-20#10:00",
        "pk_type": PK_TYPE_SESSION,
        "owner_id": "teacher-1",
        "status": STATUS_CAPTURED,
        "session_id": "historic-session",
        "raw_json": {"records": [{"answer": 8}]},
        "model_processing": {"model_version": "1.0.0-experimental", "grupo": 1},
    }

    response = route_client(service).post(
        "/captures",
        json={},
        headers=HEADERS,
    )
    assert response.status_code == 201
    created = response.get_json()

    slot = service.session.get_capture_slot()
    assert slot["active_session_id"] == created["session_id"]
    assert repo.items[historical_key]["raw_json"] == {"records": [{"answer": 8}]}
    assert repo.items[historical_key]["model_processing"]["grupo"] == 1


def test_literal_pending_partition_is_not_the_lock_key_used_by_this_backend():
    service, repo, _, _ = build()
    reported_key = (DYNAMODB_TABLE_SESSIONS, "PENDING", "capture-lock#global")
    reported_item = {
        "participant_id": "PENDING",
        "sk": "capture-lock#global",
        "pk_type": PK_TYPE_CAPTURE_LOCK,
        "owner_id": "teacher-1",
    }
    repo.items[reported_key] = dict(reported_item)

    created = start(service)

    assert created["session_id"]
    assert repo.items[reported_key] == reported_item
    assert service.session.get_capture_slot()["active_session_id"] == created["session_id"]


def test_metadata_only_lock_for_another_owner_is_not_released():
    service, repo, _, _ = build()
    lock_key = (DYNAMODB_TABLE_SESSIONS, PENDING_PARTICIPANT_ID, "capture-lock#global")
    lock = {
        "participant_id": PENDING_PARTICIPANT_ID,
        "sk": "capture-lock#global",
        "pk_type": PK_TYPE_CAPTURE_LOCK,
        "owner_id": "teacher-other",
        "created_at": "2026-11-23T10:00:00Z",
    }
    repo.items[lock_key] = dict(lock)

    with pytest.raises(DeviceBusy):
        start(service)

    assert repo.items[lock_key] == lock


def test_metadata_only_lock_is_not_released_while_any_capture_is_open():
    service, repo, _, _ = build()
    lock_key = (DYNAMODB_TABLE_SESSIONS, PENDING_PARTICIPANT_ID, "capture-lock#global")
    repo.items[lock_key] = {
        "participant_id": PENDING_PARTICIPANT_ID,
        "sk": "capture-lock#global",
        "pk_type": PK_TYPE_CAPTURE_LOCK,
        "owner_id": "teacher-1",
    }
    open_session_key = (DYNAMODB_TABLE_SESSIONS, PENDING_PARTICIPANT_ID, "capture#open")
    repo.items[open_session_key] = {
        "participant_id": PENDING_PARTICIPANT_ID,
        "sk": "capture#open",
        "pk_type": PK_TYPE_SESSION,
        "owner_id": "teacher-1",
        "session_id": "open-session",
        "status": STATUS_CAPTURING,
    }

    with pytest.raises(DeviceBusy):
        start(service)

    assert repo.items[lock_key]["owner_id"] == "teacher-1"
    assert repo.items[open_session_key]["status"] == STATUS_CAPTURING


def test_release_capture_slot_requires_matching_owner_when_owner_is_supplied():
    service, _, _, _ = build()
    sessions = service.session
    assert sessions.acquire_capture_slot("session-old", owner_id="teacher-other") is True

    assert sessions.release_capture_slot("session-old", owner_id="teacher-1") is False
    assert sessions.get_capture_slot()["active_session_id"] == "session-old"


def test_two_simultaneous_starts_cannot_both_hold_lock():
    service, *_ = build()

    # Dos inicios distintos compiten por el cerrojo global: solo uno gana.
    first = service.start_experience(
        actor_ctx=actor(), request_token="token-a"
    )

    with pytest.raises(DeviceBusy):
        service.start_experience(actor_ctx=actor(), request_token="token-b")

    slot = service.session.get_capture_slot()
    assert slot["active_session_id"] == first["session_id"]


# ----------------------------------------------------------
# FINALIZACIÓN
# ----------------------------------------------------------


def test_finalize_records_capture_ended_at_once():
    service, *_ = build()

    session_id = start(service)["session_id"]
    first = service.finalize_experience(session_id, actor_ctx=actor())
    second = service.finalize_experience(session_id, actor_ctx=actor())

    assert first["capture_ended_at"] is not None
    assert first["capture_ended_at"].endswith("Z")
    # El reintento no altera la hora de término fijada.
    assert second["capture_ended_at"] == first["capture_ended_at"]


# ----------------------------------------------------------
# EXTRACCIÓN TEMPORAL
# ----------------------------------------------------------


def test_extraction_imports_records_inside_window():
    service, *_ = build(
        aws_sessions=[aws_record("U-001", captured_at="2030-01-15T13:10:00Z")]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert [r["unity_session_id"] for r in records] == ["U-001"]


def test_extraction_excludes_records_before_start():
    service, *_ = build(
        aws_sessions=[aws_record("U-001", captured_at="2030-01-15T12:30:00Z")]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    assert view["extraction"]["files_found"] == 0
    assert view["extraction"]["import_status"] == "no_data"


def test_extraction_excludes_records_after_end_with_capture_timestamp():
    service, *_ = build(
        aws_sessions=[aws_record("U-001", captured_at="2030-01-15T14:00:00Z")]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    assert view["extraction"]["files_found"] == 0
    assert view["extraction"]["import_status"] == "no_data"


def test_extraction_includes_late_upload_captured_before_end():
    """Un JSON subido tras finalizar pero capturado dentro del intervalo no
    se pierde porque `data_ready_at` amplía el límite superior."""

    late = aws_record("U-001")
    late.pop("captured_at", None)
    late["timestamp_subida"] = "2030-01-15T13:55:00Z"

    service, *_ = build(aws_sessions=[late])

    session_id = open_window(service, ready="2030-01-15T14:00:00Z")
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert [r["unity_session_id"] for r in records] == ["U-001"]


def test_extraction_normalizes_non_utc_timestamps():
    """`2030-01-15T10:10:00-03:00` equivale a 13:10 UTC, dentro del intervalo."""

    service, *_ = build(
        aws_sessions=[
            aws_record("U-001", captured_at="2030-01-15T10:10:00-03:00")
        ]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED


def test_extraction_includes_window_boundaries():
    service, *_ = build(
        aws_sessions=[
            aws_record("U-BEGIN", captured_at=W_START),
            aws_record("U-END", captured_at=W_END),
        ]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    assert view["extraction"]["files_found"] == 2


def test_list_objects_v2_walks_all_pages():
    pages = [
        {
            "Contents": [
                {"Key": "lens/a.json", "LastModified": None, "Size": 1, "ETag": "e1"},
                {"Key": "lens/ignore.txt", "LastModified": None, "Size": 1, "ETag": "e0"},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "1",
        },
        {
            "Contents": [
                {"Key": "lens/b.json", "LastModified": None, "Size": 2, "ETag": "e2"}
            ],
            "IsTruncated": True,
            "NextContinuationToken": "2",
        },
        {
            "Contents": [
                {"Key": "lens/c.json", "LastModified": None, "Size": 3, "ETag": "e3"}
            ],
            "IsTruncated": False,
        },
    ]

    s3 = object.__new__(S3Service)
    s3._client = FakeListClient(pages)

    result = s3.list_objects_v2("unity-bucket", "lens/", suffix=".json")

    assert [item["key"] for item in result] == [
        "lens/a.json",
        "lens/b.json",
        "lens/c.json",
    ]
    # Se consultaron todas las páginas mediante ContinuationToken.
    assert len(s3._client.calls) == 3
    assert s3._client.calls[1]["ContinuationToken"] == "1"
    assert s3._client.calls[2]["ContinuationToken"] == "2"


def test_extraction_ignores_non_json_files():
    valid = aws_record("U-001", s3_key="lens/a.json")
    not_json = aws_record("U-002", s3_key="lens/notes.txt")

    service, *_ = build(aws_sessions=[valid, not_json])

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["extraction"]["files_found"] == 1
    assert view["status"] == STATUS_CAPTURED


def test_extraction_ignores_objects_outside_prefix():
    inside = aws_record("U-001", s3_key="lens/exp1/a.json")
    outside = aws_record("U-002", s3_key="lens/exp2/b.json")

    service, *_ = build(aws_sessions=[inside, outside], unity_prefix="lens/exp1/")

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["extraction"]["files_found"] == 1
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert [r["unity_session_id"] for r in records] == ["U-001"]


def test_extraction_preserves_s3_metadata():
    bucket = FakeS3Bucket()
    bucket.add(
        "lens/a.json",
        {
            "unity_session_id": "U-001",
            "session_data": {"respuestas": []},
        },
        last_modified=datetime(2026, 11, 15, 13, 10, tzinfo=timezone.utc),
        etag="etag-1",
    )

    service, *_ = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    stored = service.list_original_records(session_id, actor_ctx=actor())[0]
    assert stored["s3_key"] == "lens/a.json"
    assert stored["etag"] == "etag-1"
    assert stored["size"] > 0
    assert stored["last_modified"] is not None


def test_extraction_without_matching_files_does_not_complete():
    service, *_ = build(aws_sessions=[])

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    assert view["extraction"]["files_found"] == 0
    assert view["extraction"]["files_imported"] == 0


# ----------------------------------------------------------
# CENTRALIZACIÓN
# ----------------------------------------------------------


def test_extraction_centralizes_multiple_unity_ids():
    service, *_ = build(
        aws_sessions=[
            aws_record("U-001", s3_key="lens/a.json"),
            aws_record("U-002", s3_key="lens/b.json"),
            aws_record("U-003", s3_key="lens/c.json"),
        ]
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert sorted(r["unity_session_id"] for r in records) == [
        "U-001",
        "U-002",
        "U-003",
    ]
    assert view["record_count"] == 3


def test_extraction_preserves_original_identifiers_and_content():
    original = {
        "unity_session_id": "U-001",
        "session_data": {
            "respuestas": [{"idPregunta": "pregunta_demo", "respuestaLabel": "5"}]
        },
    }

    service, *_ = build(aws_sessions=[dict(original, s3_key="lens/a.json")])

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())

    stored = service.list_original_records(session_id, actor_ctx=actor())[0]

    # El identificador original de Unity y el contenido no se modifican.
    assert stored["unity_session_id"] == "U-001"
    assert stored["record"]["session_data"] == original["session_data"]
    # Tampoco se reemplaza la identidad de EmotionLab.
    assert service.get_experience(session_id, actor_ctx=actor())[
        "session_id"
    ] == session_id


def test_repeated_extraction_does_not_duplicate():
    service, *_ = build(
        aws_sessions=[aws_record("U-001", s3_key="lens/a.json")]
    )

    session_id = start(service)["session_id"]
    service.finalize_experience(session_id, actor_ctx=actor())
    service.session.update_session_fields(
        session_id,
        {"capture_started_at": W_START, "capture_ended_at": W_END},
    )

    session = service.session
    first = service._extract_and_centralize(
        session_id, session.get_session(session_id), None
    )
    second = service._extract_and_centralize(
        session_id, session.get_session(session_id), None
    )

    assert first["files_imported"] == 1
    # La segunda pasada reconoce el `s3_key` y no vuelve a duplicar.
    assert second["files_imported"] == 0
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert len(records) == 1


# ----------------------------------------------------------
# ERRORES Y RECUPERACIÓN
# ----------------------------------------------------------


def _bucket_with_failing_download():
    bucket = FakeS3Bucket()
    bucket.add(
        "lens/a.json",
        {"unity_session_id": "U-001", "session_data": {"respuestas": []}},
        last_modified=datetime(2026, 11, 15, 13, 10, tzinfo=timezone.utc),
        etag="e1",
    )
    bucket.add(
        "lens/b.json",
        b"contenido",
        last_modified=datetime(2026, 11, 15, 13, 20, tzinfo=timezone.utc),
        etag="e2",
    )
    bucket.objects["lens/b.json"]["error"] = True
    return bucket


def test_download_failure_keeps_previous_records():
    bucket = _bucket_with_failing_download()
    service, *_ = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    # La extracción incompleta no marca la experiencia como capturada.
    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    assert view["extraction"]["files_imported"] == 1
    assert view["extraction"]["files_pending"] == 1

    records = service.list_original_records(session_id, actor_ctx=actor())
    assert [r["unity_session_id"] for r in records] == ["U-001"]


def test_extraction_can_be_retried_after_failure():
    bucket = _bucket_with_failing_download()
    service, *_ = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())

    # Se resuelve el problema de descarga y se reintenta la extracción.
    bucket.objects["lens/b.json"]["error"] = False
    bucket.objects["lens/b.json"]["body"] = json.dumps(
        {"unity_session_id": "U-002", "session_data": {"respuestas": []}}
    ).encode("utf-8")

    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    records = service.list_original_records(session_id, actor_ctx=actor())
    # No se duplica U-001 y se incorpora U-002.
    assert sorted(r["unity_session_id"] for r in records) == ["U-001", "U-002"]


def test_extraction_errors_are_registered():
    bucket = _bucket_with_failing_download()
    service, _repo, _lens, audit = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    assert view["extraction"]["errors"]
    assert view["extraction"]["errors"][0]["stage"] == "download"
    assert AUDIT_CAPTURE_IMPORT_FAILED in audit.actions()


def test_successful_extraction_is_audited():
    service, _repo, _lens, audit = build(
        aws_sessions=[aws_record("U-001", s3_key="lens/a.json")]
    )

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())

    assert AUDIT_CAPTURE_IMPORTED in audit.actions()


# ----------------------------------------------------------
# PRIVACIDAD Y AUTORIZACIÓN
# ----------------------------------------------------------


def test_other_specialist_cannot_extract():
    service, *_ = build()

    session_id = start(service, uid="teacher-1")["session_id"]
    service.finalize_experience(session_id, actor_ctx=actor("teacher-1"))

    with pytest.raises(CaptureNotAuthorized):
        service.complete_experience(session_id, actor_ctx=actor("teacher-2"))

    with pytest.raises(CaptureNotAuthorized):
        service.list_original_records(session_id, actor_ctx=actor("teacher-2"))


def test_no_credentials_are_stored_during_extraction():
    bucket = _bucket_with_failing_download()
    service, repo, _lens, audit = build(
        aws_sessions=[],
        s3_service=bucket,
        unity_bucket="unity-bucket",
        unity_prefix="lens/",
    )

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())

    forbidden = ("secret", "password", "access_key", "session_token")

    blob = str(stored_sessions(repo)).lower()
    blob += str(audit.events).lower()
    blob += str(
        service.get_experience(session_id, actor_ctx=actor())
    ).lower()

    for needle in forbidden:
        assert needle not in blob


# ==========================================================
# 10. INGESTA DESDE `/data` (API GATEWAY DEL LENTE)
# ==========================================================
#
# La fuente PRINCIPAL de los registros de Unity es `GET /data`. Un registro
# de `/data` se correlaciona con la captura por `lens_participant_id` y, para
# capturas antiguas sin ese dato, por INTERSECCIÓN de intervalos temporales
# (la sesión de Unity puede COMENZAR antes de la captura de EmotionLab).
#
# Todo el registro se centraliza bajo un ÚNICO `session_id`: NUNCA se crea
# una sesión por escena, respuesta, modo o evento (`session_completed`).

# Captura existente a recuperar (prioridad de la Fase 0).
PRIORITY_SESSION_ID = "00000000-0000-4000-8000-000000000001"
PRIORITY_PARTICIPANT = "participant-test-001"


def seed_capture(
    service,
    session_id,
    *,
    owner_id="teacher-1",
    participant=None,
    start=None,
    end=None,
    status=STATUS_CAPTURING,
):
    """Registra una captura EXISTENTE con `session_id` fijo.

    Simula una experiencia creada con anterioridad (por ejemplo, la captura
    que quedó pendiente antes de corregir la correlación temporal).
    """

    service.session.create_capture_session(
        session_id,
        owner_id=owner_id,
        lens_participant_id=participant,
    )

    fields = {}
    if start is not None:
        fields["capture_started_at"] = start
    if end is not None:
        fields["capture_ended_at"] = end
    if fields:
        service.session.update_session_fields(session_id, fields)

    if status != STATUS_CAPTURING:
        service.session.transition_capture_status(
            session_id, status, allowed_from=STATUS_CAPTURING
        )

    return session_id


def test_lens_data_record_is_imported_by_participant():
    lens_data = FakeUnityData(records=[lens_data_record()])
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)

    view = service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    assert view["record_count"] == 1
    assert view["extraction"]["files_imported"] == 1
    assert view["extraction"]["errors"] == []

    # La identidad de EmotionLab NO se reemplaza por la del registro.
    session = service.session.get_session(PRIORITY_SESSION_ID)
    assert session["session_id"] == PRIORITY_SESSION_ID

    records = service.list_original_records(PRIORITY_SESSION_ID, actor_ctx=actor())
    assert len(records) == 1
    assert records[0]["source_key"].endswith(
        "participante_20300115_220926_participant-test-001.json"
    )

    # Una única sesión: no se crean sesiones por escena/respuesta/evento.
    assert len(stored_sessions(repo)) == 1


def test_priority_capture_recovers_full_unity_record():
    """Recupera la captura existente y centraliza el registro completo.

    El registro trae 14 respuestas y 11 eventos (incluido `session_completed`)
    y todo debe quedar bajo un ÚNICO `session_id`, sin sesiones adicionales.
    """

    eventos = [{"tipo": f"evento_{i}"} for i in range(10)]
    eventos.append({"tipo": "session_completed"})
    record = lens_data_record(eventos=eventos)

    lens_data = FakeUnityData(records=[record])
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    # Captura EXISTENTE sin `lens_participant_id` (recuperación temporal) que
    # quedó atascada antes de la corrección (`files_found: 0`). La sesión de
    # Unity COMIENZA antes de la captura, pero los intervalos se solapan.
    seed_capture(
        service,
        PRIORITY_SESSION_ID,
        start="2030-01-16T01:15:00Z",
        end="2030-01-16T01:30:00Z",
        status=STATUS_CAPTURE_IMPORT_ERROR,
    )

    view = service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURED
    assert view["extraction"]["files_found"] == 1
    assert view["extraction"]["files_imported"] == 1
    assert view["record_count"] == 1

    stored = service.list_original_records(PRIORITY_SESSION_ID, actor_ctx=actor())
    assert len(stored) == 1
    session_data = stored[0]["record"]["session_data"]
    assert len(session_data["respuestas"]) == 14
    assert len(session_data["eventos"]) == 11
    assert session_data["idParticipante"] == PRIORITY_PARTICIPANT

    # Todo bajo un ÚNICO `session_id`: ninguna sesión por escena, respuesta,
    # modo ni evento `session_completed`.
    assert len(stored_sessions(repo)) == 1


def test_lens_data_import_is_idempotent():
    lens_data = FakeUnityData(records=[lens_data_record()])
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)

    session = service.session
    first = service._extract_and_centralize(
        PRIORITY_SESSION_ID, session.get_session(PRIORITY_SESSION_ID), None
    )
    second = service._extract_and_centralize(
        PRIORITY_SESSION_ID, session.get_session(PRIORITY_SESSION_ID), None
    )

    assert first["files_imported"] == 1
    # La segunda pasada reconoce el origen y no vuelve a duplicar el registro.
    assert second["files_imported"] == 0
    assert (
        len(service.list_original_records(PRIORITY_SESSION_ID, actor_ctx=actor()))
        == 1
    )
    assert len(stored_sessions(repo)) == 1


def test_lens_data_ambiguity_blocks_import_and_creates_no_sessions():
    lens_data = FakeUnityData(records=[lens_data_record(), lens_data_record()])
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)

    view = service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    assert view["record_count"] == 0
    reasons = [item.get("reason") for item in view["extraction"]["errors"]]
    assert "SESSION_MATCH_AMBIGUOUS" in reasons
    # Nada se importa y no se crea ninguna sesión adicional.
    assert len(stored_sessions(repo)) == 1


def test_lens_data_without_candidate_stays_pending():
    lens_data = FakeUnityData(records=[lens_data_record(participant="otro")])
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)

    view = service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    reasons = [item.get("reason") for item in view["extraction"]["errors"]]
    assert "SIN_CANDIDATO" in reasons
    assert len(stored_sessions(repo)) == 1


def test_lens_data_fetch_failure_is_registered():
    lens_data = FakeUnityData(error=LensApiUnavailable("sin datos del lente"))
    service, repo, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)

    view = service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert view["status"] == STATUS_CAPTURE_IMPORT_ERROR
    stages = [item.get("stage") for item in view["extraction"]["errors"]]
    assert "fetch_unity_data" in stages
    assert len(stored_sessions(repo)) == 1


def test_unity_json_is_recoverable_by_session_id():
    lens_data = FakeUnityData(records=[lens_data_record()])
    service, *_ = build(aws_sessions=[], unity_data_service=lens_data)

    seed_capture(service, PRIORITY_SESSION_ID, participant=PRIORITY_PARTICIPANT)
    service.complete_experience(PRIORITY_SESSION_ID, actor_ctx=actor())

    original = service.load_unity_json(PRIORITY_SESSION_ID, actor_ctx=actor())

    assert original is not None
    assert original["session_data"]["idParticipante"] == PRIORITY_PARTICIPANT
    assert len(original["session_data"]["respuestas"]) == 14
    assert len(original["session_data"]["eventos"]) == 11


# ==========================================================
# 11. PROCESAMIENTO V8 AUTOMÁTICO AL FINALIZAR LA EXPERIENCIA
# ==========================================================
#
# Flujo esperado:
#
#   FINALIZACIÓN VR -> PERSISTENCIA DE CAPTURA -> ANÁLISIS V8 AUTOMÁTICO
#   -> ACTUALIZACIÓN DEL MISMO `session_id`
#
# El análisis corre DENTRO de `complete_experience`, después de centralizar
# los JSON (nunca antes de que las respuestas estén persistidas) y NUNCA crea
# una segunda sesión. La ruta manual `POST /captures/<id>/analyze` existe
# solo para REPROCESAR la MISMA sesión.

def v8_aws_record(unity_session_id="V8-001", s3_key="lens/v8.json"):
    """Synthetic complete Unity record addressed by the official V8 map."""
    model_dir = Path(__file__).resolve().parents[1] / "ia" / "modelo_v8"
    mapping = json.loads((model_dir / "mapeo_unity_ml_v1.json").read_text(encoding="utf-8"))
    labels = ("5", "6", "4", "7", "5", "6", "4", "7", "5", "6", "4", "7")
    respuestas = [
        {"idPregunta": item["idPregunta"], "escena": item["escena"], "respuestaLabel": labels[index]}
        for index, item in enumerate(mapping["equivalencias"])
    ]
    return {
        "unity_session_id": unity_session_id,
        "s3_key": s3_key,
        "session_data": {
            "idParticipante": "participante_demo_v8",
            "fechaInicio": "2030-01-15 13:00:00",
            "fechaFin": "2030-01-15 13:30:00",
            "respuestas": respuestas,
            "eventos": [
                {"nombre": "session_started", "escena": "waiting_room_inicial"},
                {"nombre": "session_completed", "escena": "Retroalimentacion_Salon"},
            ],
        },
    }


class _FakePipeline:
    """Pipeline K-Means simulado: devuelve siempre el mismo grupo."""

    def __init__(self, group):
        self.group = group

    def predict(self, matriz):
        return [self.group]


class CountingEmotionV8:
    """V8 service with a deterministic, non-Joblib test generator."""

    def __init__(self, group=1, fail=False):
        self.calls = 0
        self.fail = fail
        self.group = group
        self.service = EmotionModelV8Service(generator=self)
        self.model_version = self.service.model_version

    def analyze(self, session_id, record):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fallo simulado del generador V8")
        return self.service.analyze(session_id, record)

    def error_block(self, error, session_id=None):
        return self.service.error_block(error, session_id)

    def procesar_sesion(self, record):
        values = EmotionV8Parser(record).get_model_values()
        observed = sum(value is not None for value in values.values())
        if observed == 12:
            status = "calculado_sin_estimacion"
            group = self.group
            processed = dict(values)
            missing = []
        else:
            status = "informacion_insuficiente"
            group = None
            processed = None
            missing = [name for name, value in values.items() if value is None]
        return {
            "version_contrato": "1.1.0-experimental",
            "id_participante": record.get("session_data", {}).get("idParticipante"),
            "estado": status,
            "grupo": group,
            "observadas": observed,
            "estimadas": [],
            "fuentes_imputacion": {},
            "variables_sin_datos_suficientes": missing,
            "variables_originales": values,
            "variables_procesadas": processed,
            "error": None,
            "perfil_tension": None,
            "indice_relativo_centroides": None,
            "mensaje": None,
        }


def v8_build(engine):
    return build(aws_sessions=[v8_aws_record()], emotion_v2_service=engine)


def test_complete_triggers_v8_analysis_automatically():
    model = CountingEmotionV8(group=1)
    service, repo, *_ = v8_build(model)

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    # 1. La finalización deja la captura cerrada...
    assert view["status"] == STATUS_CAPTURED
    # 2. ...y el modelo se ejecutó UNA sola vez, sin llamada manual.
    assert model.calls == 1

    session = service.session.get_session(session_id)
    block = session["model_processing"]

    # V8 conserva el ID K-Means original y no deriva una etiqueta binaria.
    assert block["status"] == STATUS_ANALYZED
    assert block["prediction"]["group"] == block["grupo"] == 1
    assert block["n_clusters"] == 2
    assert block["algorithm"] == "KMeans"
    assert block["model_version"] == "1.0.0-experimental"
    assert block["tension_index"]["value"] is None
    assert "classification_final" not in block
    assert "wellbeing" not in block

    # El procesamiento debe leer los datos que se acaban de persistir durante
    # esta misma finalización (no el snapshot previo a la extracción).
    assert block["variables_originales"]["estado_pre"] is not None
    original_records = service.list_original_records(session_id, actor_ctx=actor())
    assert len(original_records) == 1
    assert len(original_records[0]["record"]["session_data"]["respuestas"]) == 12

    # 3 y 5. El resultado vive en la MISMA sesión: no se crea una segunda.
    assert session["session_id"] == session_id
    assert len(stored_sessions(repo)) == 1
    assert service.session.get_session(session_id) is not None
    assert service.session.get_capture_slot() is None


def test_complete_runs_the_model_only_once_per_finalization():
    model = CountingEmotionV8(group=1)
    service, *_ = v8_build(model)

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())
    # Segunda confirmación (idempotente): no vuelve a inferir.
    service.complete_experience(session_id, actor_ctx=actor())

    assert model.calls == 1


def test_v8_failure_preserves_capture_and_records_the_error():
    model = CountingEmotionV8(fail=True)
    service, repo, *_ = v8_build(model)

    session_id = open_window(service)
    view = service.complete_experience(session_id, actor_ctx=actor())

    # 6. La captura NO se pierde ni se invalida.
    assert view["status"] == STATUS_CAPTURED
    assert model.calls == 1

    block = service.session.get_session(session_id)["model_processing"]

    # El error de procesamiento queda registrado de forma controlada.
    assert block["status"] == STATUS_PROCESSING_ERROR
    assert block["error"]
    assert block["grupo"] is None
    assert block["tension_index"]["value"] is None
    assert "classification_final" not in block

    # Los datos originales siguen intactos.
    records = service.list_original_records(session_id, actor_ctx=actor())
    assert len(records) == 1
    assert records[0]["record"]["session_data"]["respuestas"]
    assert len(stored_sessions(repo)) == 1


def test_v8_retry_analyzes_the_same_session_without_duplicating():
    model = CountingEmotionV8(fail=True)
    service, repo, *_ = v8_build(model)

    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())

    # Reintento manual sobre la MISMA sesión (el motor ya no falla).
    model.fail = False
    view = service.analyze_experience(session_id, actor_ctx=actor())

    # 7. Se procesa la misma sesión: no se duplica.
    assert view["model_processing"]["status"] == STATUS_ANALYZED
    assert view["model_processing"]["grupo"] == 1
    assert "classification_final" not in view["model_processing"]
    assert model.calls == 2
    assert len(stored_sessions(repo)) == 1
    assert stored_sessions(repo)[0]["session_id"] == session_id

    # Un segundo reintento reutiliza el resultado (idempotencia).
    service.analyze_experience(session_id, actor_ctx=actor())
    assert model.calls == 2


def test_automatic_flow_uses_real_v8_when_exact_runtime_is_available():
    """Loads the frozen Joblib artifacts only under their exact V8 runtime."""
    real = EmotionModelV8Service()
    try:
        real.generator
    except RuntimeError as error:
        if "requiere Python 3.13.18" in str(error):
            pytest.skip(f"Inferencia V8 real pendiente: {error}")
        raise

    service, repo, *_ = v8_build(real)
    session_id = open_window(service)
    service.complete_experience(session_id, actor_ctx=actor())
    block = service.session.get_session(session_id)["model_processing"]
    assert block["status"] == STATUS_ANALYZED
    assert block["algorithm"] == "KMeans"
    assert block["n_clusters"] == 2
    assert block["grupo"] in (0, 1)
    assert block["tension_index"]["value"] is None
    assert len(stored_sessions(repo)) == 1


# ==========================================================
# 12. PRUEBA INTEGRAL: SECUENCIA REAL DEL CLIENTE POR HTTP
# ==========================================================
#
# Reproduce, extremo a extremo y contra los endpoints HTTP reales, la misma
# secuencia que ejecuta el cliente de Unity (opción «Finalizar y
# guardar») tras terminar la experiencia VR. No toca AWS real: usa los dobles
# en memoria de este módulo.
#
#   1. POST /captures                     -> crear sesión
#   2. POST /captures/<id>/records        -> registrar respuestas y eventos
#   3. POST /captures/<id>/finalize       -> cierre de la experiencia VR
#   4. POST /captures/<id>/complete       -> transferencia lista + ANÁLISIS V8
#   5. GET  /captures/<id>                -> MISMOS session_id
#   6. GET  /captures/<id>/records        -> datos originales intactos
#
# El análisis V8 se dispara DENTRO del paso 4 (nunca antes de persistir los
# datos) y NO requiere la llamada manual a POST /captures/<id>/analyze.

# Registro «en vivo» que el cliente sube antes de finalizar: aporta respuestas
# y eventos de la experiencia sin incluir las seis variables del modelo.
V8_LIVE_RECORD = {
    "unity_session_id": "V8-LIVE-1",
    "session_data": {
            "idParticipante": "participante_demo_v8",
        "fechaInicio": "2030-01-15 13:00:00",
        "fechaFin": "2030-01-15 13:30:00",
        "respuestas": [
            {"idPregunta": "pregunta_demo", "respuestaLabel": "5",
             "escena": "waiting_room_inicial"},
        ],
        "eventos": [
            {"nombre": "session_started", "escena": "waiting_room_inicial"},
            {"nombre": "session_completed",
             "escena": "Retroalimentacion_Salon"},
        ],
    },
}


def test_integral_client_sequence_triggers_v8_and_keeps_same_session():
    """Secuencia completa del cliente por HTTP, con V8 automático."""

    model = CountingEmotionV8(group=1)
    service, repo, *_ = v8_build(model)
    client = route_client(service)

    # 1. Crear la sesión (equivalente a iniciar la experiencia).
    created = client.post("/captures", json={}, headers=HEADERS)
    assert created.status_code == 201
    session_id = created.get_json()["session_id"]
    uuid.UUID(session_id)

    # 2. Registrar respuestas y eventos de la experiencia.
    ingested = client.post(
        f"/captures/{session_id}/records",
        json={"records": [dict(V8_LIVE_RECORD)]},
        headers=HEADERS,
    )
    assert ingested.status_code == 202

    # 3. Finalizar la experiencia VR.
    finalized = client.post(
        f"/captures/{session_id}/finalize", headers=HEADERS
    )
    assert finalized.status_code == 200

    # Fija la ventana de captura para que el registro de Unity (fecha fija
    # del fixture) caiga dentro del intervalo, igual que en la captura real.
    service.session.update_session_fields(
        session_id,
        {"capture_started_at": W_START, "capture_ended_at": W_END},
    )

    # 4. Transferencia confirmada -> el análisis V8 corre automáticamente.
    completed = client.post(
        f"/captures/{session_id}/complete", headers=HEADERS
    )
    assert completed.status_code == 200
    assert completed.get_json()["status"] == STATUS_CAPTURED
    assert model.calls == 1  # se ejecutó una sola vez, sin llamada manual

    # 5. Consultar el MISMO session_id (no se creó otro).
    status = client.get(f"/captures/{session_id}", headers=HEADERS)
    assert status.status_code == 200
    assert status.get_json()["session_id"] == session_id

    # 6. Verificar el resultado almacenado en la MISMA sesión.
    session = service.session.get_session(session_id)
    block = session["model_processing"]

    assert session["session_id"] == session_id
    assert block["status"] == STATUS_ANALYZED
    assert block["model_version"] == "1.0.0-experimental"
    assert block["grupo"] == 1
    assert block["n_clusters"] == 2
    assert block["tension_index"]["value"] is None
    assert "classification_final" not in block
    assert "wellbeing" not in block

    # Los datos originales siguen accesibles por el endpoint de registros.
    records = client.get(
        f"/captures/{session_id}/records", headers=HEADERS
    )
    assert records.status_code == 200
    assert len(records.get_json()["records"]) >= 1

    # No existe una segunda sesión: todo quedó bajo el mismo session_id.
    assert len(stored_sessions(repo)) == 1
    assert stored_sessions(repo)[0]["session_id"] == session_id


def test_integral_sequence_runs_v8_only_once_across_repeated_complete():
    """Repetir /complete no vuelve a inferir ni duplica la sesión."""

    model = CountingEmotionV8(group=1)
    service, repo, *_ = v8_build(model)
    client = route_client(service)

    session_id = client.post(
        "/captures", json={}, headers=HEADERS
    ).get_json()["session_id"]
    client.post(
        f"/captures/{session_id}/records",
        json={"records": [dict(V8_LIVE_RECORD)]},
        headers=HEADERS,
    )
    client.post(f"/captures/{session_id}/finalize", headers=HEADERS)
    service.session.update_session_fields(
        session_id,
        {"capture_started_at": W_START, "capture_ended_at": W_END},
    )
    client.post(f"/captures/{session_id}/complete", headers=HEADERS)
    # Segunda confirmación de la misma experiencia (idempotente).
    again = client.post(
        f"/captures/{session_id}/complete", headers=HEADERS
    )

    assert again.status_code == 200
    assert again.get_json()["status"] == STATUS_CAPTURED
    assert model.calls == 1
    assert len(stored_sessions(repo)) == 1

