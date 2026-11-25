from types import SimpleNamespace

from core.constants import DYNAMODB_TABLE_TRACKINGS
from services.dynamodb_service import DynamoDBService
from services.tracking_service import TrackingService


def test_dynamodb_client_uses_configured_region_and_local_endpoint(monkeypatch):
    calls = []
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(DynamoDBService, "_instance", None)
    monkeypatch.setattr(
        "services.dynamodb_service.boto3.client",
        lambda service, **config: calls.append(("client", service, config)) or object(),
    )
    monkeypatch.setattr(
        "services.dynamodb_service.boto3.resource",
        lambda service, **config: calls.append(("resource", service, config)) or SimpleNamespace(),
    )

    DynamoDBService()

    assert calls == [
        ("client", "dynamodb", {"region_name": "us-west-2", "endpoint_url": "http://127.0.0.1:8000"}),
        ("resource", "dynamodb", {"region_name": "us-west-2", "endpoint_url": "http://127.0.0.1:8000"}),
    ]


def test_tracking_service_uses_configured_tracking_table_name():
    dynamo = SimpleNamespace(transact_write_items=lambda _operations: None)
    service = TrackingService(dynamo, session_service=object())

    assert service.trackings_table == DYNAMODB_TABLE_TRACKINGS
