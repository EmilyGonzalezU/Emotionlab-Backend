"""Pruebas de seguridad y consistencia de los endpoints de seguimiento.

Se ejercitan las rutas REALES (``routes/tracking_routes.py``) sobre una
``TrackingService`` real con un doble de DynamoDB en memoria. No se toca AWS.

Cubre: autorización (token y rol), validación de identificadores, mezcla de
participantes, sesiones sin análisis, duplicados, seguimientos
inexistentes, desvinculación, eliminación sin borrar sesiones, integridad de
los datos originales y el límite transaccional de DynamoDB.
"""

import pytest

from flask import Flask, jsonify

from core.constants import (
    DYNAMODB_TABLE_SESSIONS,
    DYNAMODB_TABLE_TRACKINGS,
)
from core.exceptions import AuthenticationError, AuthorizationError
from core.roles import ROLE_TEACHER
from routes.tracking_routes import trackings_bp
from services.tracking_service import TrackingService


HEADERS = {"Authorization": "Bearer token"}


# ==========================================================
# DOBLES
# ==========================================================


class FakeDynamoDB:
    def __init__(self):
        self.trackings = {}
        self.sessions = {}
        self.transactions = []
        self.delete_calls = 0

    def get_item(self, table_name, key):
        if table_name == DYNAMODB_TABLE_TRACKINGS:
            item = self.trackings.get(key["tracking_code"])
            return dict(item) if item else None

        if table_name == DYNAMODB_TABLE_SESSIONS:
            item = self.sessions.get(
                (key["participant_id"], key["sk"])
            )
            return dict(item) if item else None

        return None

    def query_all(self, table_name, key_condition_expression, **kwargs):
        valores = (
            key_condition_expression.get_expression() or {}
        ).get("values") or ()
        valor = valores[-1] if valores else None

        if table_name == DYNAMODB_TABLE_TRACKINGS:
            coincidencias = [
                dict(item)
                for item in self.trackings.values()
                if item.get("tracking_id") == valor
            ]
            max_items = kwargs.get("max_items")
            return coincidencias[:max_items] if max_items else coincidencias

        if table_name == DYNAMODB_TABLE_SESSIONS:
            return [
                dict(item)
                for item in self.sessions.values()
                if item.get("tracking_id") == valor
            ]

        return []

    def scan_all(self, table_name, **kwargs):
        if table_name == DYNAMODB_TABLE_TRACKINGS:
            return [dict(item) for item in self.trackings.values()]
        return []

    def transact_write_items(self, operations):
        self.transactions.append(operations)

        for operation in operations:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                self.trackings[item["tracking_code"]] = dict(item)

            elif "Update" in operation:
                update = operation["Update"]
                key = update["Key"]
                session = self.sessions[(key["participant_id"], key["sk"])]

                names = update.get("ExpressionAttributeNames", {})
                values = update.get("ExpressionAttributeValues", {})
                expression = update["UpdateExpression"]

                if "SET #tracking_id" in expression:
                    session["tracking_id"] = values[":tracking_id"]

                if "REMOVE #tracking_id" in expression:
                    session.pop(names["#tracking_id"], None)

                if "#updated_at = :updated_at" in expression:
                    session[names["#updated_at"]] = values[":updated_at"]

            elif "Delete" in operation:
                delete = operation["Delete"]
                self.trackings.pop(
                    delete["Key"].get("tracking_code"), None
                )

    def delete_item(self, *args, **kwargs):
        self.delete_calls += 1
        raise AssertionError("No se deben eliminar sesiones.")


class FakeSessionService:
    def __init__(self, keys=None):
        self.keys = dict(keys or {})

    def _resolve_session_key(self, session_id):
        return self.keys.get(session_id)

    def list_sessions(self, **_kwargs):
        return []


class FakeAudit:
    def __init__(self):
        self.events = []

    def log(self, action, **kwargs):
        self.events.append({"action": action, **kwargs})


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


def _processing(group=1, inicial=5.0, facil=6.0, dificil=7.0):
    return {
        "status": "analyzed",
        "prediction": {"group": group, "n_clusters": 3},
        "derived_indicators": {
            "etapas": {
                "inicial": inicial,
                "facil": facil,
                "dificil": dificil,
            }
        },
    }


# ==========================================================
# FIXTURES
# ==========================================================


@pytest.fixture
def backend():
    dynamo = FakeDynamoDB()
    audit = FakeAudit()

    def _session(
        session_id,
        participant="PARTICIPANT-001",
        sk="2026-11-15#10:00",
        fecha="2026-11-15",
        hora="10:00",
        processing=None,
    ):
        dynamo.sessions[(participant, sk)] = {
            "participant_id": participant,
            "owner_id": "teacher-1",
            "sk": sk,
            "session_id": session_id,
            "status": "completed",
            "fecha": fecha,
            "hora": hora,
            "model_processing": (
                processing if processing is not None else _processing()
            ),
            "raw_json": {"session_id": session_id},
        }

    _session("SESSION-001", sk="2026-11-15#10:00", fecha="2026-11-15")
    _session("SESSION-002", sk="2026-11-16#10:00", fecha="2026-11-16")
    _session("SESSION-003", sk="2026-11-17#10:00", fecha="2026-11-17")
    _session(
        "SESSION-NOV2",
        sk="2026-11-18#10:00",
        fecha="2026-11-18",
        processing={"status": "processing_error"},
    )
    _session(
        "SESSION-OTRO",
        participant="PARTICIPANT-999",
        sk="2026-11-19#10:00",
        fecha="2026-11-19",
    )

    keys = {
        "SESSION-001": {"participant_id": "PARTICIPANT-001", "sk": "2026-11-15#10:00"},
        "SESSION-002": {"participant_id": "PARTICIPANT-001", "sk": "2026-11-16#10:00"},
        "SESSION-003": {"participant_id": "PARTICIPANT-001", "sk": "2026-11-17#10:00"},
        "SESSION-NOV2": {"participant_id": "PARTICIPANT-001", "sk": "2026-11-18#10:00"},
        "SESSION-OTRO": {"participant_id": "PARTICIPANT-999", "sk": "2026-11-19#10:00"},
    }

    sessions = FakeSessionService(keys)
    service = TrackingService(
        dynamodb_service=dynamo,
        session_service=sessions,
        audit_svc=audit,
    )

    return service, dynamo, audit


def _client(service, auth=None):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": auth or FakeAuthService(),
        "tracking_service": service,
    }

    @app.errorhandler(AuthorizationError)
    def _forbidden(_error):
        return jsonify({"error": "No tiene permisos."}), 403

    @app.errorhandler(AuthenticationError)
    def _unauthorized(_error):
        return jsonify({"error": "Credenciales inválidas."}), 401

    app.register_blueprint(trackings_bp)
    return app.test_client()


# ==========================================================
# AUTORIZACIÓN
# ==========================================================


def test_list_trackings_requires_bearer_token(backend):
    service, _, _ = backend
    client = _client(service)

    assert client.get("/trackings").status_code == 401


def test_list_trackings_requires_teacher_role(backend):
    service, _, _ = backend
    client = _client(service, auth=FakeAuthService(rol="student"))

    assert client.get("/trackings", headers=HEADERS).status_code == 403


def test_create_tracking_requires_teacher_role(backend):
    service, _, _ = backend
    client = _client(service, auth=FakeAuthService(rol="student"))

    response = client.post(
        "/trackings",
        json={"tracking_code": "JP-1", "session_id": "SESSION-001"},
        headers=HEADERS,
    )

    assert response.status_code == 403


# ==========================================================
# VALIDACIÓN DE ENTRADA
# ==========================================================


def test_create_tracking_rejects_missing_fields(backend):
    service, _, _ = backend
    client = _client(service)

    assert client.post(
        "/trackings", json={}, headers=HEADERS
    ).status_code == 400
    assert client.post(
        "/trackings", json={"tracking_code": "JP-1"}, headers=HEADERS
    ).status_code == 400


def test_create_tracking_rejects_unknown_session(backend):
    service, _, _ = backend
    client = _client(service)

    response = client.post(
        "/trackings",
        json={"tracking_code": "JP-1", "session_id": "NO-EXISTE"},
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_create_tracking_succeeds_and_binds_first_session(backend):
    service, dynamo, _ = backend
    client = _client(service)

    response = client.post(
        "/trackings",
        json={"tracking_code": " jp-1 ", "session_id": "SESSION-001"},
        headers=HEADERS,
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["tracking_code"] == "JP-1"
    tracking_id = body["tracking_id"]

    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-15#10:00")
    ]["tracking_id"] == tracking_id


def test_tracking_code_preview_is_generated_by_backend_and_not_persisted(backend):
    service, dynamo, _ = backend
    client = _client(service)
    response = client.get("/trackings/code", headers=HEADERS)
    assert response.status_code == 200
    code = response.get_json()["tracking_code"]
    assert code.startswith("SEG-")
    assert code not in dynamo.trackings


def test_tracking_code_preview_returns_service_unavailable_when_table_is_missing(backend):
    service, _, _ = backend
    client = _client(service)

    def raise_missing_table(table_name, _key):
        if table_name == DYNAMODB_TABLE_TRACKINGS:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}},
                "GetItem",
            )
        return None

    service.dynamo.get_item = raise_missing_table
    response = client.get("/trackings/code", headers=HEADERS)

    assert response.status_code == 503
    assert "almacenamiento" in response.get_json()["error"]
    assert "ResourceNotFoundException" not in response.get_json()["error"]


def test_create_tracking_generates_code_when_not_supplied(backend):
    service, dynamo, _ = backend
    client = _client(service)
    response = client.post(
        "/trackings", json={"session_id": "SESSION-001"}, headers=HEADERS
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["tracking_code"].startswith("SEG-")
    assert body["tracking_code"] in dynamo.trackings


def test_get_unknown_tracking_returns_404(backend):
    service, _, _ = backend
    client = _client(service)

    assert client.get(
        "/trackings/NO-EXISTE", headers=HEADERS
    ).status_code == 404


def test_legacy_tracking_without_owner_is_denied_by_id(backend):
    service, dynamo, _ = backend
    dynamo.trackings["LEGACY-1"] = {
        "tracking_code": "LEGACY-1",
        "tracking_id": "legacy-id",
    }
    client = _client(service)
    response = client.get("/trackings/legacy-id", headers=HEADERS)
    assert response.status_code == 404


# ==========================================================
# INCORPORACIÓN: VALIDACIONES
# ==========================================================


def _create(service, code="JP-1"):
    return service.create_tracking(
        tracking_code=code,
        first_session_id="SESSION-001",
        actor_ctx={"actor_uid": "teacher-1"},
    )["tracking_id"]


def test_add_session_without_completed_analysis_returns_400(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-NOV2"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "análisis" in response.get_json()["error"]


def test_add_session_other_participant_is_allowed(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-OTRO"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.get_json()["session_id"] == "SESSION-OTRO"


def test_add_session_duplicate_returns_400(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    # SESSION-001 ya está vinculada por create_tracking.
    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-001"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "asociada" in response.get_json()["error"]


def test_add_session_rejects_unknown_field(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-002", "extra": 1},
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_add_session_missing_body_returns_400(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"otra_cosa": "x"},
        headers=HEADERS,
    )

    assert response.status_code == 400


# ==========================================================
# INCORPORACIÓN POR LOTES
# ==========================================================


def test_add_sessions_batch_succeeds(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002", "SESSION-003"]},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.get_json()["added"] == ["SESSION-002", "SESSION-003"]
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#10:00")
    ]["tracking_id"] == tracking_id


def test_associated_sessions_are_recovered_after_service_reload(backend):
    service, dynamo, _ = backend
    tracking_id = _create(service)
    client = _client(service)
    added = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002", "SESSION-003"]},
        headers=HEADERS,
    )
    assert added.status_code == 200

    # Una instancia nueva del servicio simula una recarga de la aplicación;
    # la relación debe recuperarse desde los registros persistidos, sin cache.
    service_reloaded = TrackingService(dynamo, service.sessions)
    reloaded_client = _client(service_reloaded)
    response = reloaded_client.get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    )
    follow_up = reloaded_client.get(
        f"/trackings/{tracking_id}/follow-up", headers=HEADERS
    )
    assert response.status_code == 200
    assert {session["session_id"] for session in response.get_json()} == {
        "SESSION-001", "SESSION-002", "SESSION-003",
    }
    assert {session["session_id"] for session in follow_up.get_json()} == {
        "SESSION-001", "SESSION-002", "SESSION-003",
    }


def test_tracking_sessions_endpoint_returns_empty_after_last_unlink(backend):
    service, _, _ = backend
    tracking_id = _create(service)
    client = _client(service)
    removed = client.delete(
        f"/trackings/{tracking_id}/sessions/SESSION-001", headers=HEADERS
    )
    response = client.get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    )
    assert removed.status_code == 200
    assert response.status_code == 200
    assert response.get_json() == []


def test_tracking_session_recovery_deduplicates_legacy_duplicate_rows(backend):
    service, dynamo, _ = backend
    tracking_id = _create(service)
    original = dynamo.sessions[("PARTICIPANT-001", "2026-11-15#10:00")]
    duplicate = dict(original, participant_id="PARTICIPANT-999", sk="legacy-duplicate")
    dynamo.sessions[("PARTICIPANT-999", "legacy-duplicate")] = duplicate
    response = _client(service).get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    )
    ids = [session["session_id"] for session in response.get_json()]
    assert response.status_code == 200
    assert ids.count("SESSION-001") == 1


def test_add_sessions_batch_is_atomic_on_failure(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002", "SESSION-NOV2"]},
        headers=HEADERS,
    )

    assert response.status_code == 400
    # Ninguna se incorporó.
    assert "tracking_id" not in dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#10:00")
    ]


def test_add_sessions_batch_requires_non_empty_list(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    assert client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": []},
        headers=HEADERS,
    ).status_code == 400

    assert client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": "SESSION-002"},
        headers=HEADERS,
    ).status_code == 400


def test_add_sessions_batch_rejects_unexpected_fields(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002"], "extra": 1},
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_add_sessions_batch_accepts_other_participant(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002", "SESSION-OTRO"]},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.get_json()["added"] == [
        "SESSION-002", "SESSION-OTRO",
    ]
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#10:00")
    ]["tracking_id"] == tracking_id


# ==========================================================
# EVOLUCIÓN
# ==========================================================


def test_evolution_returns_chart_contract(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)
    client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002", "SESSION-003"]},
        headers=HEADERS,
    )

    response = client.get(
        f"/trackings/{tracking_id}/evolution", headers=HEADERS
    )

    assert response.status_code == 200
    body = response.get_json()

    assert body["tracking_id"] == tracking_id
    assert body["total_sessions"] == 3
    assert [p["session_number"] for p in body["sessions"]] == [1, 2, 3]
    assert [p["session_id"] for p in body["sessions"]] == [
        "SESSION-001", "SESSION-002", "SESSION-003",
    ]
    for punto in body["sessions"]:
        assert set(punto) == {
            "session_number",
            "session_id",
            "session_date",
            "model_version",
            "difficulty",
            "wellbeing",
        }
        assert punto["wellbeing"] == 6.0


def test_tracking_api_crud_recovers_all_links_and_preserves_original_sessions(backend):
    service, dynamo, _ = backend
    client = _client(service)
    for session_id, day in (("SESSION-004", "2026-11-20"), ("SESSION-005", "2026-11-21")):
        key = {"participant_id": "PARTICIPANT-001", "sk": f"{day}#10:00"}
        dynamo.sessions[(key["participant_id"], key["sk"])] = {
            **key,
            "owner_id": "teacher-1",
            "session_id": session_id,
            "status": "completed",
            "fecha": day,
            "hora": "10:00",
            "model_processing": _processing(),
        }
        service.sessions.keys[session_id] = key

    created = client.post(
        "/trackings",
        json={"tracking_code": "FLOW-1", "session_id": "SESSION-001"},
        headers=HEADERS,
    )
    assert created.status_code == 201
    tracking_id = created.get_json()["tracking_id"]
    second = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002"]},
        headers=HEADERS,
    )
    third = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-003"]},
        headers=HEADERS,
    )
    fourth = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-004"]},
        headers=HEADERS,
    )
    assert [second.status_code, third.status_code, fourth.status_code] == [200, 200, 200]

    recovered_service = TrackingService(dynamo, service.sessions)
    recovered_client = _client(recovered_service)
    sessions = recovered_client.get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    )
    evolution = recovered_client.get(
        f"/trackings/{tracking_id}/evolution", headers=HEADERS
    )
    assert sessions.status_code == evolution.status_code == 200
    assert sorted(row["session_id"] for row in sessions.get_json()) == sorted([
        "SESSION-001", "SESSION-002", "SESSION-003", "SESSION-004",
    ])
    assert evolution.get_json()["total_sessions"] == 4

    other = recovered_client.post(
        "/trackings",
        json={"tracking_code": "FLOW-2", "session_id": "SESSION-005"},
        headers=HEADERS,
    )
    other_id = other.get_json()["tracking_id"]
    other_rows = recovered_client.get(
        f"/trackings/{other_id}/sessions", headers=HEADERS
    ).get_json()
    assert [row["session_id"] for row in other_rows] == ["SESSION-005"]

    removed = recovered_client.delete(
        f"/trackings/{tracking_id}/sessions/SESSION-002", headers=HEADERS
    )
    assert removed.status_code == 200
    remaining = recovered_client.get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    ).get_json()
    assert sorted(row["session_id"] for row in remaining) == sorted([
        "SESSION-001", "SESSION-003", "SESSION-004",
    ])
    original = next(item for item in dynamo.sessions.values() if item.get("session_id") == "SESSION-002")
    assert "session_id" in original
    assert original.get("tracking_id") is None

    for session_id in ("SESSION-001", "SESSION-003", "SESSION-004"):
        assert recovered_client.delete(
            f"/trackings/{tracking_id}/sessions/{session_id}", headers=HEADERS
        ).status_code == 200
    empty_evolution = recovered_client.get(
        f"/trackings/{tracking_id}/evolution", headers=HEADERS
    )
    empty_sessions = recovered_client.get(
        f"/trackings/{tracking_id}/sessions", headers=HEADERS
    )
    assert empty_evolution.status_code == empty_sessions.status_code == 200
    assert empty_evolution.get_json()["total_sessions"] == 0
    assert empty_evolution.get_json()["sessions"] == []
    assert empty_sessions.get_json() == []


def test_v8_tracking_evolution_preserves_only_real_model_fields(backend):
    service, dynamo, _ = backend
    tracking_id = _create(service)
    v8_result = {
        "model_version": "1.0.0-experimental",
        "algorithm": "KMeans",
        "n_clusters": 2,
        "status": "analyzed",
        "grupo": 1,
        "indice_relativo_centroides": 27.5,
        "variables_originales": {
            "estado_pre": 2,
            "nerviosismo_pre": 3,
            "preocupacion_pre": 4,
            "preparacion_pre": 8,
            "estado_facil": 5,
            "nerviosismo_facil": 6,
            "nerviosismo_dificil": None,
            "satisfaccion_facil": 7,
            "satisfaccion_dificil": 5,
            "alivio_facil": 8,
            "alivio_dificil": 4,
            "estado_dificil": 6,
        },
        "variables_procesadas": {
            "nerviosismo_pre": 3,
            "nerviosismo_facil": 6,
            "nerviosismo_dificil": 9,
        },
        "estimadas": ["nerviosismo_dificil"],
        "tension_index": {"value": None},
    }
    dynamo.sessions[("PARTICIPANT-001", "2026-11-16#10:00")]["model_processing"] = v8_result
    client = _client(service)
    associated = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-002"},
        headers=HEADERS,
    )
    evolution = client.get(f"/trackings/{tracking_id}/evolution", headers=HEADERS)
    assert associated.status_code == 200
    assert evolution.status_code == 200
    v8_point = next(point for point in evolution.get_json()["sessions"] if point["session_id"] == "SESSION-002")
    assert v8_point["wellbeing"] is None
    assert v8_point["kmeans_group"] == 1
    assert v8_point["centroid_relative_index"] == 27.5
    assert v8_point["tension_index"] == {"value": None}
    assert v8_point["observations"] == [
        {"dimension": "estado_emocional", "phase": "PRE", "variable": "estado_pre", "value": 2, "type": "observed"},
        {"dimension": "nerviosismo", "phase": "PRE", "variable": "nerviosismo_pre", "value": 3, "type": "observed"},
        {"dimension": "preocupacion", "phase": "PRE", "variable": "preocupacion_pre", "value": 4, "type": "observed"},
        {"dimension": "preparacion", "phase": "PRE", "variable": "preparacion_pre", "value": 8, "type": "observed"},
        {"dimension": "estado_emocional", "phase": "FACIL", "variable": "estado_facil", "value": 5, "type": "observed"},
        {"dimension": "nerviosismo", "phase": "FACIL", "variable": "nerviosismo_facil", "value": 6, "type": "observed"},
        {"dimension": "satisfaccion", "phase": "FACIL", "variable": "satisfaccion_facil", "value": 7, "type": "observed"},
        {"dimension": "alivio", "phase": "FACIL", "variable": "alivio_facil", "value": 8, "type": "observed"},
        {"dimension": "estado_emocional", "phase": "DIFICIL", "variable": "estado_dificil", "value": 6, "type": "observed"},
        {"dimension": "satisfaccion", "phase": "DIFICIL", "variable": "satisfaccion_dificil", "value": 5, "type": "observed"},
        {"dimension": "alivio", "phase": "DIFICIL", "variable": "alivio_dificil", "value": 4, "type": "observed"},
    ]
    assert evolution.get_json()["prediction"] == {
        "status": "unavailable",
        "reason": "stable_participant_identity_unavailable",
        "method": None,
        "observations_used": 0,
        "interval": None,
    }


def test_v8_observations_reject_estimates_invalid_values_and_other_models(backend):
    service, _, _ = backend
    processing = {
        "model_version": "1.0.0-experimental",
        "algorithm": "KMeans",
        "n_clusters": 2,
        "variables_originales": {
            "nerviosismo_pre": 3,
            "nerviosismo_facil": 5,
            "nerviosismo_dificil": 6,
        },
        "variables_procesadas": {"nerviosismo_dificil": 8},
        "estimadas": ["nerviosismo_pre"],
    }
    assert service._v8_original_emotional_observations(processing) == [
        {"dimension": "nerviosismo", "phase": "FACIL", "variable": "nerviosismo_facil", "value": 5, "type": "observed"},
        {"dimension": "nerviosismo", "phase": "DIFICIL", "variable": "nerviosismo_dificil", "value": 6, "type": "observed"},
    ]
    assert service._v8_original_emotional_observations({**processing, "variables_originales": {"nerviosismo_pre": 3, "nerviosismo_facil": 0, "nerviosismo_dificil": True}, "estimadas": []}) == [
        {"dimension": "nerviosismo", "phase": "PRE", "variable": "nerviosismo_pre", "value": 3, "type": "observed"},
    ]
    assert service._v8_original_emotional_observations({**processing, "model_version": "2.0"}) == []


def test_evolution_deduplicates_reprocessed_session_using_latest_record(backend):
    service, dynamo, _ = backend
    tracking_id = _create(service)
    client = _client(service)
    assert client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-002"},
        headers=HEADERS,
    ).status_code == 200

    original_key, original = next(
        (key, item)
        for key, item in dynamo.sessions.items()
        if item.get("session_id") == "SESSION-002"
    )
    latest = dict(original)
    latest["updated_at"] = "2099-01-01T00:00:00Z"
    latest["model_processing"] = {
        "model_version": "1.0.0-experimental",
        "algorithm": "KMeans",
        "n_clusters": 2,
        "variables_originales": {"nerviosismo_pre": 8},
        "estimadas": [],
    }
    dynamo.sessions[(original_key[0], "duplicate-record")] = latest

    response = client.get(f"/trackings/{tracking_id}/evolution", headers=HEADERS)

    assert response.status_code == 200
    body = response.get_json()
    assert body["total_sessions"] == 2
    point = next(item for item in body["sessions"] if item["session_id"] == "SESSION-002")
    assert point["observations"] == [
        {"dimension": "nerviosismo", "phase": "PRE", "variable": "nerviosismo_pre", "value": 8, "type": "observed"},
    ]


def test_evolution_unknown_tracking_returns_404(backend):
    service, _, _ = backend
    client = _client(service)

    assert client.get(
        "/trackings/NO-EXISTE/evolution", headers=HEADERS
    ).status_code == 404


@pytest.mark.parametrize("suffix", ["sessions", "evolution"])
def test_missing_tracking_gsi_returns_actionable_503(backend, suffix):
    service, _, _ = backend
    tracking_id = _create(service)
    client = _client(service)
    query_all = service.dynamo.query_all

    def query_with_missing_index(table_name, key_condition_expression, **kwargs):
        if kwargs.get("index_name") == "gsi-sessions-tracking":
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "The table does not have the specified index: gsi-sessions-tracking",
                    }
                },
                "Query",
            )
        return query_all(table_name, key_condition_expression, **kwargs)

    service.dynamo.query_all = query_with_missing_index
    response = client.get(
        f"/trackings/{tracking_id}/{suffix}", headers=HEADERS
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "TRACKING_INDEX_UNAVAILABLE"


# ==========================================================
# DESVINCULACIÓN Y ELIMINACIÓN
# ==========================================================


def test_remove_session_unlinks_without_deleting_data(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.delete(
        f"/trackings/{tracking_id}/sessions/SESSION-001",
        headers=HEADERS,
    )

    assert response.status_code == 200
    session = dynamo.sessions[("PARTICIPANT-001", "2026-11-15#10:00")]
    assert "tracking_id" not in session
    assert session["raw_json"] == {"session_id": "SESSION-001"}
    assert dynamo.delete_calls == 0


def test_remove_session_wrong_tracking_returns_400(backend):
    service, _, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    response = client.delete(
        f"/trackings/{tracking_id}/sessions/SESSION-002",
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_delete_tracking_never_deletes_sessions(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)
    client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ["SESSION-002"]},
        headers=HEADERS,
    )

    response = client.delete(
        f"/trackings/{tracking_id}", headers=HEADERS
    )

    assert response.status_code == 200
    assert dynamo.trackings == {}

    # Las sesiones siguen existiendo, solo desvinculadas.
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-15#10:00")
    ]["raw_json"] == {"session_id": "SESSION-001"}
    assert "tracking_id" not in dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-15#10:00")
    ]
    assert "tracking_id" not in dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#10:00")
    ]
    assert dynamo.delete_calls == 0


def test_delete_unknown_tracking_returns_404(backend):
    service, _, _ = backend
    client = _client(service)

    assert client.delete(
        "/trackings/NO-EXISTE", headers=HEADERS
    ).status_code == 404


# ==========================================================
# LÍMITES TRANSACCIONALES
# ==========================================================


def test_batch_over_dynamodb_limit_returns_400(backend):
    service, dynamo, _ = backend
    client = _client(service)
    tracking_id = _create(service)

    ids = []
    for index in range(101):
        session_id = f"BULK-{index}"
        key = {
            "participant_id": "PARTICIPANT-001",
            "sk": f"2026-12-01#{index:05d}",
        }
        dynamo.sessions[(key["participant_id"], key["sk"])] = {
            "participant_id": "PARTICIPANT-001",
            "owner_id": "teacher-1",
            "sk": key["sk"],
            "session_id": session_id,
            "status": "completed",
            "model_processing": _processing(),
        }
        service.sessions.keys[session_id] = key
        ids.append(session_id)

    response = client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_ids": ids},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "100" in response.get_json()["error"]


def test_tracking_owner_is_required_for_read_update_and_delete(backend):
    service, dynamo, _ = backend
    dynamo.sessions[("PARTICIPANT-001", "2026-11-15#10:00")]["owner_id"] = "teacher-owner"
    tracking_id = service.create_tracking(
        tracking_code="OWNER-1",
        first_session_id="SESSION-001",
        actor_ctx={"actor_uid": "teacher-owner"},
    )["tracking_id"]
    client = _client(
        service,
        auth=FakeAuthService(uid="teacher-other"),
    )

    assert client.get(
        f"/trackings/{tracking_id}",
        headers=HEADERS,
    ).status_code == 404
    assert client.post(
        f"/trackings/{tracking_id}/sessions",
        json={"session_id": "SESSION-002"},
        headers=HEADERS,
    ).status_code == 400
    assert client.delete(
        f"/trackings/{tracking_id}",
        headers=HEADERS,
    ).status_code == 404

