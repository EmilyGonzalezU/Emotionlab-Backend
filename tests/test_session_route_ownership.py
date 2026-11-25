from flask import Flask

from routes.session_routes import sessions_bp


class FakeAuthService:
    def verify_token(self, _token):
        return {"sub": "teacher-1"}

    def get_current_user(self, _token):
        return {"uid": "teacher-1", "rol": "teacher"}


class FakeSessionService:
    def __init__(self):
        self.rows = [
            {
                "session_id": "OWN",
                "owner_id": "teacher-1",
                "status": "completed",
                "student_data": {"edad": "+17", "genero": "Masculino"},
                "record_count": 1,
                "capture_started_at": "2026-11-17T00:10:00Z",
                "model_processing": {
                    "session_id": "OWN",
                    "model_version": "1.0.0-experimental",
                    "algorithm": "KMeans",
                    "n_clusters": 2,
                    "status": "analyzed",
                    "grupo": 1,
                    "variables_originales": {"estado_pre": 5},
                    "tension_index": {"status": "pending_validation", "value": None},
                },
                "raw_json": {
                    "records": [{
                        "record": {"session_data": {
                            "respuestas": [{"respuestaLabel": "5"}],
                            "eventos": [{"nombre": "inicio"}],
                        }}
                    }]
                },
            },
            {"session_id": "OTHER", "owner_id": "teacher-2", "status": "completed"},
            {"session_id": "LEGACY", "status": "completed"},
        ]
        self.update_calls = []

    def list_sessions(self, **_kwargs):
        return self.rows

    def list_sessions_by_date(self, _date):
        return self.rows

    def search_by_name(self, _name):
        return self.rows

    def get_sessions_by_participant(self, _participant_id):
        return self.rows

    def get_session_detail(self, session_id):
        return next((row for row in self.rows if row["session_id"] == session_id), None)

    @staticmethod
    def _get_date(session):
        return session.get("fecha")

    @staticmethod
    def _get_time(session):
        return session.get("hora")

    def update_session(self, session_id, body):
        self.update_calls.append((session_id, body))

    def update_name(self, session_id, name):
        self.update_calls.append((session_id, name))


def make_client(service):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": FakeAuthService(),
        "session_service": service,
    }
    app.register_blueprint(sessions_bp)
    return app.test_client()


def test_session_list_exposes_only_owned_sessions():
    service = FakeSessionService()
    response = make_client(service).get("/sessions", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert [row["session_id"] for row in response.get_json()] == ["OWN"]


def test_session_detail_hides_other_and_legacy_records():
    client = make_client(FakeSessionService())
    headers = {"Authorization": "Bearer test"}
    assert client.get("/sessions/OWN", headers=headers).status_code == 200
    assert client.get("/sessions/OTHER", headers=headers).status_code == 404
    assert client.get("/sessions/LEGACY", headers=headers).status_code == 404


def test_session_detail_exposes_capture_data_and_utc_timestamp():
    response = make_client(FakeSessionService()).get(
        "/sessions/OWN", headers={"Authorization": "Bearer test"}
    )
    data = response.get_json()
    assert data["student_data"]["edad"] == "+17"
    assert data["record_count"] == 1
    assert data["capture_started_at"].endswith("Z")
    record = data["raw_data"]["records"][0]["record"]["session_data"]
    assert len(record["respuestas"]) == 1
    assert len(record["eventos"]) == 1


def test_session_detail_returns_v8_result_and_original_unity_payload_unchanged():
    service = FakeSessionService()
    raw_before = service.rows[0]["raw_json"]
    response = make_client(service).get(
        "/sessions/OWN", headers={"Authorization": "Bearer test"}
    )

    data = response.get_json()
    assert data["session_id"] == "OWN"
    assert data["model_processing"]["session_id"] == "OWN"
    assert data["model_processing"]["grupo"] == 1
    assert data["model_processing"]["tension_index"]["value"] is None
    assert data["raw_data"] == raw_before
    assert "classification_final" not in data["model_processing"]


def test_session_updates_require_matching_owner():
    service = FakeSessionService()
    client = make_client(service)
    headers = {"Authorization": "Bearer test"}
    assert client.patch("/sessions/OTHER", json={"nombre": "x"}, headers=headers).status_code == 404
    assert client.patch("/sessions/OWN/name", json={"nombre": "x"}, headers=headers).status_code == 200
    assert service.update_calls == [("OWN", "x")]
