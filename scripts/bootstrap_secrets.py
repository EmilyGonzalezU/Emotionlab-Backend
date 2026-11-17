"""Inicializa de forma segura y reproducible los secretos de EmotionLab.

Crea la PRIMERA versión en AWS Secrets Manager de los secretos que aún no
tienen valor. Es idempotente: si un secreto ya tiene una versión vigente, se
conserva sin cambios.

El valor se genera en memoria (módulo ``secrets`` de la librería estándar) y se
envía directamente a Secrets Manager. Nunca se escribe en el repositorio, en
``terraform.tfvars`` ni en el state de Terraform, y nunca se imprime por
pantalla.

Uso (PowerShell):

    .venv\\Scripts\\python.exe scripts/bootstrap_secrets.py `
        --region us-east-1 `
        --jwt-secret-id emotionlab/dev/JWT_SECRET `
        --hmac-secret-id emotionlab/dev/SESSION_CODE_HMAC_SECRET

Los identificadores pueden ser el nombre o el ARN del secreto (por ejemplo, la
salida ``jwt_secret_arn`` y ``session_code_hmac_secret_arn`` de Terraform).

Comprueba la existencia de la etiqueta ``AWSCURRENT`` con ``DescribeSecret``
(solo metadatos); nunca invoca ``GetSecretValue``, por lo que no llega a leer el
valor de ningún secreto existente.

Requiere credenciales AWS con ``secretsmanager:DescribeSecret`` y
``secretsmanager:PutSecretValue`` sobre esos secretos. No crea, modifica ni
elimina otros recursos: el contenedor del secreto lo crea Terraform.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.aws_error_logging import aws_error_context  # noqa: E402


MIN_ENTROPY_BYTES = 32


class MissingSecretError(RuntimeError):
    """El contenedor del secreto todavía no existe en AWS."""


def _ensure_initial_version(client, *, secret_id: str, entropy_bytes: int) -> str:
    """Garantiza una versión inicial y devuelve un estado legible.

    Consulta solo metadatos (``DescribeSecret``) para saber si el secreto ya
    tiene una versión vigente (``AWSCURRENT``); no lee el valor con
    ``GetSecretValue``. No expone el valor del secreto en ningún caso.
    """

    try:
        described = client.describe_secret(SecretId=secret_id)
    except ClientError as error:
        if aws_error_context(error)["error_code"] != "ResourceNotFoundException":
            raise
        raise MissingSecretError(
            "el secreto no existe todavía; Terraform debe crear el contenedor "
            f"antes de inicializar su valor ({secret_id})"
        )

    stages = described.get("VersionIdsToStages") or {}
    if any("AWSCURRENT" in labels for labels in stages.values()):
        return "ya tenía valor (sin cambios)"

    client.put_secret_value(
        SecretId=secret_id,
        SecretString=secrets.token_urlsafe(entropy_bytes),
    )
    return "inicializado"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True, help="Región AWS del secreto.")
    parser.add_argument(
        "--jwt-secret-id",
        required=True,
        help="Nombre o ARN del secreto JWT_SECRET.",
    )
    parser.add_argument(
        "--hmac-secret-id",
        required=True,
        help="Nombre o ARN del secreto SESSION_CODE_HMAC_SECRET.",
    )
    parser.add_argument(
        "--entropy-bytes",
        type=int,
        default=MIN_ENTROPY_BYTES,
        help=f"Entropía mínima del valor generado (por defecto {MIN_ENTROPY_BYTES}).",
    )
    args = parser.parse_args()

    if args.entropy_bytes < MIN_ENTROPY_BYTES:
        parser.error(f"--entropy-bytes debe ser al menos {MIN_ENTROPY_BYTES}")

    client = boto3.client("secretsmanager", region_name=args.region)
    targets = {
        "JWT_SECRET": args.jwt_secret_id,
        "SESSION_CODE_HMAC_SECRET": args.hmac_secret_id,
    }

    exit_code = 0
    for label, secret_id in targets.items():
        try:
            status = _ensure_initial_version(
                client, secret_id=secret_id, entropy_bytes=args.entropy_bytes
            )
        except ClientError as error:
            context = aws_error_context(error)
            print(
                f"{label}: ERROR service={context['aws_service']} "
                f"operation={context['aws_operation']} "
                f"code={context['error_code']} request_id={context['request_id']}",
                file=sys.stderr,
            )
            exit_code = 1
            continue
        except MissingSecretError as error:
            print(f"{label}: ERROR {error}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"{label}: {status} ({secret_id})")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
