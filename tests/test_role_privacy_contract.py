from flask import Flask, jsonify

from core.exceptions import AuthenticationError, AuthorizationError
from routes.audit_routes import audit_bp
from routes.capture_routes import captures_bp
from routes.dashboard_routes import dashboard_bp
from routes.participant_routes import participants_bp
from routes.report_requests import report_requests_bp
from routes.report_routes import reports_bp
from routes.session_routes import sessions_bp
from routes.tracking_routes import trackings_bp
from routes.user_routes import users_bp


class FakeAuth:
    def verify_token(self, token):
        return {"sub": token, "email": f"{token}@example.invalid", "rol": token}

    def get_current_user(self, token):
        return {"uid": token, "email": f"{token}@example.invalid", "rol": token}


class FakeReportRequests:
    sensitive_request = {
        "request_id": "request-1",
        "report_request_id": "request-1",
        "session_id": "session-1",
        "requester_uid": "teacher-1",
        "requester_email": "teacher@example.invalid",
        "motivo": "Revisión solicitada",
        "status": "PENDING",
        "created_at": "2026-01-01T00:00:00Z",
        "participant_id": "student-private-id",
        "report_s3_key": "private/report.pdf",
        "generation_error": "private detail",
        "raw_data": {"student": "private"},
    }

    def list_requests(self, **_kwargs):
        return [dict(self.sensitive_request)], None

    def get_request(self, _request_id):
        return dict(self.sensitive_request)

    def authorize_request_access(self, request_item, **_kwargs):
        return dict(request_item)

    def approve_request(self, *_args, **_kwargs):
        result = dict(self.sensitive_request)
        result["status"] = "COMPLETED"
        return result

    def reject_request(self, *_args, **_kwargs):
        result = dict(self.sensitive_request)
        result["status"] = "REJECTED"
        return result


class FakeUsers:
    def list_public_users(self):
        return [{"uid": "admin-1", "nombre": "Administrador", "rol": "admin"}]


class FakeDynamo:
    def query(self, *_args, **_kwargs):
        return [], None


def make_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["services"] = {
        "auth_service": FakeAuth(),
        "report_request_service": FakeReportRequests(),
        "user_service": FakeUsers(),
        "dynamodb_service": FakeDynamo(),
    }
    for blueprint in (
        audit_bp,
        captures_bp,
        dashboard_bp,
        participants_bp,
        report_requests_bp,
        reports_bp,
        sessions_bp,
        trackings_bp,
        users_bp,
    ):
        app.register_blueprint(blueprint)

    @app.errorhandler(AuthenticationError)
    def authentication_error(_error):
        return jsonify({"error": "unauthenticated"}), 401

    @app.errorhandler(AuthorizationError)
    def authorization_error(_error):
        return jsonify({"error": "forbidden"}), 403

    return app.test_client()


def admin_headers():
    return {"Authorization": "Bearer admin"}


def test_administrator_cannot_read_session_tracking_participant_or_export_data():
    client = make_client()
    headers = admin_headers()
    paths = (
        "/sessions",
        "/sessions/session-1",
        "/trackings",
        "/trackings/tracking-1",
        "/trackings/tracking-1/sessions",
        "/trackings/tracking-1/evolution",
        "/participants",
        "/participants/participant-1",
        "/dashboard",
        "/dashboard/recent",
        "/dashboard/chart",
        "/reports/excel",
        "/captures/session-1",
        "/report-requests/request-1/download",
    )

    for path in paths:
        assert client.get(path, headers=headers).status_code == 403, path


def test_administrator_keeps_user_and_audit_administration_access():
    client = make_client()

    users = client.get("/users", headers=admin_headers())
    audit = client.get("/audit-logs", headers=admin_headers())

    assert users.status_code == 200
    assert users.get_json()[0]["rol"] == "admin"
    assert audit.status_code == 200


def test_administrator_report_queue_and_review_return_only_allowed_metadata():
    client = make_client()
    headers = admin_headers()

    queue = client.get("/report-requests", headers=headers)
    detail = client.get("/report-requests/request-1", headers=headers)
    reviewed = client.patch(
        "/report-requests/request-1/review",
        headers=headers,
        json={"approved": True},
    )

    assert queue.status_code == detail.status_code == reviewed.status_code == 200
    for item in [queue.get_json()["items"][0], detail.get_json(), reviewed.get_json()]:
        assert item["requester_uid"] == "teacher-1"
        assert item["session_id"] == "session-1"
        assert "requester_email" not in item
        assert "participant_id" not in item
        assert "report_s3_key" not in item
        assert "generation_error" not in item
        assert "raw_data" not in item


def test_specialist_cannot_manage_user_or_read_audit_logs():
    client = make_client()
    headers = {"Authorization": "Bearer teacher"}

    assert client.get("/users", headers=headers).status_code == 403
    assert client.get("/audit-logs", headers=headers).status_code == 403
