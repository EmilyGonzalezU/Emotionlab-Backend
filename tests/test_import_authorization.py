from flask import Flask, jsonify

from core.exceptions import AuthenticationError, AuthorizationError
from routes.import_routes import imports_bp
from routes.session_routes import sessions_bp


class FakeAuthService:
    def verify_token(self, token):
        if token not in {"admin", "teacher", "unknown"}:
            raise AuthenticationError("invalid token")
        return {"sub": token}

    def get_current_user(self, token):
        role = {
            "admin": "admin",
            "teacher": "teacher",
            "unknown": "observer",
        }[token]
        return {"uid": token, "email": f"{token}@example.invalid", "rol": role}


class FakeImportService:
    def __init__(self):
        self.global_calls = 0
        self.batch_calls = 0

    def import_from_aws(self, aws_service):
        self.global_calls += 1
        assert aws_service is not None
        return {
            "pending_identification": [{"session_id": "GLOBAL-1"}],
            "aggregated": [],
            "import_errors": [],
            "skipped_existing": [],
            "total": 1,
        }

    def import_batch(self, payload):
        self.batch_calls += 1
        return {
            "total": len(payload.get("sesiones", [])),
            "pending_identification": [{"session_id": "BATCH-1"}],
        }


def make_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": FakeAuthService(),
        "import_service": FakeImportService(),
        "aws_service": object(),
    }
    app.register_blueprint(sessions_bp)
    app.register_blueprint(imports_bp, url_prefix="/imports", name="imports_plural")
    app.register_blueprint(imports_bp, url_prefix="/import", name="imports_legacy")

    @app.errorhandler(AuthenticationError)
    def authentication_error(_error):
        return jsonify({"error": "unauthenticated"}), 401

    @app.errorhandler(AuthorizationError)
    def authorization_error(_error):
        return jsonify({"error": "forbidden"}), 403

    return app.test_client()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_can_trigger_legacy_global_import():
    client = make_client()
    response = client.post("/sessions/import", headers=auth("admin"))

    assert response.status_code == 200
    assert response.get_json()["total"] == 1
    assert "GLOBAL-1" not in response.get_data(as_text=True)
    assert set(response.get_json()) == {
        "importadas", "agregadas", "errores", "omitidas_existentes", "total"
    }
    assert client.application.extensions["services"]["import_service"].global_calls == 1


def test_specialist_cannot_trigger_global_import_or_enumerate_results():
    client = make_client()
    response = client.post(
        "/sessions/import",
        headers=auth("teacher"),
        json={"owner_id": "teacher", "specialist_id": "teacher", "session_id": "GLOBAL-1"},
    )

    assert response.status_code == 403
    assert "GLOBAL-1" not in response.get_data(as_text=True)
    service = client.application.extensions["services"]["import_service"]
    assert service.global_calls == 0
    assert service.batch_calls == 0


def test_unauthenticated_and_unknown_role_cannot_import():
    client = make_client()

    assert client.post("/sessions/import").status_code == 401
    assert client.post("/sessions/import", headers=auth("unknown")).status_code == 403
    assert client.application.extensions["services"]["import_service"].global_calls == 0


def test_all_existing_import_routes_are_admin_only():
    client = make_client()
    teacher = auth("teacher")
    admin = auth("admin")

    # The JSON batch endpoint and both aliases for the AWS sync endpoint
    # remain available to administrators and reject specialists.
    assert client.post("/imports/sessions", headers=teacher, json={"sesiones": []}).status_code == 403
    assert client.post("/import/sessions", headers=teacher, json={"sesiones": []}).status_code == 403
    assert client.post("/imports/sync", headers=teacher).status_code == 403
    assert client.post("/import/sync", headers=teacher).status_code == 403

    batch_response = client.post("/imports/sessions", headers=admin, json={"sesiones": []})
    assert batch_response.status_code == 201
    assert "BATCH-1" not in batch_response.get_data(as_text=True)
    assert batch_response.get_json()["importadas"] == 1
    assert client.post("/imports/sync", headers=admin).status_code == 201
    assert client.application.extensions["services"]["import_service"].batch_calls == 1
    assert client.application.extensions["services"]["import_service"].global_calls == 1


def test_capture_and_other_session_flows_are_not_import_routes():
    # Regression guard: the public routing table exposes one session import
    # path and the explicit admin sync/batch paths only.
    client = make_client()
    paths = {
        rule.rule for rule in client.application.url_map.iter_rules()
        if "import" in rule.rule or "sync" in rule.rule
    }
    assert "/sessions/import" in paths
    assert "/imports/sync" in paths
    assert "/import/sync" in paths
    assert paths == {
        "/sessions/import",
        "/imports/sessions",
        "/imports/sync",
        "/import/sessions",
        "/import/sync",
    }
