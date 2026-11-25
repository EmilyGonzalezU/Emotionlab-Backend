import pytest
from types import SimpleNamespace

from core.constants import (
    DYNAMODB_TABLE_SESSIONS,
    DYNAMODB_TABLE_TRACKINGS,
)
from services.tracking_service import TrackingService


def test_generate_tracking_code_retries_when_candidate_already_exists(
    tracking_setup, monkeypatch
):
    service, _, _, _ = tracking_setup
    candidates = iter(["A" * 12, "B" * 12])
    checked = []

    monkeypatch.setattr(
        "services.tracking_service.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(candidates)),
    )

    def existing_code(code):
        checked.append(code)
        return {"tracking_code": code} if code.endswith("A" * 12) else None

    monkeypatch.setattr(service, "_get_tracking_by_code", existing_code)

    assert service.generate_tracking_code() == f"SEG-{'B' * 12}"
    assert checked == [f"SEG-{'A' * 12}", f"SEG-{'B' * 12}"]


def test_tracking_summary_preserves_unity_wall_time_and_utc_capture_instant(tracking_setup):
    service, _, _, _ = tracking_setup
    session = {
        "session_id": "synthetic-session",
        "capture_started_at": "2026-11-17T00:10:00Z",
        "fecha": "2026-11-17",
        "hora": "00:10",
        "model_processing": {"status": "analyzed"},
    }

    summary = service._session_summary(session)
    assert summary["fecha"] == "2026-11-17"
    assert summary["hora"] == "00:10"
    assert summary["capture_started_at"].endswith("Z")
    assert service._session_date(session) == "2026-11-17 00:10"


def test_v8_tracking_summary_keeps_group_centroid_and_pending_tension(tracking_setup):
    service, _, _, _ = tracking_setup
    processing = {
        "model_version": "1.0.0-experimental",
        "algorithm": "KMeans",
        "n_clusters": 2,
        "status": "analyzed",
        "grupo": 1,
        "indice_relativo_centroides": None,
        "tension_index": {"status": "pending_validation", "value": None},
    }

    summary = service._session_summary({
        "session_id": "session-v8",
        "model_processing": processing,
    })

    assert summary["kmeans_group"] == 1
    assert summary["centroid_relative_index"] is None
    assert summary["tension_index"] == {"status": "pending_validation", "value": None}
    assert summary["classification_final"] is None
    assert summary["bienestar_promedio"] is None


class FakeDynamoDB:
    """Persistencia en memoria para pruebas unitarias."""

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

    def query_all(
        self,
        table_name,
        key_condition_expression,
        **kwargs,
    ):
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
            return (
                coincidencias[:max_items]
                if max_items
                else coincidencias
            )

        if table_name == DYNAMODB_TABLE_SESSIONS:
            return [
                dict(item)
                for item in self.sessions.values()
                if item.get("tracking_id") == valor
            ]

        return []

    def scan_all(self, table_name, **kwargs):
        if table_name == DYNAMODB_TABLE_TRACKINGS:
            return [
                dict(item)
                for item in self.trackings.values()
            ]

        return []

    def transact_write_items(self, operations):
        self.transactions.append(operations)

        # Simulación mínima de las operaciones utilizadas
        # por TrackingService. No llama a DynamoDB.

        for operation in operations:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                self.trackings[item["tracking_code"]] = dict(item)

            elif "Update" in operation:
                update = operation["Update"]
                key = update["Key"]

                session = self.sessions[
                    (key["participant_id"], key["sk"])
                ]

                names = update.get(
                    "ExpressionAttributeNames", {}
                )
                values = update.get(
                    "ExpressionAttributeValues", {}
                )
                expression = update["UpdateExpression"]

                if "SET #tracking_id" in expression:
                    session["tracking_id"] = values[
                        ":tracking_id"
                    ]

                if "REMOVE #tracking_id" in expression:
                    session.pop(
                        names["#tracking_id"],
                        None,
                    )

                if "#updated_at = :updated_at" in expression:
                    session[names["#updated_at"]] = values[
                        ":updated_at"
                    ]

            elif "Delete" in operation:
                delete = operation["Delete"]
                self.trackings.pop(
                    delete["Key"].get("tracking_code"),
                    None,
                )

    def delete_item(self, *args, **kwargs):
        self.delete_calls += 1
        raise AssertionError(
            "La desvinculación no debe eliminar sesiones."
        )


class FakeSessionService:
    def __init__(self, keys):
        self.keys = keys

    def _resolve_session_key(self, session_id):
        return self.keys.get(session_id)

    def list_sessions(self, **_kwargs):
        return []


class FakeAudit:
    def __init__(self):
        self.events = []

    def log(self, action, **kwargs):
        self.events.append({
            "action": action,
            **kwargs,
        })


@pytest.fixture
def tracking_setup():
    dynamo = FakeDynamoDB()
    audit = FakeAudit()

    session_key = {
        "participant_id": "PARTICIPANT-001",
        "sk": "2026-11-15#10:00",
    }

    dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-15#10:00")
    ] = {
        "participant_id": "PARTICIPANT-001",
        "owner_id": "teacher-001",
        "sk": "2026-11-15#10:00",
        "session_id": "SESSION-001",
        "status": "completed",
        "model_processing": {"status": "analyzed"},
        "raw_json": {"test": "data"},
    }

    sessions = FakeSessionService({
        "SESSION-001": session_key,
    })

    service = TrackingService(
        dynamodb_service=dynamo,
        session_service=sessions,
        audit_svc=audit,
    )

    return service, dynamo, audit, session_key


def test_create_tracking_associates_first_session(tracking_setup):
    service, dynamo, audit, session_key = tracking_setup

    result = service.create_tracking(
        tracking_code="JP-001",
        first_session_id="SESSION-001",
        actor_ctx={
            "actor_uid": "teacher-001",
            "actor_email": "teacher@example.com",
            "actor_rol": "teacher",
        },
    )

    session = dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]

    assert result["tracking_code"] == "JP-001"
    assert result["tracking_id"]
    assert session["tracking_id"] == result["tracking_id"]

    # El contenido original de la sesión se conserva.
    assert session["raw_json"] == {"test": "data"}

    # Seguimiento + sesión se escriben en una transacción.
    assert len(dynamo.transactions) == 1
    assert len(dynamo.transactions[0]) == 2

    # Se registró el evento de auditoría.
    assert audit.events
    assert audit.events[0]["action"] == "TRACKING_CREATED"


def test_create_tracking_rejects_duplicate_code(tracking_setup):
    service, dynamo, _, _ = tracking_setup

    dynamo.trackings["JP-001"] = {
        "tracking_code": "JP-001",
        "tracking_id": "existing-id",
    }

    with pytest.raises(
        ValueError,
        match="Ya existe un seguimiento",
    ):
        service.create_tracking(
            tracking_code="JP-001",
            first_session_id="SESSION-001",
        )

    assert dynamo.transactions == []


def test_add_session_rejects_session_already_linked(tracking_setup):
    service, dynamo, _, session_key = tracking_setup

    dynamo.trackings["JP-001"] = {
        "tracking_code": "JP-001",
        "tracking_id": "tracking-001",
        "created_by": "teacher-001",
        "created_by": "teacher-001",
    }

    session = dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]
    session["tracking_id"] = "another-tracking"

    with pytest.raises(
        ValueError,
        match="ya está asociada",
    ):
        service.add_session(
            tracking_id="tracking-001",
            session_id="SESSION-001",
        )

    assert dynamo.transactions == []


def test_remove_session_unlinks_without_deleting_data(tracking_setup):
    service, dynamo, audit, session_key = tracking_setup

    dynamo.trackings["JP-001"] = {
        "tracking_code": "JP-001",
        "tracking_id": "tracking-001",
        "created_by": "teacher-001",
        "created_by": "teacher-001",
    }

    session = dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]
    session["tracking_id"] = "tracking-001"

    original_raw_json = dict(session["raw_json"])

    result = service.remove_session(
        tracking_id="tracking-001",
        session_id="SESSION-001",
        actor_ctx={
            "actor_uid": "teacher-001",
            "actor_rol": "teacher",
        },
    )

    assert result["session_id"] == "SESSION-001"
    assert "tracking_id" not in session

    # La sesión y sus datos siguen existiendo.
    assert session["session_id"] == "SESSION-001"
    assert session["raw_json"] == original_raw_json

    assert dynamo.delete_calls == 0
    assert len(dynamo.transactions) == 1

    assert audit.events
    assert audit.events[0]["action"] == "TRACKING_SESSION_REMOVED"


def test_tracking_code_is_normalized_and_validated(tracking_setup):
    service, _, _, _ = tracking_setup

    assert service._normalize_tracking_code(" jp-001 ") == "JP-001"

    with pytest.raises(ValueError):
        service._normalize_tracking_code("Juan Pérez")

    with pytest.raises(ValueError):
        service._normalize_tracking_code("")


def test_tracking_groups_multiple_sessions_by_original_session_id(tracking_setup):
    """Un seguimiento agrupa experiencias independientes por session_id."""

    service, dynamo, _, session_key = tracking_setup

    # Segunda sesión del MISMO participante técnico.
    second_key = {
        "participant_id": "PARTICIPANT-001",
        "sk": "2026-11-16#09:00",
    }
    dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#09:00")
    ] = {
        "participant_id": "PARTICIPANT-001",
        "owner_id": "teacher-001",
        "sk": "2026-11-16#09:00",
        "session_id": "SESSION-002",
        "status": "completed",
        "model_processing": {"status": "analyzed"},
        "raw_json": {"test": "data-2"},
    }
    service.sessions.keys["SESSION-002"] = second_key

    created = service.create_tracking(
        tracking_code="JP-002",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    service.add_session(
        tracking_id=tracking_id,
        session_id="SESSION-002",
    )

    first = dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]
    second = dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#09:00")
    ]

    # Ambas sesiones se agrupan bajo el mismo seguimiento, conservando
    # cada una su identificador original.
    assert first["tracking_id"] == tracking_id
    assert second["tracking_id"] == tracking_id
    assert first["session_id"] == "SESSION-001"
    assert second["session_id"] == "SESSION-002"


def test_add_session_accepts_other_participant(tracking_setup):
    """participant_id no restringe las experiencias incorporadas."""

    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-004",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    dynamo.sessions[
        ("PARTICIPANT-999", "2026-11-17#09:00")
    ] = {
        "participant_id": "PARTICIPANT-999",
        "owner_id": "teacher-001",
        "sk": "2026-11-17#09:00",
        "session_id": "SESSION-999",
        "status": "completed",
        "model_processing": {"status": "analyzed"},
    }
    service.sessions.keys["SESSION-999"] = {
        "participant_id": "PARTICIPANT-999",
        "sk": "2026-11-17#09:00",
    }

    service.add_session(
        tracking_id=tracking_id,
        session_id="SESSION-999",
    )

    ajeno = dynamo.sessions[
        ("PARTICIPANT-999", "2026-11-17#09:00")
    ]
    assert ajeno["tracking_id"] == tracking_id


# ==========================================================
# VALIDACIÓN: SOLO SESIONES CON ANÁLISIS COMPLETADO
# ==========================================================


def test_create_tracking_rejects_session_without_completed_analysis(
    tracking_setup,
):
    service, dynamo, _, session_key = tracking_setup

    dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]["model_processing"] = {"status": "processing_error"}

    with pytest.raises(ValueError, match="finalizado correctamente"):
        service.create_tracking(
            tracking_code="JP-003",
            first_session_id="SESSION-001",
        )

    assert dynamo.transactions == []


def test_add_session_rejects_session_without_completed_analysis(
    tracking_setup,
):
    service, dynamo, _, session_key = tracking_setup

    dynamo.trackings["JP-001"] = {
        "tracking_code": "JP-001",
        "tracking_id": "tracking-001",
    }
    dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ].pop("model_processing", None)

    with pytest.raises(ValueError, match="finalizado correctamente"):
        service.add_session(
            tracking_id="tracking-001",
            session_id="SESSION-001",
        )

    assert dynamo.transactions == []


# ==========================================================
# ELIMINAR SEGUIMIENTO SIN ELIMINAR SESIONES
# ==========================================================


def test_delete_tracking_unlinks_sessions_without_deleting_them(
    tracking_setup,
):
    service, dynamo, audit, session_key = tracking_setup

    dynamo.trackings["JP-001"] = {
        "tracking_code": "JP-001",
        "tracking_id": "tracking-001",
        "created_by": "teacher-001",
    }

    session = dynamo.sessions[
        (session_key["participant_id"], session_key["sk"])
    ]
    session["tracking_id"] = "tracking-001"
    original_raw_json = dict(session["raw_json"])

    result = service.delete_tracking(
        tracking_id="tracking-001",
        actor_ctx={"actor_uid": "teacher-001", "actor_rol": "teacher"},
    )

    assert result["tracking_id"] == "tracking-001"

    # El seguimiento desaparece; la sesión permanece pero desvinculada.
    assert "JP-001" not in dynamo.trackings
    assert session["session_id"] == "SESSION-001"
    assert session.get("tracking_id") is None
    assert session["raw_json"] == original_raw_json

    # Nunca se elimina la sesión original.
    assert dynamo.delete_calls == 0
    assert audit.events[-1]["action"] == "TRACKING_DELETED"


# ==========================================================
# INCORPORACIÓN POR LOTES
# ==========================================================


def _register_session(
    service,
    dynamo,
    session_id,
    sk,
    participant="PARTICIPANT-001",
    model_processing=None,
):
    dynamo.sessions[(participant, sk)] = {
        "participant_id": participant,
        "owner_id": "teacher-001",
        "sk": sk,
        "session_id": session_id,
        "status": "completed",
        "model_processing": (
            model_processing
            if model_processing is not None
            else {"status": "analyzed"}
        ),
        "raw_json": {"session_id": session_id},
    }
    service.sessions.keys[session_id] = {
        "participant_id": participant,
        "sk": sk,
    }


def test_add_sessions_incorporates_batch_atomically(tracking_setup):
    service, dynamo, audit, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-BATCH",
        first_session_id="SESSION-001",
        actor_ctx={"actor_uid": "teacher-001"},
    )
    tracking_id = created["tracking_id"]

    _register_session(
        service, dynamo, "SESSION-002", "2026-11-16#09:00"
    )
    _register_session(
        service, dynamo, "SESSION-003", "2026-11-17#09:00"
    )

    result = service.add_sessions(
        tracking_id=tracking_id,
        session_ids=["SESSION-002", "SESSION-003"],
        actor_ctx={"actor_uid": "teacher-001"},
    )

    assert result["added"] == ["SESSION-002", "SESSION-003"]
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#09:00")
    ]["tracking_id"] == tracking_id
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-17#09:00")
    ]["tracking_id"] == tracking_id

    # Una sola transacción con las dos actualizaciones (atomicidad).
    assert len(dynamo.transactions) == 2  # create + lote
    assert len(dynamo.transactions[-1]) == 2

    acciones = [event["action"] for event in audit.events]
    assert acciones.count("TRACKING_SESSION_ADDED") == 2


def test_add_sessions_rejects_duplicates(tracking_setup):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-DUP",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    _register_session(
        service, dynamo, "SESSION-002", "2026-11-16#09:00"
    )

    with pytest.raises(ValueError, match="duplicados"):
        service.add_sessions(
            tracking_id=tracking_id,
            session_ids=["SESSION-002", "SESSION-002"],
        )


def test_add_sessions_is_atomic_when_one_session_is_invalid(
    tracking_setup,
):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-ATOMIC",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    _register_session(
        service, dynamo, "SESSION-002", "2026-11-16#09:00"
    )
    _register_session(
        service,
        dynamo,
        "SESSION-003",
        "2026-11-17#09:00",
        model_processing={"status": "processing_error"},
    )

    with pytest.raises(ValueError, match="finalizado correctamente"):
        service.add_sessions(
            tracking_id=tracking_id,
            session_ids=["SESSION-002", "SESSION-003"],
        )

    # Ninguna se incorporó: la válida permanece sin seguimiento.
    assert "tracking_id" not in dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#09:00")
    ]
    # La única transacción es la de create_tracking.
    assert len(dynamo.transactions) == 1


def test_add_sessions_accepts_other_participant_atomically(
    tracking_setup,
):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-MIX",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    _register_session(
        service, dynamo, "SESSION-002", "2026-11-16#09:00"
    )
    _register_session(
        service,
        dynamo,
        "SESSION-900",
        "2026-11-18#09:00",
        participant="PARTICIPANT-999",
    )

    service.add_sessions(
        tracking_id=tracking_id,
        session_ids=["SESSION-002", "SESSION-900"],
    )

    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-16#09:00")
    ]["tracking_id"] == tracking_id
    assert dynamo.sessions[
        ("PARTICIPANT-999", "2026-11-18#09:00")
    ]["tracking_id"] == tracking_id


def test_add_sessions_rejects_sessions_already_linked(tracking_setup):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-IDEMP",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    # SESSION-001 ya quedó vinculada al crear el seguimiento.
    with pytest.raises(ValueError, match="ya está asociada"):
        service.add_sessions(
            tracking_id=tracking_id,
            session_ids=["SESSION-001"],
        )

    assert len(dynamo.transactions) == 1  # no se escribió nada nuevo


def test_add_sessions_rejects_session_linked_to_another_tracking(
    tracking_setup,
):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-OTHER",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    _register_session(
        service, dynamo, "SESSION-777", "2026-11-19#09:00"
    )
    dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-19#09:00")
    ]["tracking_id"] = "otro-seguimiento"

    with pytest.raises(ValueError, match="otro seguimiento"):
        service.add_sessions(
            tracking_id=tracking_id,
            session_ids=["SESSION-777"],
        )

    # No se reescribió el vínculo previo.
    assert dynamo.sessions[
        ("PARTICIPANT-001", "2026-11-19#09:00")
    ]["tracking_id"] == "otro-seguimiento"


def test_add_sessions_rejects_empty_list(tracking_setup):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-EMPTY",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    with pytest.raises(ValueError, match="lista de session_id"):
        service.add_sessions(tracking_id=tracking_id, session_ids=[])


def test_add_sessions_enforces_dynamodb_transaction_limit(
    tracking_setup,
):
    service, dynamo, _, _ = tracking_setup

    created = service.create_tracking(
        tracking_code="JP-LIMIT",
        first_session_id="SESSION-001",
    )
    tracking_id = created["tracking_id"]

    for index in range(101):
        _register_session(
            service,
            dynamo,
            f"BULK-{index}",
            f"2026-12-01#{index:05d}",
        )

    with pytest.raises(ValueError, match="100"):
        service.add_sessions(
            tracking_id=tracking_id,
            session_ids=[f"BULK-{index}" for index in range(101)],
        )
