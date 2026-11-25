"""Cliente síncrono para invocar el modelo EmotionLab V8 en AWS Lambda."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import boto3

from services.emotion_model_v8_service import build_v8_error_block


class EmotionModelV8LambdaClient:
    """Invoca exclusivamente el contrato de inferencia EmotionLab V8."""

    def __init__(
        self,
        function_name: str | None = None,
        region_name: str | None = None,
        client: Any = None,
    ) -> None:
        self.function_name = (
            function_name
            or os.getenv(
                "EMOTION_MODEL_V8_FUNCTION_NAME",
                "EmotionLabModelV8Function",
            )
        ).strip()

        if not self.function_name:
            raise ValueError("EMOTION_MODEL_V8_FUNCTION_NAME está vacío.")

        self.client = client or boto3.client(
            "lambda",
            region_name=region_name or os.getenv("AWS_REGION") or None,
        )

    def analyze(
        self,
        session_id: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id es obligatorio.")

        if not isinstance(record, Mapping):
            raise ValueError("record debe ser un objeto JSON.")

        request = {
            "session_id": session_id,
            "record": dict(record),
        }

        response = self.client.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

        payload_stream = response.get("Payload")
        if payload_stream is None:
            raise RuntimeError("La Lambda V8 no devolvió un payload.")

        raw_payload = payload_stream.read()
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")

        if response.get("FunctionError"):
            raise RuntimeError(
                f"Error en Lambda V8: {raw_payload}"
            )

        result = json.loads(raw_payload)

        if not isinstance(result, dict):
            raise RuntimeError(
                "La respuesta de Lambda V8 no es un objeto JSON."
            )

        return result

    def error_block(
        self,
        error: Exception,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return build_v8_error_block(error, session_id)
