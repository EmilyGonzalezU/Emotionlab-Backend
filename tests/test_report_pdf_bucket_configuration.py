from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from services.report_request_service import ReportRequestService, STATUS_COMPLETED
from services.report_service import ReportService
from services.s3_service import S3Service


AUTHORIZED_BUCKET = "emotionlab-dev-report-pdfs"
S3_ENCRYPTION_FIELDS = {
    "ServerSideEncryption",
    "SSEKMSKeyId",
    "SSECustomerAlgorithm",
    "SSECustomerKey",
    "SSECustomerKeyMD5",
}


class FakeDynamo:
    def __init__(self):
        self.items = {}

    def put_item(self, _table, item, **_kwargs):
        self.items[(item["requester_uid"], item["sk"])] = dict(item)

    def query(self, _table, condition, **_kwargs):
        requester_uid = condition._values[-1]
        return [
            dict(item) for (uid, _), item in self.items.items()
            if uid == requester_uid
        ], None

    def query_all(self, _table, condition, **_kwargs):
        request_id = condition._values[-1]
        return [
            dict(item) for item in self.items.values()
            if item.get("report_request_id") == request_id
        ]

    def update_item(self, _table, *, key, update_data, **_kwargs):
        item = self.items[(key["requester_uid"], key["sk"])]
        item.update(update_data)
        return item


class FakeSession:
    def __init__(self):
        self.record = {
            "owner_id": "teacher-1",
            "participant_id": None,
            "fecha": "2026-11-24",
            "raw_json": {
                "session_data": {
                    "fechaInicio": "2026-11-24 09:00:00",
                    "fechaFin": "2026-11-24 09:30:00",
                },
            },
        }

    def get_session_owner(self, _session_id):
        return {"owner_id": self.record["owner_id"]}

    def get_session(self, _session_id):
        return dict(self.record)

    def get_session_detail(self, session_id):
        return {**self.record, "session_id": session_id}

    @staticmethod
    def _get_date(_session):
        return "2026-11-24"


def test_admin_approval_uploads_report_to_authorized_bucket(monkeypatch, caplog):
    caplog.set_level("INFO", logger="services.s3_service")
    monkeypatch.setenv("S3_REPORT_PDFS_BUCKET", AUTHORIZED_BUCKET)
    monkeypatch.setenv("EMOTIONLAB_S3_REPORT_BUCKET_SOURCE", "dotenv:test-config")
    S3Service._instance = None
    client = Mock()
    resource = Mock()
    try:
        with patch("services.s3_service.boto3.client", return_value=client), patch(
            "services.s3_service.boto3.resource", return_value=resource
        ):
            s3 = S3Service()
            sessions = FakeSession()
            report = ReportService(session_service=sessions, s3_service=s3)
            dynamo = FakeDynamo()
            requests = ReportRequestService(
                dynamodb_service=dynamo,
                audit_svc=Mock(),
                report_service=report,
                session_service=sessions,
                s3_service=s3,
                ses_service=False,
            )

            request = requests.create_request(
                "session-1",
                requester_uid="teacher-1",
                requester_email="teacher@example.invalid",
                requester_rol=ROLE_TEACHER,
            )
            result = requests.approve_request(
                request["request_id"],
                reviewer_uid="admin-1",
                reviewer_email="admin@example.invalid",
                reviewer_rol=ROLE_ADMIN,
            )

        assert result["status"] == STATUS_COMPLETED
        client.put_object.assert_called_once()
        upload = client.put_object.call_args.kwargs
        assert upload["Bucket"] == AUTHORIZED_BUCKET
        assert upload["ServerSideEncryption"] == "aws:kms"
        assert "SSEKMSKeyId" not in upload
        assert S3_ENCRYPTION_FIELDS.intersection(upload) == {"ServerSideEncryption"}
        assert upload["Body"].startswith(b"%PDF-")
        assert upload["Key"].startswith(
            f"reports/2026-11-24/{request['request_id']}/"
        )
        assert "bucket=emotionlab-dev-report-pdfs" in caplog.text
        assert "config_source=dotenv:test-config" in caplog.text
    finally:
        S3Service._instance = None


def test_legacy_or_unapproved_report_bucket_is_rejected_before_s3_client(monkeypatch):
    monkeypatch.setenv("S3_REPORT_PDFS_BUCKET", "emotionlab-dev-repo")
    S3Service._instance = None
    try:
        with patch("services.s3_service.boto3.client") as client:
            with pytest.raises(RuntimeError, match="Bucket de informes PDF no autorizado"):
                S3Service()
            client.assert_not_called()
    finally:
        S3Service._instance = None


def test_tracking_report_upload_uses_same_authorized_bucket(monkeypatch):
    monkeypatch.setenv("S3_REPORT_PDFS_BUCKET", AUTHORIZED_BUCKET)
    monkeypatch.setenv("EMOTIONLAB_S3_REPORT_BUCKET_SOURCE", "dotenv:test-config")
    S3Service._instance = None
    client = Mock()
    try:
        with patch("services.s3_service.boto3.client", return_value=client), patch(
            "services.s3_service.boto3.resource", return_value=Mock()
        ), patch(
            "services.tracking_report_pdf.build_tracking_pdf",
            return_value=b"%PDF-tracking-test",
        ):
            s3 = S3Service()
            report = ReportService(session_service=FakeSession(), s3_service=s3)
            _, key = report.generate_and_upload_tracking_pdf(
                {"tracking_id": "tracking-1", "tracking_code": "SEG-1"},
                [{"session_id": "session-1"}],
                request_id="request-1",
                owner_id="teacher-1",
            )

        upload = client.put_object.call_args.kwargs
        assert upload["Bucket"] == AUTHORIZED_BUCKET
        assert upload["ServerSideEncryption"] == "aws:kms"
        assert "SSEKMSKeyId" not in upload
        assert S3_ENCRYPTION_FIELDS.intersection(upload) == {"ServerSideEncryption"}
        assert upload["Key"] == key
        assert key.startswith("reports/")
        assert "/request-1/reporte_seguimiento_SEG-1_" in key
    finally:
        S3Service._instance = None


def test_non_report_s3_upload_does_not_receive_report_encryption_parameters(monkeypatch):
    monkeypatch.setenv("S3_REPORT_PDFS_BUCKET", AUTHORIZED_BUCKET)
    monkeypatch.setenv("S3_SESSIONS_RAW_BUCKET", "emotionlab-test-sessions-raw")
    S3Service._instance = None
    client = Mock()
    try:
        with patch("services.s3_service.boto3.client", return_value=client), patch(
            "services.s3_service.boto3.resource", return_value=Mock()
        ):
            s3 = S3Service()
            s3.put_session_raw_json("sessions/session-1.json", b"{}")

        upload = client.put_object.call_args.kwargs
        assert upload["ContentType"] == "application/json"
        assert "ServerSideEncryption" not in upload
        assert "SSEKMSKeyId" not in upload
        assert S3_ENCRYPTION_FIELDS.isdisjoint(upload)
    finally:
        S3Service._instance = None
