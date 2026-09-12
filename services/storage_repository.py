"""Storage repository implementations for DynamoDB and S3 backends."""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import boto3
from botocore.exceptions import ClientError

from core.constants import (
    DYNAMODB_TABLE_AUDIT_LOGS,
    DYNAMODB_TABLE_PARTICIPANTS,
    DYNAMODB_TABLE_REPORT_REQUESTS,
    DYNAMODB_TABLE_SESSIONS,
    DYNAMODB_TABLE_USERS,
)


class StorageRepository(ABC):
    @abstractmethod
    def get_item(self, table_name: str, key: Mapping[str, Any]) -> Optional[dict]: ...

    @abstractmethod
    def put_item(self, table_name: str, item: Mapping[str, Any], **kwargs) -> bool: ...

    @abstractmethod
    def update_item(self, table_name: str, key: Mapping[str, Any], update_data: Mapping[str, Any], **kwargs) -> bool: ...

    @abstractmethod
    def delete_item(self, table_name: str, key: Mapping[str, Any], **kwargs) -> bool: ...

    @abstractmethod
    def scan_all(self, table_name: str, **kwargs) -> list[dict]: ...

    @abstractmethod
    def query_all(self, table_name: str, key_condition_expression, **kwargs) -> list[dict]: ...

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DynamoDBRepository(StorageRepository):
    """Adapter preserving the current DynamoDB service contract."""

    def __init__(self, dynamodb_service):
        self._service = dynamodb_service

    def get_item(self, table_name, key):
        return self._service.get_item(table_name, key)

    def put_item(self, table_name, item, **kwargs):
        return self._service.put_item(table_name, item, **kwargs)

    def update_item(self, table_name, key, update_data=None, **kwargs):
        update_data = update_data if update_data is not None else kwargs.pop("attribute_updates", {})
        return self._service.update_item(table_name, key, update_data, **kwargs)

    def delete_item(self, table_name, key, **kwargs):
        return self._service.delete_item(table_name, key, **kwargs)

    def scan_all(self, table_name, **kwargs):
        return self._service.scan_all(table_name, **kwargs)

    def query_all(self, table_name, key_condition_expression, **kwargs):
        return self._service.query_all(table_name, key_condition_expression, **kwargs)

    def query(self, *args, **kwargs):
        return self._service.query(*args, **kwargs)

    def scan(self, *args, **kwargs):
        return self._service.scan(*args, **kwargs)

    def table(self, table_name):
        return self._service.table(table_name)


class S3Repository(StorageRepository):
    """JSON object repository. One application entity is stored per S3 object."""

    PREFIXES = {
        DYNAMODB_TABLE_USERS: "users",
        DYNAMODB_TABLE_PARTICIPANTS: "participants",
        DYNAMODB_TABLE_SESSIONS: "sessions",
        DYNAMODB_TABLE_REPORT_REQUESTS: "report-requests",
        DYNAMODB_TABLE_AUDIT_LOGS: "audit-logs",
    }

    def __init__(self, bucket: Optional[str] = None, client=None, region: Optional[str] = None):
        self.bucket = (bucket or os.getenv("S3_DATA_BUCKET", "")).strip()
        if not self.bucket:
            raise RuntimeError("S3_DATA_BUCKET es obligatorio cuando STORAGE_BACKEND=s3")
        self.client = client or boto3.client(
            "s3", region_name=region or os.getenv("AWS_REGION", "us-east-1")
        )

    def _prefix(self, table_name: str) -> str:
        try:
            return self.PREFIXES[table_name]
        except KeyError as error:
            raise ValueError(f"Tabla no soportada por S3Repository: {table_name}") from error

    def _key(self, table_name: str, item: Mapping[str, Any]) -> str:
        prefix = self._prefix(table_name)
        if table_name == DYNAMODB_TABLE_USERS:
            identifier = item.get("uid") or item.get("user_id")
        elif table_name == DYNAMODB_TABLE_PARTICIPANTS:
            identifier = item.get("participant_id")
        elif table_name == DYNAMODB_TABLE_SESSIONS:
            identifier = item.get("session_id") or item.get("id")
        elif table_name == DYNAMODB_TABLE_REPORT_REQUESTS:
            identifier = item.get("request_id")
        else:
            identifier = item.get("audit_id") or item.get("sk") or uuid.uuid4().hex
        if not identifier:
            raise ValueError(f"Entidad sin identificador para {table_name}")
        return f"{prefix}/{identifier}.json"

    def _key_from_mapping(self, table_name: str, key: Mapping[str, Any]) -> str:
        return self._key(table_name, key)

    def _read_key(self, key: str) -> Optional[dict]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    def get_item(self, table_name, key):
        return self._read_key(self._key_from_mapping(table_name, key))

    def put_item(self, table_name, item, **kwargs):
        key = self._key(table_name, item)
        if kwargs.get("condition_expression") is not None and self._read_key(key) is not None:
            return False
        body = json.dumps(dict(item), ensure_ascii=False, default=str).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        return True

    def update_item(self, table_name, key, update_data=None, **kwargs):
        update_data = update_data if update_data is not None else kwargs.pop("attribute_updates", {})
        current = self.get_item(table_name, key)
        if current is None:
            return False
        current.update(dict(update_data))
        self.put_item(table_name, current)
        return True

    def delete_item(self, table_name, key, **kwargs):
        self.client.delete_object(Bucket=self.bucket, Key=self._key_from_mapping(table_name, key))
        return True

    def _list_prefix(self, table_name: str) -> list[dict]:
        prefix = self._prefix(table_name) + "/"
        items = []
        token = None
        while True:
            params = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                params["ContinuationToken"] = token
            response = self.client.list_objects_v2(**params)
            for obj in response.get("Contents", []):
                item = self._read_key(obj["Key"])
                if item is not None:
                    items.append(item)
            token = response.get("NextContinuationToken")
            if not token:
                return items

    def scan_all(self, table_name, **kwargs):
        items = self._list_prefix(table_name)
        max_items = kwargs.get("max_items")
        return items[:max_items] if max_items else items

    @staticmethod
    def _condition_filter(expression):
        values = getattr(expression, "_values", ())
        if len(values) == 2:
            attribute, expected = values
            name = getattr(attribute, "name", None)
            if name:
                return name, expected
        return None, None

    def query_all(self, table_name, key_condition_expression, **kwargs):
        name, expected = self._condition_filter(key_condition_expression)
        items = self._list_prefix(table_name)
        if name is not None:
            items = [item for item in items if item.get(name) == expected]
        return items[: kwargs["max_items"]] if kwargs.get("max_items") else items

    def query(self, table_name, key_condition_expression, **kwargs):
        items = self.query_all(table_name, key_condition_expression, **kwargs)
        limit = kwargs.get("limit")
        return (items[:limit] if limit else items), None

    def scan(self, table_name, **kwargs):
        items = self.scan_all(table_name, max_items=kwargs.get("limit"))
        return items, None
