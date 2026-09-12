# =============================================================================
# setup_s3_configs.py
# =============================================================================
# Script IDEMPOTENTE para aplicar configuraciones de seguridad S3 directamente
# via boto3. Diseñado para AWS Learner Lab, donde un SCP deniega
# s3:GetObjectLockConfiguration y por tanto el provider hashicorp/aws 5.x no
# puede gestionar las configuraciones child S3 (siempre consulta Object Lock
# durante el refresh, lo cual se bloquea con 403 AccessDenied).
#
# Las 5 configuraciones aplicadas (ninguna requiere permisos sobre Object Lock):
#   1. PutPublicAccessBlock           -> Bloqueo de acceso público completo
#   2. PutBucketEncryption            -> SSE-KMS (alias/aws/s3) + Bucket Key
#   3. PutBucketVersioning            -> Suspended (dev; cambiar a Enabled prod)
#   4. PutBucketOwnershipControls     -> BucketOwnerEnforced
#   5. PutBucketPolicy                -> Deny HTTP (SÓLO TLS) + requiere KMS upload
#
# USO:
#   python scripts/setup_s3_configs.py           # Aplicar configuraciones
#   python scripts/setup_s3_configs.py --verify  # Verificar ya aplicadas
#
# CREDENCIALES (NUNCA escribir en el código):
#   Resueltas por la cadena default de boto3:
#     1. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
#     2. ~/.aws/credentials  [profile default]
#     3. IAM Role (ECS/EC2 instance role)
#
# NO USA: s3:GetObjectLockConfiguration. Compatibilidad 100% con SCP Learner Lab.
# =============================================================================

from __future__ import annotations

import argparse
import json
import sys
import io
from typing import Dict, List, Tuple

import boto3
from botocore.exceptions import ClientError


# =============================================================================
# FIX: compatibilidad con codificacion PowerShell 5 (Windows).
# Forzamos UTF-8 y usamos caracteres ASCII robustos en lugar de unicode special.
# =============================================================================
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass  # Python < 3.7 / streams no reconfigurables

OK_MARK   = "[OK]"
FAIL_MARK = "[FAIL]"
BULLET    = "*"


BUCKETS: List[Tuple[str, str]] = [
    ("emotionlab-dev-report-pdfs", "Report PDFs (privado, acceso por backend via IAM)"),
    ("emotionlab-dev-sessions-raw", "Sesiones raw JSON cuando superan 350KB DynamoDB"),
]


def _build_https_only_and_require_kms_policy(bucket_arn: str) -> Dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnforceTLSOnly",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [bucket_arn, f"{bucket_arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "RequireKMSEncryptionOnUpload",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": f"{bucket_arn}/*",
                "Condition": {
                    "StringNotEquals": {
                        "s3:x-amz-server-side-encryption": "aws:kms"
                    }
                },
            },
        ],
    }


def _region() -> str:
    import os
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def s3_client():
    session = boto3.session.Session()
    return session.client("s3", region_name=_region())


def _ensure_bucket_exists(client, bucket: str) -> None:
    """HeadBucket -> si no existe, abortar."""
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in ("404", "NotFound"):
            raise RuntimeError(
                f"BUCKET NO EXISTE: {bucket}. Detenido sin modificaciones. "
                "Verifica que terraform apply creó los buckets o créalos manualmente."
            ) from exc
        # 403 Forbidden: SCP bloquea HeadBucket? No pasa en Learner Lab normal.
        raise


def apply_public_access_block(client, bucket: str) -> str:
    client.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    return "PublicAccessBlock: TODOS=true"


def apply_encryption(client, bucket: str) -> str:
    kwargs = {
        "Bucket": bucket,
        "ServerSideEncryptionConfiguration": {
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "aws:kms",
                        "KMSMasterKeyID": "alias/aws/s3",
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        },
    }
    owner = _account_id_from_arn()
    if owner:
        kwargs["ExpectedBucketOwner"] = owner
    client.put_bucket_encryption(**kwargs)
    return "Encryption: aws:kms (alias/aws/s3) + BucketKeyEnabled"


def apply_versioning(client, bucket: str) -> str:
    # Etapa DEV -> Suspended. Cambiar a Enabled en producción.
    client.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Suspended"},
    )
    return "Versioning: Suspended (dev)"


def apply_ownership_controls(client, bucket: str) -> str:
    client.put_bucket_ownership_controls(
        Bucket=bucket,
        OwnershipControls={
            "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
        },
    )
    return "OwnershipControls: BucketOwnerEnforced"


def apply_bucket_policy(client, bucket: str) -> str:
    bucket_arn = f"arn:aws:s3:::{bucket}"
    policy = _build_https_only_and_require_kms_policy(bucket_arn)
    client.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(policy, ensure_ascii=False),
    )
    return "Policy: TLS only + Require aws:kms on upload"


def verify_all(client, bucket: str) -> Dict[str, bool]:
    result: Dict[str, bool] = {}

    pub = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    result["public_access_block"] = all([
        pub["BlockPublicAcls"], pub["IgnorePublicAcls"],
        pub["BlockPublicPolicy"], pub["RestrictPublicBuckets"],
    ])

    enc = client.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"][0]
    result["encryption_kms"] = (
        enc["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        and enc.get("BucketKeyEnabled") is True
    )

    ver = client.get_bucket_versioning(Bucket=bucket).get("Status", "Suspended")
    result["versioning_suspended_dev"] = (ver == "Suspended")

    own = client.get_bucket_ownership_controls(Bucket=bucket)["OwnershipControls"]["Rules"][0]["ObjectOwnership"]
    result["ownership_enforced"] = (own == "BucketOwnerEnforced")

    try:
        policy_str = client.get_bucket_policy(Bucket=bucket)["Policy"]
        policy = json.loads(policy_str)
        statements = {s.get("Sid"): s for s in policy.get("Statement", [])}
        tls_ok = (
            isinstance(statements.get("EnforceTLSOnly"), dict)
            and statements["EnforceTLSOnly"]["Effect"] == "Deny"
        )
        kms_ok = (
            isinstance(statements.get("RequireKMSEncryptionOnUpload"), dict)
            and statements["RequireKMSEncryptionOnUpload"]["Effect"] == "Deny"
        )
        result["policy_https_and_kms_required"] = tls_ok and kms_ok
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchBucketPolicy", "404"):
            result["policy_https_and_kms_required"] = False
        else:
            raise
    return result


def _account_id_from_arn() -> str | None:
    """Learner Lab: ExpectedBucketOwner es opcional. Devolvemos None por defecto."""
    return None


def _banner(text: str) -> None:
    bar = "=" * 80
    print(f"\n{bar}\n{text}\n{bar}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aplicar configuraciones de seguridad a buckets S3 de EmotionLab "
            "(compatible con AWS Learner Lab: NO usa GetObjectLockConfiguration)."
        )
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Solo verifica que las configuraciones estén aplicadas (no modifica nada).",
    )
    args = parser.parse_args()

    client = s3_client()

    _banner(f"CONFIGURACIÓN S3 | {'MODO VERIFICACIÓN (no modifica)' if args.verify else 'MODO APLICAR'} | region={_region()}")

    all_ok = True
    for bucket, purpose in BUCKETS:
        print(f"{BULLET} Bucket: {bucket:<40} Propósito: {purpose}")
        try:
            _ensure_bucket_exists(client, bucket)
        except RuntimeError as exc:
            print(f"  {FAIL_MARK} {exc}")
            all_ok = False
            continue

        if not args.verify:
            steps = [
                apply_public_access_block,
                apply_encryption,
                apply_versioning,
                apply_ownership_controls,
                apply_bucket_policy,
            ]
            for step in steps:
                print(f"    - {step(client, bucket)}")
            print(f"    {OK_MARK} Todas las configuraciones aplicadas (idempotente).\n")

        status = verify_all(client, bucket)
        print("    VERIFICACIÓN:")
        for k, ok in status.items():
            mark = OK_MARK if ok else FAIL_MARK
            print(f"      {mark} {k:<50} -> {ok}")
            if not ok:
                all_ok = False
        print()

    _banner("RESUMEN")
    if all_ok:
        print(f"RESULTADO: {OK_MARK} TODAS LAS CONFIGURACIONES APLICADAS Y VERIFICADAS.")
        return 0
    print(f"RESULTADO: {FAIL_MARK} AL MENOS UNA CONFIGURACIÓN NO ESTÁ CORRECTA.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
