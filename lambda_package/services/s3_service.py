"""Servicio de almacenamiento en Amazon S3.

Gestiona buckets privados para:
- PDFs de informes (report-pdfs)
- JSON raw de sesiones > 350KB (sessions-raw)

Utiliza presigned URLs para descargas autenticadas sin exponer el bucket.
"""
from __future__ import annotations

import os
from io import BytesIO
from typing import Any, Mapping, Optional, Union

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

class S3Service:
    """Cliente S3 para buckets privados de EmotionLab."""

    _instance: Optional["S3Service"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        region = os.getenv(
        "AWS_REGION",
            os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )

        endpoint = os.getenv("S3_ENDPOINT_URL")

        client_config: dict[str, Any] = {
            "region_name": region,
            "config": Config(signature_version="s3v4"),
        }

        if endpoint:
            client_config["endpoint_url"] = endpoint

        self._client = boto3.client("s3", **client_config)
        self._resource = boto3.resource("s3", **client_config)
        self.report_bucket = os.getenv("S3_REPORT_PDFS_BUCKET", "").strip()
        self.sessions_raw_bucket = os.getenv("S3_SESSIONS_RAW_BUCKET", "").strip()

        self._initialized = True

    # ------------------------------------------------------------------
    # Helpers genericos
    # ------------------------------------------------------------------
    def require_bucket(self, bucket: str) -> None:
        if not bucket:
            raise RuntimeError("El nombre del bucket S3 no esta configurado")

    def put_object(
        self,
        bucket: str,
        key: str,
        body: Union[bytes, str, BytesIO],
        *,
        content_type: Optional[str] = None,
        metadata: Optional[Mapping[str, str]] = None,
        tags: Optional[str] = None,
    ) -> None:
        self.require_bucket(bucket)
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body.getvalue() if isinstance(body, BytesIO) else body,
            "ServerSideEncryption": "aws:kms",
        }
        if content_type:
            params["ContentType"] = content_type
        if metadata:
            params["Metadata"] = dict(metadata)
        if tags:
            params["Tagging"] = tags
        self._client.put_object(**params)

    def get_object(self, bucket: str, key: str) -> bytes:
        self.require_bucket(bucket)
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def get_object_stream(self, bucket: str, key: str) -> BytesIO:
        return BytesIO(self.get_object(bucket, key))

    def object_exists(self, bucket: str, key: str) -> bool:
        self.require_bucket(bucket)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def delete_object(self, bucket: str, key: str) -> None:
        self.require_bucket(bucket)
        self._client.delete_object(Bucket=bucket, Key=key)

    def generate_presigned_download_url(
        self,
        bucket: str,
        key: str,
        *,
        expires_in: int = 300,
        download_filename: Optional[str] = None,
    ) -> str:
        """Genera una URL presignada de descarga (ExpiresIn en segundos)."""
        self.require_bucket(bucket)
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
        }
        if download_filename:
            params["ResponseContentDisposition"] = (
                f'attachment; filename="{download_filename}"'
            )
            params["ResponseContentType"] = "application/pdf"
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    # ------------------------------------------------------------------
    # Helpers especificos de EmotionLab
    # ------------------------------------------------------------------
    def put_report_pdf(
        self,
        key: str,
        pdf_bytes: bytes,
        *,
        metadata: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.require_bucket(self.report_bucket)
        self.put_object(
            bucket=self.report_bucket,
            key=key,
            body=pdf_bytes,
            content_type="application/pdf",
            metadata=metadata,
        )

    def get_report_pdf_url(
        self,
        key: str,
        *,
        download_filename: Optional[str] = None,
        expires_in: int = 300,
    ) -> str:
        return self.generate_presigned_download_url(
            bucket=self.report_bucket,
            key=key,
            expires_in=expires_in,
            download_filename=download_filename,
        )

    def put_session_raw_json(
        self,
        key: str,
        json_bytes: bytes,
    ) -> None:
        self.require_bucket(self.sessions_raw_bucket)
        self.put_object(
            bucket=self.sessions_raw_bucket,
            key=key,
            body=json_bytes,
            content_type="application/json",
        )

    def get_session_raw_json(self, key: str) -> dict:
        import json

        self.require_bucket(self.sessions_raw_bucket)
        data = self.get_object(self.sessions_raw_bucket, key)
        return json.loads(data.decode("utf-8"))
