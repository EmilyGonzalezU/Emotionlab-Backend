import pytest

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from services.report_request_service import (
    ReportRequestService,
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_PENDING,
)


class FakeDynamo:
    def __init__(self):
        self.items = {}
        self.updates = 0

    def put_item(self, table, item, **kwargs):
        self.items[(item["requester_uid"], item["sk"])] = dict(item)

    def query(self, table, condition, **kwargs):
        # list_my_requests always queries by requester_uid in these tests.
        requester_uid = condition._values[-1]
        rows = [
            dict(item) for (uid, _), item in self.items.items()
            if uid == requester_uid
        ]
        return rows, None

    def query_all(self, table, condition, **kwargs):
        request_id = condition._values[-1]
        return [
            dict(item) for item in self.items.values()
            if item.get("report_request_id") == request_id
        ]

    def update_item(self, table, *, key, update_data, **kwargs):
        self.updates += 1
        item = self.items[(key["requester_uid"], key["sk"])]
        item.update(update_data)
        for field in kwargs.get("remove_fields", []):
            item.pop(field, None)
        return item


class FakeSessions:
    def __init__(self, sessions):
        self.sessions = sessions
        self.forbid_full_reads = False

    def get_session(self, session_id):
        if self.forbid_full_reads:
            raise AssertionError("Administrative report review loaded session content")
        session = self.sessions.get(session_id)
        return dict(session) if session else None

    def get_session_owner(self, session_id):
        session = self.sessions.get(session_id)
        return {"owner_id": session.get("owner_id")} if session else None


class FakeReportGenerator:
    def __init__(self):
        self.calls = []

    def generate_and_upload_pdf(self, session_id, *, request_id, owner_id=None):
        self.calls.append((session_id, request_id))
        return b"pdf", f"reports/{request_id}.pdf"


class FakeS3:
    report_bucket = "private-reports"

    def __init__(self):
        self.calls = []

    def generate_presigned_download_url(self, **kwargs):
        self.calls.append(kwargs)
        return "https://example.invalid/presigned"


class FakeAudit:
    def __getattr__(self, name):
        if name.startswith("log_report_"):
            return lambda **kwargs: None
        raise AttributeError(name)


@pytest.fixture
def service_factory():
    def make(sessions):
        dynamo = FakeDynamo()
        report = FakeReportGenerator()
        s3 = FakeS3()
        service = ReportRequestService(
            dynamodb_service=dynamo,
            audit_svc=FakeAudit(),
            report_service=report,
            session_service=FakeSessions(sessions),
            s3_service=s3,
            ses_service=None,
        )
        return service, dynamo, report, s3
    return make


def session(owner_id="teacher-a", participant_id="participant-1"):
    return {"owner_id": owner_id, "participant_id": participant_id}


def create(service, uid="teacher-a", session_id="session-a", role=ROLE_TEACHER):
    return service.create_request(
        session_id,
        requester_uid=uid,
        requester_email=f"{uid}@example.invalid",
        requester_rol=role,
    )


def test_owner_can_create_request_for_own_session(service_factory):
    service, _, _, _ = service_factory({"session-a": session()})
    request = create(service)
    assert request["session_id"] == "session-a"
    assert request["requester_uid"] == "teacher-a"
    assert request["status"] == STATUS_PENDING


@pytest.mark.parametrize(
    "sessions,session_id",
    [
        ({"session-b": session("teacher-b")}, "session-b"),
        ({}, "missing-session"),
        ({"ownerless": {"participant_id": "participant-1"}}, "ownerless"),
    ],
)
def test_foreign_missing_and_ownerless_sessions_are_indistinguishable(
    service_factory, sessions, session_id
):
    service, _, _, _ = service_factory(sessions)
    with pytest.raises(PermissionError, match="Solicitud no disponible"):
        create(service, session_id=session_id)


def test_manipulated_session_id_cannot_be_used_by_another_specialist(service_factory):
    service, dynamo, _, _ = service_factory({"session-b": session("teacher-b")})
    with pytest.raises(PermissionError):
        create(service, session_id="session-b")
    assert not dynamo.items


def test_specialist_cannot_read_another_request_or_download_it(service_factory):
    service, _, _, s3 = service_factory({"session-a": session()})
    request = create(service)
    request.update({"status": STATUS_COMPLETED, "report_s3_key": "reports/a.pdf"})
    key = (request["requester_uid"], request["sk"])
    service.dynamo.items[key].update(request)

    with pytest.raises(PermissionError, match="Solicitud no disponible"):
        service.authorize_request_access(
            request,
            actor_uid="teacher-b",
            actor_rol=ROLE_TEACHER,
        )
    with pytest.raises(PermissionError, match="Solicitud no disponible"):
        service.get_download_url(
            request["request_id"],
            actor_uid="teacher-b",
            actor_email="b@example.invalid",
            actor_rol=ROLE_TEACHER,
        )
    assert not s3.calls


def test_admin_approval_does_not_bypass_original_session_owner(service_factory):
    service, dynamo, report, _ = service_factory({"session-b": session("teacher-b")})
    forged = {
        "request_id": "forged-id",
        "report_request_id": "forged-id",
        "sk": "REQUEST#forged-id",
        "requester_uid": "teacher-a",
        "requester_email": "a@example.invalid",
        "session_id": "session-b",
        "participant_id": "participant-1",
        "status": STATUS_PENDING,
    }
    dynamo.items[("teacher-a", forged["sk"])] = forged

    with pytest.raises(PermissionError, match="Solicitud no disponible"):
        service.approve_request(
            "forged-id",
            reviewer_uid="admin-1",
            reviewer_email="admin@example.invalid",
            reviewer_rol=ROLE_ADMIN,
        )
    assert dynamo.updates == 0
    assert not report.calls


def test_owner_change_blocks_generation_retry_and_download(service_factory):
    service, dynamo, report, s3 = service_factory({"session-a": session("teacher-b")})
    request = {
        "request_id": "request-1",
        "report_request_id": "request-1",
        "sk": "REQUEST#request-1",
        "requester_uid": "teacher-a",
        "requester_email": "a@example.invalid",
        "session_id": "session-a",
        "participant_id": "participant-1",
        "status": STATUS_APPROVED,
        "report_s3_key": "reports/a.pdf",
    }
    dynamo.items[("teacher-a", request["sk"])] = request

    with pytest.raises(PermissionError):
        service._generate_and_store_pdf("request-1")
    with pytest.raises(PermissionError):
        service.regenerate_report(
            "request-1",
            reviewer_uid="admin-1",
            reviewer_email="admin@example.invalid",
            reviewer_rol=ROLE_ADMIN,
        )
    with pytest.raises(PermissionError):
        service.get_download_url(
            "request-1",
            actor_uid="teacher-a",
            actor_email="a@example.invalid",
            actor_rol=ROLE_TEACHER,
        )
    assert not report.calls
    assert not s3.calls


def test_admin_can_approve_but_cannot_download_report(service_factory):
    service, _, report, s3 = service_factory({"session-a": session()})
    request = create(service)
    service.session_service.forbid_full_reads = True

    result = service.approve_request(
        request["request_id"],
        reviewer_uid="admin-1",
        reviewer_email="admin@example.invalid",
        reviewer_rol=ROLE_ADMIN,
    )
    assert result["status"] == STATUS_COMPLETED
    assert report.calls == [("session-a", request["request_id"])]

    with pytest.raises(PermissionError, match="Solicitud no disponible"):
        service.get_download_url(
            request["request_id"],
            actor_uid="admin-1",
            actor_email="admin@example.invalid",
            actor_rol=ROLE_ADMIN,
        )
    assert not s3.calls

    # Only the submitting specialist can download a completed report.
    url = service.get_download_url(
        request["request_id"],
        actor_uid="teacher-a",
        actor_email="teacher-a@example.invalid",
        actor_rol=ROLE_TEACHER,
    )
    assert url.startswith("https://")
    assert len(s3.calls) == 1
