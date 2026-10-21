"""Create the first EmotionLab administrator after a fresh AWS deployment.

Run only against a newly deployed, empty environment using a trusted operator
AWS profile. This never accepts or stores a password; Cognito sends its normal
temporary-password invitation.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


def _has_admin(table) -> bool:
    scan_args = {"FilterExpression": Attr("rol").eq("admin"), "ProjectionExpression": "uid"}
    while True:
        response = table.scan(**scan_args)
        if response.get("Items"):
            return True
        key = response.get("LastEvaluatedKey")
        if not key:
            return False
        scan_args["ExclusiveStartKey"] = key


def bootstrap(*, region: str, user_pool_id: str, users_table_name: str, email: str, name: str) -> str:
    idp = boto3.client("cognito-idp", region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(users_table_name)

    if _has_admin(table):
        raise RuntimeError("Ya existe un administrador; no se creó otra cuenta bootstrap.")

    normalized_email = email.strip().lower()
    response = idp.admin_create_user(
        UserPoolId=user_pool_id,
        Username=normalized_email,
        UserAttributes=[
            {"Name": "email", "Value": normalized_email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "name", "Value": name.strip()},
        ],
        DesiredDeliveryMediums=["EMAIL"],
    )
    user = response.get("User", {})
    attributes = {item["Name"]: item["Value"] for item in user.get("Attributes", [])}
    uid = attributes.get("sub")
    username = user.get("Username")
    if not uid or not username:
        try:
            idp.admin_delete_user(UserPoolId=user_pool_id, Username=username or normalized_email)
        finally:
            raise RuntimeError("Cognito no devolvió los identificadores requeridos; se revirtió la cuenta.")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {
        "uid": uid,
        "nombre": name.strip(),
        "email": normalized_email,
        "rol": "admin",
        "activo": True,
        "cognito_username": username,
        "user_status": user.get("UserStatus", "FORCE_CHANGE_PASSWORD"),
        "legacy_uid": None,
        "created_at": now,
        "updated_at": now,
        "pk_type": "USER",
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(uid)")
    except Exception:
        try:
            idp.admin_delete_user(UserPoolId=user_pool_id, Username=username)
        except Exception:
            pass
        raise
    return uid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--users-table", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not args.email.strip() or not args.name.strip():
        parser.error("--email y --name no pueden estar vacíos")
    try:
        bootstrap(
            region=args.region,
            user_pool_id=args.user_pool_id,
            users_table_name=args.users_table,
            email=args.email,
            name=args.name,
        )
    except (ClientError, RuntimeError) as error:
        print(f"No se pudo crear el administrador inicial: {error}", file=sys.stderr)
        return 1
    print("Administrador inicial creado; Cognito enviará la invitación temporal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
