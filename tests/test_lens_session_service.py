"""Pruebas del servicio que identifica sesiones del lente por displayCode.

La integración real usa el **API Gateway de los lentes** por HTTPS
(`POST {LENS_API_BASE_URL}/session/confirm`); NO se accede a DynamoDB de la
cuenta externa. Estas pruebas sustituyen el cliente HTTP por un doble para
verificar:

    - la validación sin consumir el PIN (`validateOnly = true`)
    - la confirmación real e idempotente (`validateOnly = false`)
    - la traducción de respuestas HTTP a errores tipados
    - el limitador de intentos LOCAL a EmotionLab
    - que `sessionToken` nunca se exponga
"""

import requests
import pytest

from flask import Flask

from botocore.exceptions import ClientError

from core.constants import (
    AUDIT_SESSION_CODE_RESOLVE_FAILED,
    AUDIT_SESSION_CODE_RESOLVED,
    DYNAMODB_SESSION_CODE_RATE_LIMITS,
)
from core.roles import ROLE_TEACHER
from routes.session_code_routes import session_codes_bp
from services.lens_session_service import (
    LensApiConfirmationAmbiguous,
    LensApiError,
    LensApiRateLimited,
    LensApiUnavailable,
    LensSessionConflict,
    LensSessionExpired,
    LensSessionNotFound,
    LensSessionService,
    SessionCodeRateLimitExceeded,
    SessionCodeRateLimitUnavailable,
)


VALID_CODE = "482913"
# `sessionId` original que genera la Lambda `createSession`. Es la
# identidad permanente de la experiencia y de todos sus datos.
LENS_SESSION_ID = "6f1c0b9e-0000-4000-8000-000000000001"
BASE_URL = "https://lens.example/prod"


# ==========================================================
# DOBLES
# ==========================================================


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttp:
    """Cliente HTTP doble. Registra cada llamada y devuelve respuestas."""

    def __init__(self, responses=None, response=None):
        if responses is not None:
            self._queue = list(responses)
        else:
            self._queue = None
        self._response = response
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(
            {"url": url, "json": dict(json or {}), "timeout": timeout}
        )

        if self._queue is not None:
            item = self._queue.pop(0)
        else:
            item = self._response

        # Una excepción simula un fallo de red.
        if isinstance(item, Exception):
            raise item

        return item


class FakeSSM:
    def get_session_code_hmac_secret(self):
        return "test-secret-key-for-session-code-hmac-0001x"


class MissingSecretSSM:
    """Simula un secreto ausente o no configurado."""

    def get_session_code_hmac_secret(self):
        return None


class ShortSecretSSM:
    """Simula un secreto por debajo de la longitud mínima."""

    def get_session_code_hmac_secret(self):
        return "demasiado-corto"


class FakeAuthService:
    """Especialista autenticado, sin Cognito real."""

    def verify_token(self, token):
        return {
            "sub": "teacher-1",
            "email": "docente@example.com",
            "rol": ROLE_TEACHER,
        }

    def get_current_user(self, token):
        return {
            "uid": "teacher-1",
            "email": "docente@example.com",
            "rol": ROLE_TEACHER,
        }


def _route_client(service):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": FakeAuthService(),
        "lens_session_service": service,
    }
    app.register_blueprint(session_codes_bp)
    return app.test_client()


class FakeAudit:
    def __init__(self):
        self.events = []

    def log(self, action, **kwargs):
        self.events.append({"action": action, **kwargs})


class FakeDynamoDB:
    """Solo el limitador LOCAL de EmotionLab (nunca tablas del lente)."""

    def __init__(self):
        self.rate_limits = {}
        self.deleted = []
        self.get_error = None

    def get_item(self, table_name, key, consistent_read=False):
        if self.get_error is not None:
            raise self.get_error
        if table_name == DYNAMODB_SESSION_CODE_RATE_LIMITS:
            item = self.rate_limits.get(key["rate_key"])
            return dict(item) if item else None
        return None

    def transact_write_items(self, operations):
        updates = []
        puts = []
        for operation in operations:
            update = operation.get("Update")
            put = operation.get("Put")
            if update and update["TableName"] == DYNAMODB_SESSION_CODE_RATE_LIMITS:
                rate_key = update["Key"]["rate_key"]
                values = update["ExpressionAttributeValues"]
                record = self.rate_limits.get(rate_key)
                attempts = record.get("attempts", 0) if record else 0
                if attempts >= values[":max_attempts"]:
                    raise ClientError(
                        {
                            "Error": {"Code": "TransactionCanceledException", "Message": "condition failed"},
                            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                        },
                        "TransactWriteItems",
                    )
                updates.append((rate_key, values))
            elif put and put["TableName"] == DYNAMODB_SESSION_CODE_RATE_LIMITS:
                rate_key = put["Item"]["rate_key"]
                if rate_key in self.rate_limits:
                    raise ClientError(
                        {
                            "Error": {"Code": "TransactionCanceledException", "Message": "condition failed"},
                            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                        },
                        "TransactWriteItems",
                    )
                puts.append(put["Item"])

        for rate_key, values in updates:
            record = self.rate_limits.get(rate_key)
            attempts = record.get("attempts", 0) if record else 0
            if record is None:
                record = {"rate_key": rate_key, "attempts": 0}
                self.rate_limits[rate_key] = record
            record["attempts"] = attempts + 1
            record["expires_at"] = values[":expires_at"]
        for item in puts:
            self.rate_limits[item["rate_key"]] = dict(item)

        return {}

    def delete_item(self, table_name, key, condition_expression=None):
        if table_name == DYNAMODB_SESSION_CODE_RATE_LIMITS:
            self.rate_limits.pop(key["rate_key"], None)

        self.deleted.append((table_name, key))
        return True


def _ok_payload(**overrides):
    """Cuerpo que devuelve la Lambda `confirmSession` en caso exitoso."""

    data = {
        "sessionId": LENS_SESSION_ID,
        "displayCode": VALID_CODE,
        "sessionToken": "private-token-value",
        "idParticipante": "participante_20260101_000000_abcd1234",
        "status": "pending",
        "createdAt": "2026-01-01T10:00:00",
    }
    data.update(overrides)
    return data


def _build(http=None, dynamo=None, ssm=None, base_url=BASE_URL, timeout=5):
    dynamo = dynamo or FakeDynamoDB()
    audit = FakeAudit()
    service = LensSessionService(
        dynamodb_service=dynamo,
        audit_svc=audit,
        ssm_service=ssm or FakeSSM(),
        base_url=base_url,
        timeout=timeout,
        http_client=http or FakeHttp(response=FakeResponse(200, _ok_payload())),
    )
    return service, dynamo, audit


# ==========================================================
# VALIDACIÓN (validateOnly)
# ==========================================================


def test_valid_code_resolves_lens_session():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, audit = _build(http=http)

    session = service.resolve_by_display_code(
        VALID_CODE,
        client_ip="203.0.113.10",
    )

    assert session["session_id"] == LENS_SESSION_ID
    assert session["display_code"] == VALID_CODE
    assert session["capture_status"] == "pending"
    assert [event["action"] for event in audit.events] == [
        AUDIT_SESSION_CODE_RESOLVED
    ]
    assert VALID_CODE not in repr(audit.events)


def test_validate_uses_validate_only_flag():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, _ = _build(http=http)

    service.validate_display_code(VALID_CODE)

    call = http.calls[0]
    assert call["url"] == BASE_URL + "/session/confirm"
    assert call["json"] == {"displayCode": VALID_CODE, "validateOnly": True}


def test_resolution_does_not_claim_a_processed_session():
    service, _, _ = _build()

    session = service.resolve_by_display_code(
        VALID_CODE,
        client_ip="203.0.113.10",
    )

    assert "processed_session_id" not in session
    assert "tracking_id" not in session
    assert "linkable" not in session
    assert "capture_session_id" not in session


def test_session_token_is_never_exposed():
    service, _, _ = _build()

    session = service.resolve_by_display_code(
        VALID_CODE,
        client_ip="203.0.113.10",
    )

    assert "sessionToken" not in session
    assert "sessionToken" not in str(session)


def test_participant_id_is_exposed_when_present():
    service, _, _ = _build()

    session = service.resolve_by_display_code(
        VALID_CODE,
        client_ip="203.0.113.10",
    )

    assert session["participant_id"] == (
        "participante_20260101_000000_abcd1234"
    )


# ==========================================================
# CONFIRMACIÓN (validateOnly = false)
# ==========================================================


def test_confirm_sends_full_payload():
    http = FakeHttp(
        response=FakeResponse(200, _ok_payload(status="confirmed"))
    )
    service, _, _ = _build(http=http)

    session = service.confirm_display_code(
        VALID_CODE,
        specialist_id="teacher-1",
        confirmation_request_id="confirm-abc",
        patient_id="paciente-9",
    )

    call = http.calls[0]
    assert call["json"] == {
        "displayCode": VALID_CODE,
        "validateOnly": False,
        "specialistId": "teacher-1",
        "patientId": "paciente-9",
        "confirmationRequestId": "confirm-abc",
    }
    assert session["session_id"] == LENS_SESSION_ID
    assert session["capture_status"] == "confirmed"


def test_confirm_without_request_id_omits_field():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, _ = _build(http=http)

    service.confirm_display_code(VALID_CODE, specialist_id="teacher-1")

    assert "confirmationRequestId" not in http.calls[0]["json"]


def test_confirm_never_exposes_session_token():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, _ = _build(http=http)

    session = service.confirm_display_code(
        VALID_CODE,
        specialist_id="teacher-1",
        confirmation_request_id="confirm-abc",
    )

    assert "sessionToken" not in session
    assert "sessionToken" not in str(session)


# ==========================================================
# FORMATO Y TRADUCCIÓN DE ERRORES HTTP
# ==========================================================


def test_invalid_format_is_rejected_without_http_call():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, dynamo, _ = _build(http=http)

    for invalid in ("12345", "1234567", "abcdef", "", "12345a"):
        with pytest.raises(ValueError):
            service.resolve_by_display_code(
                invalid,
                client_ip="203.0.113.10",
            )

    # Un formato inválido no llega a la API ni consume intentos.
    assert http.calls == []
    assert dynamo.rate_limits == {}


def test_unknown_code_is_rejected_and_counted():
    http = FakeHttp(response=FakeResponse(404, {"error": "not found"}))
    service, dynamo, audit = _build(http=http)

    with pytest.raises(LensSessionNotFound):
        service.resolve_by_display_code(
            "000000",
            client_ip="203.0.113.10",
        )

    assert len(dynamo.rate_limits) == 1
    assert [event["action"] for event in audit.events] == [
        AUDIT_SESSION_CODE_RESOLVE_FAILED
    ]
    assert "000000" not in repr(audit.events)


def test_expired_code_is_mapped_to_expired():
    http = FakeHttp(response=FakeResponse(410, {"error": "gone"}))
    service, _, _ = _build(http=http)

    with pytest.raises(LensSessionExpired):
        service.resolve_by_display_code(
            VALID_CODE,
            client_ip="203.0.113.10",
        )


def test_conflict_is_mapped_to_conflict():
    http = FakeHttp(response=FakeResponse(409, {"error": "confirmed"}))
    service, _, _ = _build(http=http)

    with pytest.raises(LensSessionConflict):
        service.validate_display_code(VALID_CODE)


def test_bad_request_is_mapped_to_value_error():
    http = FakeHttp(response=FakeResponse(400, {"error": "bad"}))
    service, _, _ = _build(http=http)

    with pytest.raises(ValueError):
        service.validate_display_code(VALID_CODE)


def test_remote_rate_limit_is_mapped():
    http = FakeHttp(response=FakeResponse(429, {"error": "slow down"}))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiRateLimited):
        service.validate_display_code(VALID_CODE)


def test_server_error_is_unavailable():
    http = FakeHttp(response=FakeResponse(500, {"error": "boom"}))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiUnavailable):
        service.validate_display_code(VALID_CODE)


def test_connection_error_is_unavailable():
    http = FakeHttp(response=requests.exceptions.ConnectionError("network"))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiUnavailable):
        service.validate_display_code(VALID_CODE)


def test_confirm_ambiguous_timeout_is_distinguishable():
    """Un timeout al confirmar es ambiguo: el resultado es indeterminado."""

    http = FakeHttp(response=requests.exceptions.ReadTimeout("slow"))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiConfirmationAmbiguous):
        service.confirm_display_code(VALID_CODE, specialist_id="teacher-1")

    # Sigue siendo tratable como indisponibilidad genérica.
    assert issubclass(LensApiConfirmationAmbiguous, LensApiUnavailable)
    assert len(http.calls) == 1


def test_validate_timeout_is_not_ambiguous():
    """Un timeout al validar NO se marca como confirmación indeterminada."""

    http = FakeHttp(response=requests.exceptions.ReadTimeout("slow"))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiUnavailable) as excinfo:
        service.validate_display_code(VALID_CODE)

    assert not isinstance(
        excinfo.value, LensApiConfirmationAmbiguous
    )


def test_invalid_json_is_api_error():
    http = FakeHttp(
        response=FakeResponse(200, ValueError("no json"))
    )
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiError):
        service.validate_display_code(VALID_CODE)


def test_success_without_session_id_is_api_error():
    http = FakeHttp(response=FakeResponse(200, {"status": "pending"}))
    service, _, _ = _build(http=http)

    with pytest.raises(LensApiError):
        service.validate_display_code(VALID_CODE)


def test_missing_base_url_fails_closed(monkeypatch):
    monkeypatch.delenv("LENS_API_BASE_URL", raising=False)
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, _ = _build(http=http, base_url="")

    with pytest.raises(RuntimeError):
        service.validate_display_code(VALID_CODE)

    assert http.calls == []


# ==========================================================
# LIMITADOR LOCAL DE INTENTOS
# ==========================================================


def test_rate_limit_blocks_sixth_failed_attempt():
    http = FakeHttp(response=FakeResponse(404, {"error": "not found"}))
    service, dynamo, _ = _build(http=http)

    for _ in range(5):
        with pytest.raises(LensSessionNotFound):
            service.resolve_by_display_code(
                "000000",
                client_ip="203.0.113.10",
            )

    with pytest.raises(SessionCodeRateLimitExceeded):
        service.resolve_by_display_code(
            "000000",
            client_ip="203.0.113.10",
        )

    assert len(dynamo.rate_limits) == 1
    assert next(iter(dynamo.rate_limits.values()))["attempts"] == 5


def test_rate_limit_is_isolated_by_origin():
    http = FakeHttp(
        responses=[
            FakeResponse(404, {"error": "not found"}),
            FakeResponse(404, {"error": "not found"}),
            FakeResponse(404, {"error": "not found"}),
            FakeResponse(404, {"error": "not found"}),
            FakeResponse(404, {"error": "not found"}),
            FakeResponse(200, _ok_payload()),
        ]
    )
    service, dynamo, _ = _build(http=http)

    for _ in range(5):
        with pytest.raises(LensSessionNotFound):
            service.resolve_by_display_code(
                "000000",
                client_ip="203.0.113.10",
            )

    # Otro origen mantiene el acceso disponible.
    session = service.resolve_by_display_code(
        VALID_CODE,
        client_ip="198.51.100.7",
    )

    assert session["session_id"] == LENS_SESSION_ID
    assert len(dynamo.rate_limits) == 1


def test_successful_resolution_resets_counter():
    http = FakeHttp(response=FakeResponse(404, {"error": "not found"}))
    service, dynamo, _ = _build(http=http)

    for _ in range(4):
        with pytest.raises(LensSessionNotFound):
            service.resolve_by_display_code(
                "000000",
                client_ip="203.0.113.10",
            )

    http._response = FakeResponse(200, _ok_payload())

    service.resolve_by_display_code(
        VALID_CODE,
        client_ip="203.0.113.10",
    )

    assert dynamo.rate_limits == {}


def test_invalid_client_ip_is_rejected():
    service, _, _ = _build()

    with pytest.raises(ValueError):
        service.resolve_by_display_code(
            VALID_CODE,
            client_ip="not-an-ip",
        )


def test_missing_hmac_secret_fails_closed():
    """Sin la clave HMAC no se omite el limitador: falla antes de resolver."""

    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    dynamo = FakeDynamoDB()

    for ssm in (None, MissingSecretSSM(), ShortSecretSSM()):
        service = LensSessionService(
            dynamodb_service=dynamo,
            audit_svc=FakeAudit(),
            ssm_service=ssm,
            base_url=BASE_URL,
            http_client=http,
        )

        with pytest.raises(RuntimeError):
            service.resolve_by_display_code(
                VALID_CODE,
                client_ip="203.0.113.10",
            )

    assert dynamo.rate_limits == {}
    assert http.calls == []


def test_lookup_delegates_without_rate_limit():
    """La validación automática (`lookup_by_display_code`) omite el limitador."""

    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, dynamo, audit = _build(http=http)

    session = service.lookup_by_display_code(VALID_CODE)

    assert session["session_id"] == LENS_SESSION_ID
    assert dynamo.rate_limits == {}
    assert audit.events == []


# ==========================================================
# CONTRATO DE LA RUTA HTTP
# ==========================================================


def test_route_response_cannot_be_read_as_processed_session():
    http = FakeHttp(response=FakeResponse(200, _ok_payload()))
    service, _, _ = _build(http=http)
    client = _route_client(service)

    response = client.post(
        "/sessions/resolve-display-code",
        json={"display_code": VALID_CODE},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200

    data = response.get_json()

    assert set(data.keys()) == {
        "session_id",
        "display_code",
        "capture_status",
        "created_at",
        "participant_id",
    }
    assert data["session_id"] == LENS_SESSION_ID
    assert "processed_session_id" not in data
    assert "tracking_id" not in data
    assert "capture_session_id" not in data
    assert "sessionToken" not in data


@pytest.mark.parametrize(
    "status,expected",
    [
        (404, 404),
        (410, 410),
        (409, 409),
        (429, 429),
        (500, 503),
    ],
)
def test_route_maps_remote_errors(status, expected):
    http = FakeHttp(response=FakeResponse(status, {"error": "x"}))
    service, _, _ = _build(http=http)
    client = _route_client(service)

    response = client.post(
        "/sessions/resolve-display-code",
        json={"display_code": VALID_CODE},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == expected


def test_route_maps_invalid_payload_to_502():
    """Una respuesta no interpretable del lente se informa como 502."""

    http = FakeHttp(response=FakeResponse(200, {"status": "pending"}))
    service, _, _ = _build(http=http)
    client = _route_client(service)

    response = client.post(
        "/sessions/resolve-display-code",
        json={"display_code": VALID_CODE},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 502


def test_capture_rate_limit_deduplicates_failed_idempotent_request():
    service, dynamo, _audit = _build()
    args = {
        "client_ip": "203.0.113.25",
        "actor_uid": "teacher-1",
        "request_token": "capture-attempt-1",
        "failure_code": "not_found",
    }

    assert service.register_capture_failure(**args) is True
    assert service.register_capture_failure(**args) is False
    assert service.check_capture_attempt(
        client_ip=args["client_ip"],
        actor_uid=args["actor_uid"],
        request_token=args["request_token"],
    ) == "not_found"

    counter = next(
        value for value in dynamo.rate_limits.values()
        if "attempts" in value
    )
    marker = next(
        value for value in dynamo.rate_limits.values()
        if value.get("failure_code") == "not_found"
    )
    assert counter["attempts"] == 1
    assert args["request_token"] not in marker["rate_key"]


def test_capture_rate_limit_storage_error_is_not_reported_as_bad_pin():
    dynamo = FakeDynamoDB()
    dynamo.get_error = RuntimeError("tabla inexistente")
    service, _, _ = _build(dynamo=dynamo)

    with pytest.raises(SessionCodeRateLimitUnavailable):
        service.check_capture_attempt(
            client_ip="203.0.113.26",
            actor_uid="teacher-1",
            request_token="capture-attempt-2",
        )
