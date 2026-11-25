from services.report_service import ReportService
from services.session_service import SessionService


def test_report_history_contains_only_sessions_of_the_request_owner():
    history = [
        {"session_id": "same-participant-a", "owner_id": "teacher-a"},
        {"session_id": "same-participant-b", "owner_id": "teacher-b"},
        {"session_id": "missing-owner", "owner_id": None},
    ]

    selected = ReportService._sessions_for_owner(history, "teacher-a")

    assert [session["session_id"] for session in selected] == ["same-participant-a"]


def test_report_history_is_empty_without_a_verified_owner():
    history = [{"session_id": "session-a", "owner_id": "teacher-a"}]

    assert ReportService._sessions_for_owner(history, None) == []


def test_session_owner_check_requests_only_owner_attribute():
    class FakeDynamo:
        def __init__(self):
            self.kwargs = None

        def query_all(self, table, condition, **kwargs):
            self.kwargs = kwargs
            return [{"owner_id": "teacher-a"}]

    dynamo = FakeDynamo()
    sessions = SessionService(dynamodb_service=dynamo)

    assert sessions.get_session_owner("session-a") == {"owner_id": "teacher-a"}
    assert dynamo.kwargs["projection_expression"] == "owner_id"
    assert dynamo.kwargs["index_name"] == "gsi-sessions-sessionid"


def test_session_list_can_skip_materializing_raw_json_from_s3():
    class FakeDynamo:
        def scan_all(self, _table):
            return [{
                "session_id": "session-a",
                "fecha": "2026-11-20",
                "raw_json": {"__s3_key": "raw/session-a.json"},
            }]

    class FakeS3:
        def __init__(self):
            self.calls = 0

        def get_session_raw_json(self, _key):
            self.calls += 1
            return {"respuestas": []}

    s3 = FakeS3()
    sessions = SessionService(dynamodb_service=FakeDynamo(), s3_service=s3)

    summary_rows = sessions.list_sessions(include_raw_json=False)
    assert summary_rows[0]["raw_json"]["__s3_key"] == "raw/session-a.json"
    assert s3.calls == 0

    detail_rows = sessions.list_sessions(include_raw_json=True)
    assert detail_rows[0]["raw_json"] == {"respuestas": []}
    assert s3.calls == 1


def test_individual_pdf_reuses_the_session_read_for_path_and_content():
    class FakeSessions:
        def __init__(self):
            self.calls = 0

        def get_session(self, _session_id):
            self.calls += 1
            return {"session_id": "session-a", "owner_id": "teacher-a", "fecha": "2026-11-20"}

        @staticmethod
        def _get_date(session):
            return session["fecha"]

    class FakeS3:
        def __init__(self):
            self.key = None

        def put_report_pdf(self, key, _content):
            self.key = key

    sessions = FakeSessions()
    storage = FakeS3()
    report = ReportService(session_service=sessions, s3_service=storage)
    report.generate_individual_pdf = lambda _session_id, **kwargs: (
        b"pdf" if kwargs["session_data"]["owner_id"] == "teacher-a" else b""
    )

    content, key = report.generate_and_upload_pdf(
        "session-a", request_id="request-a", owner_id="teacher-a"
    )

    assert sessions.calls == 1
    assert content == b"pdf"
    assert key == "reports/2026-11-20/request-a/reporte_sesion_session-a_2026-11-20.pdf"
    assert storage.key == key


def test_tracking_pdf_uses_owner_verified_session_snapshots(monkeypatch):
    from services import tracking_report_pdf

    class FakeSessions:
        def __init__(self):
            self.materialized = 0

        def _materialize_raw_json(self, session):
            self.materialized += 1
            return dict(session)

        def get_session_detail(self, _session_id):
            raise AssertionError("The verified session snapshot must be reused")

    class FakeS3:
        def put_report_pdf(self, _key, _content):
            pass

    monkeypatch.setattr(tracking_report_pdf, "build_tracking_pdf", lambda *_args: b"pdf")
    sessions = FakeSessions()
    report = ReportService(session_service=sessions, s3_service=FakeS3())

    content, key = report.generate_and_upload_tracking_pdf(
        {"tracking_id": "tracking-a", "tracking_code": "SEG-ABC"},
        [{"session_id": "session-a", "owner_id": "teacher-a"}],
        request_id="request-a",
        owner_id="teacher-a",
    )

    assert content == b"pdf"
    assert key.startswith("reports/") and "/request-a/reporte_seguimiento_SEG-ABC_" in key
    assert sessions.materialized == 1
