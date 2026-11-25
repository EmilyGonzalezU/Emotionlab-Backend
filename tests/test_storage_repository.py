from __future__ import annotations

from botocore.exceptions import ClientError

from services.storage_repository import S3Repository


class Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self):
        return self.value


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs["Key"]))
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs["Key"]))
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": Body(self.objects[kwargs["Key"]])}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs["Key"]))
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        keys = [{"Key": key} for key in self.objects if key.startswith(prefix)]
        return {"Contents": keys}


def build_repo():
    return S3Repository(bucket="test-data", client=FakeS3())


def test_s3_user_repository_round_trip():
    repo = build_repo()
    repo.put_item("emotionlab-dev-users", {"uid": "u1", "email": "u@example.com"})
    assert repo.get_item("emotionlab-dev-users", {"uid": "u1"})["email"] == "u@example.com"


def test_s3_participant_repository_search():
    repo = build_repo()
    repo.put_item("emotionlab-dev-participants", {"participant_id": "AB", "nombre": "Usuario Alfa"})
    repo.put_item("emotionlab-dev-participants", {"participant_id": "CD", "nombre": "Usuario Beta"})
    items = repo.query_all("emotionlab-dev-participants", type("Expr", (), {"_values": (type("Attr", (), {"name": "nombre"})(), "Usuario Alfa")})())
    assert items == []
    assert len(repo.scan_all("emotionlab-dev-participants")) == 2


def test_s3_session_repository_updates_model_processing():
    repo = build_repo()
    repo.put_item("emotionlab-dev-sessions", {"session_id": "s1", "model_processing": None})
    repo.update_item(
        "emotionlab-dev-sessions",
        {"session_id": "s1"},
        {"model_processing": {"status": "analyzed", "prediction": {"group": 1}}},
    )
    stored = repo.get_item("emotionlab-dev-sessions", {"session_id": "s1"})
    assert stored["model_processing"]["prediction"]["group"] == 1


def test_s3_report_request_and_audit_keys():
    repo = build_repo()
    repo.put_item("emotionlab-dev-report-requests", {"request_id": "r1", "status": "PENDING"})
    repo.put_item("emotionlab-dev-audit-logs", {"audit_id": "a1", "action": "LOGIN_OK"})
    assert "report-requests/r1.json" in repo.client.objects
    assert "audit-logs/a1.json" in repo.client.objects


def test_s3_missing_entity_and_delete():
    repo = build_repo()
    assert repo.get_item("emotionlab-dev-users", {"uid": "missing"}) is None
    repo.put_item("emotionlab-dev-users", {"uid": "u1"})
    repo.delete_item("emotionlab-dev-users", {"uid": "u1"})
    assert repo.get_item("emotionlab-dev-users", {"uid": "u1"}) is None
