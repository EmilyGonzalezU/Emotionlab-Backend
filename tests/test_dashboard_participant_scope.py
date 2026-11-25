from flask import Flask
from io import BytesIO
from openpyxl import load_workbook

from routes.dashboard_routes import dashboard_bp
from routes.participant_routes import participants_bp
from services.report_service import ReportService


class Auth:
    def verify_token(self, _token):
        return {"sub": "teacher-a"}

    def get_current_user(self, _token):
        return {"uid": "teacher-a", "rol": "teacher"}


class Dashboard:
    def __init__(self):
        self.scopes = []

    def get_date_overview(self, **kwargs):
        self.scopes.append(kwargs["owner_id"])
        return {"today": {"total": 0}, "dates": []}

    def get_recent_sessions(self, **kwargs):
        self.scopes.append(kwargs["owner_id"])
        return []

    def get_sessions_by_date(self, **kwargs):
        self.scopes.append(kwargs["owner_id"])
        return []


class Sessions:
    def list_sessions(self, **_kwargs):
        return [
            {"session_id": "own-session", "owner_id": "teacher-a", "participant_id": "OWN"},
            {"session_id": "other-session", "owner_id": "teacher-b", "participant_id": "OTHER"},
            {"session_id": "legacy-session", "participant_id": "LEGACY"},
        ]


class Participants:
    rows = [
        {"participant_id": "OWN", "nombre": "A"},
        {"participant_id": "OTHER", "nombre": "B"},
        {"participant_id": "LEGACY", "nombre": "C"},
        {"participant_id": "CREATED", "nombre": "D", "created_by": "teacher-a"},
    ]

    def list_participants(self):
        return self.rows

    def search_by_name(self, _name):
        return self.rows

    def get_participant(self, participant_id):
        return next((row for row in self.rows if row["participant_id"] == participant_id), None)

    def update_observations(self, *_args):
        raise AssertionError("foreign participant must not be modified")


def make_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    dashboard = Dashboard()
    app.extensions["services"] = {
        "auth_service": Auth(),
        "dashboard_service": dashboard,
        "session_service": Sessions(),
        "participant_service": Participants(),
    }
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(participants_bp)
    return app.test_client(), dashboard


def test_all_dashboard_endpoints_are_scoped_to_teacher_uid():
    client, service = make_client()
    headers = {"Authorization": "Bearer test"}
    assert client.get("/dashboard", headers=headers).status_code == 200
    assert client.get("/dashboard/recent", headers=headers).status_code == 200
    assert client.get("/dashboard/chart", headers=headers).status_code == 200
    assert service.scopes == ["teacher-a"] * 3


def test_participant_list_and_search_only_show_owned_or_created_records():
    client, _ = make_client()
    headers = {"Authorization": "Bearer test"}
    expected = {"OWN", "CREATED"}
    for path in ("/participants", "/participants/search?name=a"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert {row["participant_id"] for row in response.get_json()} == expected


def test_foreign_and_legacy_participants_are_hidden_from_detail_and_updates():
    client, _ = make_client()
    headers = {"Authorization": "Bearer test"}
    assert client.get("/participants/OWN", headers=headers).status_code == 200
    assert client.get("/participants/OTHER", headers=headers).status_code == 404
    assert client.get("/participants/LEGACY", headers=headers).status_code == 404
    assert client.patch("/participants/OTHER", json={"observaciones": "x"}, headers=headers).status_code == 404


def test_excel_report_owner_scope_excludes_other_and_legacy_sessions():
    service = ReportService(session_service=Sessions())
    workbook_bytes = service.generate_sessions_excel(owner_id="teacher-a")
    worksheet = load_workbook(BytesIO(workbook_bytes), read_only=True).active
    rows = list(worksheet.iter_rows(values_only=True))
    exported_ids = {row[0] for row in rows[1:]}
    assert exported_ids == {"own-session"}
