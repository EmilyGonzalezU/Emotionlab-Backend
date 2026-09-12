"""Servicio base de acceso a Amazon DynamoDB.

Implementa las operaciones de persistencia sobre DynamoDB.
Implementa un patron singleton para reutilizar el cliente boto3.

Uso basico:
    dynamo = DynamoDBService()
    item = dynamo.get_item("mi-tabla", {"pk": "valor"})
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

import boto3
from boto3.dynamodb.conditions import Attr, BeginsWith, Between, Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


class DynamoDBService:
    """Encapsula cliente boto3 DynamoDB y helpers comunes."""

    _instance: Optional["DynamoDBService"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

        endpoint_override = os.getenv("DYNAMODB_ENDPOINT_URL")

        config: dict[str, Any] = {"region_name": region}
        if endpoint_override:
            config["endpoint_url"] = endpoint_override

        self._client = boto3.client("dynamodb", **config)
        self._resource = boto3.resource("dynamodb", **config)

        env_flag = os.getenv("DYNAMODB_LOGS")
        if getattr(sys, "frozen", False):
            self._logs_enabled = env_flag in ("1", "true", "True", "TRUE")
        else:
            self._logs_enabled = env_flag not in ("0", "false", "False", "FALSE")

        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

        self._initialized = True

    # ------------------------------------------------------------------
    # Helpers de logging
    # ------------------------------------------------------------------
    def _log(self, operation: str, detail: str) -> None:
        if not self._logs_enabled:
            return
        print(f"[DYNAMODB] {operation} {detail}", flush=True)

    # ------------------------------------------------------------------
    # Helpers de tipos
    # ------------------------------------------------------------------
    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _to_dynamo_value(value: Any) -> Any:
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {k: DynamoDBService._to_dynamo_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [DynamoDBService._to_dynamo_value(v) for v in value]
        if isinstance(value, set):
            return {DynamoDBService._to_dynamo_value(v) for v in value}
        return value

    @staticmethod
    def _from_dynamo_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            if value == int(value):
                return int(value)
            return float(value)
        if isinstance(value, dict):
            return {k: DynamoDBService._from_dynamo_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [DynamoDBService._from_dynamo_value(v) for v in value]
        if isinstance(value, set):
            return {DynamoDBService._from_dynamo_value(v) for v in value}
        return value

    @staticmethod
    def _item_to_dict(item: Mapping[str, Any]) -> dict[str, Any]:
        return DynamoDBService._from_dynamo_value(dict(item)) if item else {}

    # ------------------------------------------------------------------
    # Operaciones de bajo nivel (boto3 Table)
    # ------------------------------------------------------------------
    def table(self, table_name: str):
        return self._resource.Table(table_name)

    def get_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        consistent_read: bool = False,
    ) -> Optional[dict[str, Any]]:
        self._log("GET", f"{table_name} key={sorted(key.keys())}")
        tbl = self.table(table_name)
        response = tbl.get_item(Key=dict(key), ConsistentRead=consistent_read)
        item = response.get("Item")
        return self._item_to_dict(item) if item else None

    def put_item(
        self,
        table_name: str,
        item: Mapping[str, Any],
        condition_expression: Optional[str] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        expression_attribute_names: Optional[dict[str, str]] = None,
    ) -> bool:
        """Escribe un item. Retorna True si se escribio, False si fallo condicion."""
        self._log("PUT", f"{table_name} item_keys={sorted(item.keys())}")
        tbl = self.table(table_name)
        safe_item = self._to_dynamo_value(dict(item))
        kwargs: dict[str, Any] = {"Item": safe_item}
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = self._to_dynamo_value(
                expression_attribute_values
            )
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        try:
            tbl.put_item(**kwargs)
            return True
        except self._client.exceptions.ConditionalCheckFailedException:
            return False
        except Exception:
            raise

    def update_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        update_data: Mapping[str, Any],
        condition_expression: Optional[str] = None,
    ) -> bool:
        if not update_data:
            return True
        self._log("UPDATE", f"{table_name} key={sorted(key.keys())} fields={sorted(update_data.keys())}")
        tbl = self.table(table_name)

        update_parts: list[str] = []
        expression_values: dict[str, Any] = {}
        expression_names: dict[str, str] = {}

        for idx, (field, value) in enumerate(update_data.items()):
            placeholder = f":v{idx}"
            name_placeholder = f"#f{idx}"
            update_parts.append(f"{name_placeholder} = {placeholder}")
            expression_values[placeholder] = self._to_dynamo_value(value)
            expression_names[name_placeholder] = field

        update_expression = "SET " + ", ".join(update_parts)

        kwargs: dict[str, Any] = {
            "Key": dict(key),
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": expression_values,
            "ExpressionAttributeNames": expression_names,
        }
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        try:
            tbl.update_item(**kwargs)
            return True
        except self._client.exceptions.ConditionalCheckFailedException:
            return False

    def delete_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        condition_expression: Optional[str] = None,
    ) -> bool:
        self._log("DELETE", f"{table_name} key={sorted(key.keys())}")
        tbl = self.table(table_name)
        kwargs: dict[str, Any] = {"Key": dict(key)}
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        try:
            tbl.delete_item(**kwargs)
            return True
        except self._client.exceptions.ConditionalCheckFailedException:
            return False

    # ------------------------------------------------------------------
    # Queries / Scans con paginacion automatica
    # ------------------------------------------------------------------
    def query(
        self,
        table_name: str,
        key_condition_expression,
        index_name: Optional[str] = None,
        filter_expression=None,
        expression_attribute_names: Optional[dict[str, str]] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        scan_index_forward: bool = True,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """Retorna (items, last_evaluated_key) para paginacion."""
        self._log("QUERY", f"{table_name} index={index_name}")
        tbl = self.table(table_name)
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition_expression,
            "ScanIndexForward": scan_index_forward,
        }
        if index_name:
            kwargs["IndexName"] = index_name
        if filter_expression:
            kwargs["FilterExpression"] = filter_expression
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = self._to_dynamo_value(
                expression_attribute_values
            )
        if limit:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = dict(exclusive_start_key)

        response = tbl.query(**kwargs)
        items = [self._item_to_dict(i) for i in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, last_key

    def query_all(
        self,
        table_name: str,
        key_condition_expression,
        index_name: Optional[str] = None,
        filter_expression=None,
        expression_attribute_names: Optional[dict[str, str]] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        scan_index_forward: bool = True,
        max_items: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Realiza query paginando hasta el final y retorna todos los items."""
        all_items: list[dict[str, Any]] = []
        last_key: Optional[dict[str, Any]] = None
        while True:
            remaining = None
            if max_items is not None:
                remaining = max_items - len(all_items)
                if remaining <= 0:
                    break
            items, last_key = self.query(
                table_name=table_name,
                key_condition_expression=key_condition_expression,
                index_name=index_name,
                filter_expression=filter_expression,
                expression_attribute_names=expression_attribute_names,
                expression_attribute_values=expression_attribute_values,
                scan_index_forward=scan_index_forward,
                limit=remaining,
                exclusive_start_key=last_key,
            )
            all_items.extend(items)
            if not last_key:
                break
        return all_items

    def scan(
        self,
        table_name: str,
        index_name: Optional[str] = None,
        filter_expression=None,
        expression_attribute_names: Optional[dict[str, str]] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        self._log("SCAN", f"{table_name} index={index_name}")
        tbl = self.table(table_name)
        kwargs: dict[str, Any] = {}
        if index_name:
            kwargs["IndexName"] = index_name
        if filter_expression:
            kwargs["FilterExpression"] = filter_expression
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = self._to_dynamo_value(
                expression_attribute_values
            )
        if limit:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = dict(exclusive_start_key)

        response = tbl.scan(**kwargs)
        items = [self._item_to_dict(i) for i in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, last_key

    def scan_all(
        self,
        table_name: str,
        index_name: Optional[str] = None,
        filter_expression=None,
        expression_attribute_names: Optional[dict[str, str]] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        max_items: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        last_key: Optional[dict[str, Any]] = None
        while True:
            remaining = None
            if max_items is not None:
                remaining = max_items - len(all_items)
                if remaining <= 0:
                    break
            items, last_key = self.scan(
                table_name=table_name,
                index_name=index_name,
                filter_expression=filter_expression,
                expression_attribute_names=expression_attribute_names,
                expression_attribute_values=expression_attribute_values,
                limit=remaining,
                exclusive_start_key=last_key,
            )
            all_items.extend(items)
            if not last_key:
                break
        return all_items

    # ------------------------------------------------------------------
    # Batch write para migraciones
    # ------------------------------------------------------------------
    def batch_write(self, table_name: str, items: list[Mapping[str, Any]]) -> int:
        """Escribe una lista de items en lotes de 25 (limite DynamoDB). Retorna items escritos."""
        if not items:
            return 0
        self._log("BATCH_WRITE", f"{table_name} count={len(items)}")
        tbl = self.table(table_name)
        written = 0
        with tbl.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=self._to_dynamo_value(dict(item)))
                written += 1
        return written


class _LazyDynamoDB:
    """Compatibilidad legacy sin crear cliente hasta que alguien lo use."""

    def __init__(self):
        self._service: Optional[DynamoDBService] = None

    def _get(self) -> DynamoDBService:
        if self._service is None:
            self._service = DynamoDBService()
        return self._service

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


dynamodb = _LazyDynamoDB()
