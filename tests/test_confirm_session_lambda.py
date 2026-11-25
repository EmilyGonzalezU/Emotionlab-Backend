"""Pruebas de la Lambda externa `emotionlabtest-confirmSession`.

La Lambda pertenece a la cuenta de los lentes y NO se despliega desde este
repositorio; estas pruebas validan su contrato de forma aislada con una
tabla DynamoDB en memoria que interpreta las condiciones reales de
`boto3.dynamodb.conditions` (`Key`/`Attr`):

    validateOnly = true   -> valida sin modificar el estado.
    validateOnly = false  -> confirma (pending -> confirmed) con idempotencia.

Reglas verificadas:

    - Las llamadas usan las condiciones correctas (GSI y TTL).
    - La vigencia se decide con `ttl`; sin `ttl` válido no se confirma.
    - `sessionToken` nunca se expone.
    - Los errores de DynamoDB se informan como 500 (nunca como 409 espurio).
    - Se recorren todas las páginas del GSI.
    - Las respuestas (éxito y error) incluyen encabezados CORS.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError

import integrations.lens_api.confirmSession_lambda as confirm_session
from integrations.lens_api.confirmSession_lambda import (
    DEFAULT_DISPLAY_CODE_INDEX,
    DEFAULT_SPECIALIST_ID,
    DEFAULT_TABLE_NAME,
    REQUIRED_INDEX_ATTRIBUTES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    _get_table,
    _handle,
    _query_by_display_code,
)


DISPLAY_CODE = "482913"
SESSION_ID = "6f1c0b9e-0000-4000-8000-000000000001"
OTHER_SESSION_ID = "6f1c0b9e-0000-4000-8000-000000000002"
TOKEN = "private-token-value"

MINE_RID = "11111111-1111-4111-8111-111111111111"
OTHER_RID = "22222222-2222-4222-8222-222222222222"

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())
FUTURE_TTL = int((NOW + timedelta(minutes=20)).timestamp())
PAST_TTL = int((NOW - timedelta(minutes=20)).timestamp())


# ==========================================================
# DOBLES
# ==========================================================


def _eval_condition(condition, item):
    """Evalúa una condición de boto3 (`Key`/`Attr`) sobre un item."""

    expr = condition.get_expression()
    operator = expr["operator"]
    values = expr["values"]

    if operator == "AND":
        return all(_eval_condition(value, item) for value in values)

    if operator == "OR":
        return any(_eval_condition(value, item) for value in values)

    attribute, expected = values
    actual = item.get(attribute.name)

    if operator == "=":
        return actual == expected

    if operator == ">":
        try:
            return int(actual) > int(expected)
        except (TypeError, ValueError):
            return False

    raise AssertionError(f"Operador no soportado: {operator}")


class FakeTable:
    """Tabla `emotionlab-sessions` en memoria (PK sessionId, GSI displayCode)."""

    def __init__(
        self,
        items=None,
        *,
        page_size=None,
        stale_queries=0,
        query_error=None,
        update_error=None,
        get_error=None,
        on_update=None,
    ):
        self.items = {
            item["sessionId"]: dict(item) for item in (items or [])
        }
        self.page_size = page_size
        self.stale_queries = stale_queries
        self.query_error = query_error
        self.update_error = update_error
        self.get_error = get_error
        self.on_update = on_update

        self.query_calls = []
        self.update_calls = []
        self.get_calls = []
        self.queried_index = None

    # -------- lectura (GSI) --------

    def query(self, IndexName=None, KeyConditionExpression=None,
              ExclusiveStartKey=None):
        if self.stale_queries > 0:
            self.stale_queries -= 1
            self.query_calls.append({"stale": True})
            return {"Items": []}

        if self.query_error is not None:
            raise self.query_error

        self.queried_index = IndexName
        self.query_calls.append(
            {
                "index": IndexName,
                "condition": KeyConditionExpression,
                "exclusive_start": ExclusiveStartKey,
            }
        )

        matched = [
            dict(item)
            for item in self.items.values()
            if _eval_condition(KeyConditionExpression, item)
        ]

        start = 0
        if ExclusiveStartKey is not None:
            start = ExclusiveStartKey["__start__"]

        if self.page_size:
            page = matched[start:start + self.page_size]
            response = {"Items": page}
            if start + self.page_size < len(matched):
                response["LastEvaluatedKey"] = {
                    "__start__": start + self.page_size
                }
            return response

        return {"Items": matched}

    def get_item(self, Key, ConsistentRead=False):
        if self.get_error is not None:
            raise self.get_error

        self.get_calls.append({"key": Key, "consistent": ConsistentRead})

        item = self.items.get(Key["sessionId"])
        return {"Item": dict(item)} if item else {}

    # -------- escritura condicional --------

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues,
                    ReturnValues):
        if self.update_error is not None:
            raise self.update_error

        self.update_calls.append(
            {
                "key": Key,
                "update": UpdateExpression,
                "condition": ConditionExpression,
                "values": ExpressionAttributeValues,
            }
        )

        item = self.items.get(Key["sessionId"])

        if item is not None and self.on_update is not None:
            # Simula que otra solicitud commitea primero.
            self.on_update(item)

        if item is None or not _eval_condition(ConditionExpression, item):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "condition failed",
                    }
                },
                "UpdateItem",
            )

        values = ExpressionAttributeValues
        item["status"] = values[":confirmed"]
        item["specialistId"] = values[":specialist"]
        item["patientId"] = values[":patient"]
        item["confirmedAt"] = values[":confirmed_at"]

        if ":rid" in values:
            item["confirmationRequestId"] = values[":rid"]

        return {"Attributes": dict(item)}


def _pending_item(**overrides):
    item = {
        "sessionId": SESSION_ID,
        "displayCode": DISPLAY_CODE,
        "sessionToken": TOKEN,
        "idParticipante": "participante_20260101_000000_abcd1234",
        "status": STATUS_PENDING,
        "createdAt": "2026-01-01T11:59:00",
        "ttl": FUTURE_TTL,
    }
    item.update(overrides)
    return item


def _no_sleep(_delay):
    return None


def _event(**payload):
    return {"body": json.dumps(payload)}


def _parse(response):
    raw = response["body"]
    return response["statusCode"], json.loads(raw), raw


def _call(table, sleep=None, method=None, **payload):
    event = _event(**payload)
    if method:
        event["httpMethod"] = method
    return _parse(_handle(event, table=table, now=NOW, sleep=sleep or _no_sleep))


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


# ==========================================================
# CONDICIONES DE DYNAMODB
# ==========================================================


def test_query_uses_gsi_key_condition():
    table = FakeTable([_pending_item()])

    _call(table, displayCode=DISPLAY_CODE, validateOnly=True)

    assert table.queried_index == DEFAULT_DISPLAY_CODE_INDEX

    condition = table.query_calls[0]["condition"]
    expr = condition.get_expression()

    assert expr["operator"] == "="
    attribute, expected = expr["values"]
    assert attribute.name == "displayCode"
    assert expected == DISPLAY_CODE


def test_confirm_uses_conditional_update_with_ttl():
    table = FakeTable([_pending_item()])

    _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        specialistId="teacher-1",
        confirmationRequestId=MINE_RID,
    )

    condition = table.update_calls[0]["condition"]
    expr = condition.get_expression()

    assert expr["operator"] == "AND"

    status_expr, ttl_expr = expr["values"]

    status_condition = status_expr.get_expression()
    assert status_condition["operator"] == "="
    status_attr, status_value = status_condition["values"]
    assert status_attr.name == "status"
    assert status_value == STATUS_PENDING

    ttl_condition = ttl_expr.get_expression()
    assert ttl_condition["operator"] == ">"
    ttl_attr, ttl_value = ttl_condition["values"]
    assert ttl_attr.name == "ttl"
    assert ttl_value == NOW_EPOCH


def test_confirm_update_sends_no_unused_values():
    """La condición `Attr()` genera sus propios marcadores.

    No deben enviarse valores no referenciados por el `UpdateExpression`
    (DynamoDB rechaza `ExpressionAttributeValues` con claves sin uso).
    """

    table = FakeTable([_pending_item()])

    _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        specialistId="teacher-1",
        confirmationRequestId=MINE_RID,
    )

    call = table.update_calls[0]
    values = call["values"]
    update = call["update"]

    assert ":pending" not in values
    assert ":now_epoch" not in values

    for placeholder in values:
        assert placeholder in update


def test_reload_uses_consistent_read():
    table = FakeTable([_pending_item()])

    confirm_session._reload(table, SESSION_ID)

    assert table.get_calls == [
        {"key": {"sessionId": SESSION_ID}, "consistent": True}
    ]


# ==========================================================
# VALIDACIÓN (validateOnly = true)
# ==========================================================


def test_validate_only_returns_session_without_mutating():
    table = FakeTable([_pending_item()])

    status, body, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True
    )

    assert status == 200
    assert body["sessionId"] == SESSION_ID
    assert body["status"] == STATUS_PENDING
    assert body["success"] is True
    assert table.items[SESSION_ID]["status"] == STATUS_PENDING
    assert "specialistId" not in table.items[SESSION_ID]


def test_query_uses_configured_index():
    table = FakeTable([_pending_item()])

    _call(table, displayCode=DISPLAY_CODE, validateOnly=True)

    assert table.queried_index == DEFAULT_DISPLAY_CODE_INDEX


# ==========================================================
# VIGENCIA (ttl)
# ==========================================================


def test_expired_code_returns_410():
    table = FakeTable([_pending_item(ttl=PAST_TTL)])

    status, _, _ = _call(table, displayCode=DISPLAY_CODE, validateOnly=True)

    assert status == 410


def test_missing_ttl_is_not_confirmable():
    item = _pending_item()
    del item["ttl"]
    table = FakeTable([item])

    status, _, _ = _call(table, displayCode=DISPLAY_CODE, validateOnly=True)

    assert status == 410


def test_invalid_ttl_is_not_confirmable():
    table = FakeTable([_pending_item(ttl="no-numérico")])

    status, _, _ = _call(table, displayCode=DISPLAY_CODE, validateOnly=True)

    assert status == 410


def test_numeric_string_and_decimal_ttl_are_supported():
    for ttl in (str(FUTURE_TTL), Decimal(FUTURE_TTL)):
        table = FakeTable([_pending_item(ttl=ttl)])

        status, _, _ = _call(
            table, displayCode=DISPLAY_CODE, validateOnly=True
        )

        assert status == 200


def test_expiry_between_query_and_confirm_is_rejected():
    """El TTL se revalida en la actualización condicional."""

    def expire(item):
        item["ttl"] = PAST_TTL

    table = FakeTable([_pending_item()], on_update=expire)

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=False
    )

    assert status == 409


# ==========================================================
# CONFIRMACIÓN (validateOnly = false)
# ==========================================================


def test_confirm_transitions_pending_to_confirmed():
    table = FakeTable([_pending_item()])

    status, body, raw = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        specialistId="teacher-1",
        patientId="paciente-9",
        confirmationRequestId=MINE_RID,
    )

    assert status == 200
    assert body["status"] == STATUS_CONFIRMED
    assert body["success"] is True

    stored = table.items[SESSION_ID]
    assert stored["status"] == STATUS_CONFIRMED
    assert stored["specialistId"] == "teacher-1"
    assert stored["patientId"] == "paciente-9"
    assert stored["confirmationRequestId"] == MINE_RID
    # Nunca se devuelve el token privado.
    assert "sessionToken" not in raw
    assert TOKEN not in raw


def test_validate_only_does_not_need_specialist():
    table = FakeTable([_pending_item()])

    status, body, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True
    )

    assert status == 200
    assert body["success"] is True


def test_confirm_without_specialist_uses_anonymous_default():
    """Compatibilidad con el Dashboard anterior (sin `specialistId`)."""

    table = FakeTable([_pending_item()])

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 200
    assert (
        table.items[SESSION_ID]["specialistId"] == DEFAULT_SPECIALIST_ID
    )


def test_confirm_is_idempotent_with_same_request_id():
    table = FakeTable(
        [
            _pending_item(
                status=STATUS_CONFIRMED,
                specialistId="teacher-1",
                confirmationRequestId=MINE_RID,
            )
        ]
    )

    status, body, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 200
    assert body["idempotent"] is True


def test_confirm_conflict_with_different_request_id():
    table = FakeTable(
        [
            _pending_item(
                status=STATUS_CONFIRMED,
                specialistId="teacher-1",
                confirmationRequestId=OTHER_RID,
            )
        ]
    )

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 409


def test_confirmed_session_validate_only_is_conflict():
    table = FakeTable(
        [
            _pending_item(
                status=STATUS_CONFIRMED,
                confirmationRequestId=OTHER_RID,
            )
        ]
    )

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True
    )

    assert status == 409


def test_simultaneous_requests_loser_gets_conflict():
    """Dos solicitudes simultáneas: la perdedora no se apropia."""

    def confirm_by_other(item):
        item["status"] = STATUS_CONFIRMED
        item["confirmationRequestId"] = OTHER_RID

    table = FakeTable([_pending_item()], on_update=confirm_by_other)

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 409


def test_simultaneous_retry_with_same_id_is_idempotent():
    def confirm_by_self(item):
        item["status"] = STATUS_CONFIRMED
        item["confirmationRequestId"] = MINE_RID

    table = FakeTable([_pending_item()], on_update=confirm_by_self)

    status, body, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 200
    assert body["idempotent"] is True


def test_multiple_pending_sessions_are_rejected():
    table = FakeTable(
        [_pending_item(), _pending_item(sessionId=OTHER_SESSION_ID)]
    )

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 409


# ==========================================================
# PAGINACIÓN Y CONSISTENCIA EVENTUAL
# ==========================================================


def test_query_walks_all_pages():
    table = FakeTable(
        [
            _pending_item(),
            _pending_item(sessionId=OTHER_SESSION_ID),
        ],
        page_size=1,
    )

    items = _query_by_display_code(table, DISPLAY_CODE)

    assert len(items) == 2
    assert len(table.query_calls) == 2
    assert table.query_calls[0]["exclusive_start"] is None
    assert table.query_calls[1]["exclusive_start"] == {"__start__": 1}


def test_gsi_eventual_consistency_retries_then_succeeds():
    table = FakeTable([_pending_item()], stale_queries=2)
    delays = []

    status, _, _ = _call(
        table,
        sleep=lambda delay: delays.append(delay),
        displayCode=DISPLAY_CODE,
        validateOnly=True,
    )

    assert status == 200
    assert len(delays) == 2


def test_gsi_eventual_consistency_gives_up_with_404():
    table = FakeTable([_pending_item()], stale_queries=99)
    delays = []

    status, _, _ = _call(
        table,
        sleep=lambda delay: delays.append(delay),
        displayCode=DISPLAY_CODE,
        validateOnly=True,
    )

    assert status == 404
    # Reintentos acotados: exactamente `attempts - 1` esperas.
    assert len(delays) == confirm_session.QUERY_RETRY_ATTEMPTS - 1


# ==========================================================
# FALLOS DE DYNAMODB
# ==========================================================


def test_query_failure_returns_500():
    table = FakeTable(query_error=_client_error("InternalServerError"))

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True
    )

    assert status == 500


def test_update_failure_returns_500():
    table = FakeTable(
        [_pending_item()],
        update_error=_client_error("ProvisionedThroughputExceededException"),
    )

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=False
    )

    assert status == 500


def test_get_failure_during_recovery_returns_500():
    def confirm_by_self(item):
        item["status"] = STATUS_CONFIRMED
        item["confirmationRequestId"] = MINE_RID

    table = FakeTable(
        [_pending_item()],
        on_update=confirm_by_self,
        get_error=_client_error("InternalServerError"),
    )

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 500


# ==========================================================
# PROYECCIÓN DEL ÍNDICE (DisplayCodeIndex)
# ==========================================================


def test_query_without_projected_status_is_not_confirmed():
    """Proyección `KEYS_ONLY`: sin `status` no se puede validar ni confirmar."""

    table = FakeTable(
        [{"sessionId": SESSION_ID, "displayCode": DISPLAY_CODE}]
    )

    status, body, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    assert status == 500
    assert body["success"] is False
    assert table.update_calls == []
    assert table.items[SESSION_ID].get("status") is None


@pytest.mark.parametrize("missing_attribute", ("idParticipante", "createdAt"))
def test_missing_view_attributes_are_not_confirmed(missing_attribute):
    """Sin `idParticipante`/`createdAt` la proyección es insuficiente."""

    assert missing_attribute in REQUIRED_INDEX_ATTRIBUTES

    item = _pending_item()
    del item[missing_attribute]
    table = FakeTable([item])

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId=MINE_RID,
    )

    # Nunca se confirma un registro cuya proyección es incompleta.
    assert status == 500
    assert table.items[SESSION_ID]["status"] == STATUS_PENDING
    assert table.update_calls == []


def test_incomplete_record_is_not_validated_either():
    item = _pending_item()
    del item["idParticipante"]
    table = FakeTable([item])

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True
    )

    assert status == 500


# ==========================================================
# VALIDACIÓN DE ENTRADAS
# ==========================================================


def test_unknown_code_returns_404():
    table = FakeTable([_pending_item()])

    status, body, _ = _call(
        table, displayCode="000000", validateOnly=True
    )

    assert status == 404
    assert body["success"] is False


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "12345a", ""])
def test_invalid_display_code_returns_400(code):
    table = FakeTable([_pending_item()])

    status, _, _ = _call(table, displayCode=code, validateOnly=True)

    assert status == 400


def test_invalid_body_returns_400():
    table = FakeTable([_pending_item()])

    status, _, _ = _parse(_handle({"body": "no-json"}, table=table, now=NOW))

    assert status == 400


@pytest.mark.parametrize("value", ["false", "true", 1, 0, None])
def test_validate_only_must_be_boolean(value):
    table = FakeTable([_pending_item()])

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=value
    )

    assert status == 400


def test_validate_only_absent_defaults_to_false():
    table = FakeTable([_pending_item()])

    status, body, _ = _call(table, displayCode=DISPLAY_CODE)

    assert status == 200
    assert body["status"] == STATUS_CONFIRMED


def test_invalid_specialist_type_returns_400():
    table = FakeTable([_pending_item()])

    status, _, _ = _call(
        table, displayCode=DISPLAY_CODE, validateOnly=True, specialistId=123
    )

    assert status == 400


def test_invalid_confirmation_request_id_returns_400():
    table = FakeTable([_pending_item()])

    status, _, _ = _call(
        table,
        displayCode=DISPLAY_CODE,
        validateOnly=False,
        confirmationRequestId="no-es-uuid",
    )

    assert status == 400


def test_base64_body_is_decoded():
    table = FakeTable([_pending_item()])
    raw = json.dumps({"displayCode": DISPLAY_CODE, "validateOnly": True})

    status, body, _ = _parse(
        _handle(
            {
                "body": base64.b64encode(raw.encode("utf-8")).decode("utf-8"),
                "isBase64Encoded": True,
            },
            table=table,
            now=NOW,
        )
    )

    assert status == 200
    assert body["sessionId"] == SESSION_ID


def test_direct_invocation_reads_top_level_fields():
    table = FakeTable([_pending_item()])

    status, body, _ = _parse(
        _handle(
            {"displayCode": DISPLAY_CODE, "validateOnly": True},
            table=table,
            now=NOW,
        )
    )

    assert status == 200
    assert body["sessionId"] == SESSION_ID


# ==========================================================
# CORS
# ==========================================================


def test_success_response_has_cors_headers():
    table = FakeTable([_pending_item()])

    response = _handle(
        _event(displayCode=DISPLAY_CODE, validateOnly=True),
        table=table,
        now=NOW,
        sleep=_no_sleep,
    )

    headers = response["headers"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in headers["Access-Control-Allow-Methods"]


def test_error_response_has_cors_headers():
    table = FakeTable([_pending_item()])

    response = _handle(
        _event(displayCode="12345", validateOnly=True),
        table=table,
        now=NOW,
        sleep=_no_sleep,
    )

    assert response["statusCode"] == 400
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


def test_options_preflight_is_accepted():
    table = FakeTable()

    response = _handle(
        {"httpMethod": "OPTIONS"},
        table=table,
        now=NOW,
        sleep=_no_sleep,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


# ==========================================================
# VARIABLES DE ENTORNO DE LA TABLA
# ==========================================================


class _FakeResource:
    def __init__(self):
        self.names = []

    def Table(self, name):
        self.names.append(name)
        return "table"


@pytest.fixture
def fake_boto3(monkeypatch):
    resource = _FakeResource()
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: resource)
    return resource


def test_table_name_prefers_legacy_env(monkeypatch, fake_boto3):
    monkeypatch.setenv("DYNAMODB_TABLE", "legacy")
    monkeypatch.setenv("SESSIONS_TABLE", "nuevo")

    assert _get_table() == "table"
    assert fake_boto3.names == ["legacy"]


def test_table_name_falls_back_to_sessions_env(monkeypatch, fake_boto3):
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.setenv("SESSIONS_TABLE", "nuevo")

    assert _get_table() == "table"
    assert fake_boto3.names == ["nuevo"]


def test_table_name_default(monkeypatch, fake_boto3):
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("SESSIONS_TABLE", raising=False)

    assert _get_table() == "table"
    assert fake_boto3.names == [DEFAULT_TABLE_NAME]
