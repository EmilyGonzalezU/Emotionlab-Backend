from datetime import datetime, timezone
from unittest.mock import MagicMock

from services.audit_service import AuditService


def test_log_populates_gsi_timestamp_and_drops_sensitive_extra():
    dynamo = MagicMock()
    service = AuditService(dynamodb_service=dynamo)

    item = service.log(
        "REPORT_REQUESTED",
        actor_uid="actor-1",
        extra={
            "source": "report_requests",
            "access_token": "must-not-persist",
            "password": "must-not-persist",
            "nested": {"secret": "must-not-persist"},
        },
        timestamp=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )

    assert item["timestamp"] == item["iso_timestamp"]
    assert item["timestamp"] == "2026-09-12T00:00:00.000Z"
    assert item["extra"] == {"source": "report_requests"}
    dynamo.put_item.assert_called_once_with(service.table, item)


def test_audit_queries_use_declared_terraform_indexes():
    dynamo = MagicMock()
    dynamo.query.return_value = ([], None)
    service = AuditService(dynamodb_service=dynamo)

    service.list_by_actor("actor-1")
    service.list_by_action("LOGIN_OK")
    service.list_by_participant("participant-1")
    service.list_by_target_user("user-1")

    indexes = [call.kwargs["index_name"] for call in dynamo.query.call_args_list]
    assert indexes == [
        "gsi-audit-actor-date",
        "gsi-audit-action-month",
        "gsi-audit-participant",
        "gsi-audit-targetuser",
    ]
