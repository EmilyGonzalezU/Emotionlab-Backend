"""AWS Lambda entrypoint for the official EmotionLab V8 model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.emotion_model_v8_service import EmotionModelV8Service


# One service instance per Lambda execution environment.
# The constructor verifies the official V8 package and its SHA-256 manifest.
model_service = EmotionModelV8Service()


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Receive {session_id, record} and return the complete V8 result."""

    if not isinstance(event, Mapping):
        raise TypeError("El evento de Lambda debe ser un objeto JSON.")

    session_id = event.get("session_id")
    record = event.get("record")

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id es obligatorio.")

    if not isinstance(record, Mapping):
        raise ValueError("record debe ser un objeto JSON.")

    return model_service.analyze(session_id, record)
