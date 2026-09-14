from flask import Blueprint, current_app, jsonify
from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required
from datetime import datetime
from boto3.dynamodb.conditions import Key
from core.constants import DYNAMODB_TABLE_AUDIT_LOGS

audit_bp = Blueprint("audit", __name__, url_prefix="/audit-logs")

@audit_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN)
def get_logs():
    # Jugada maestra definitiva: Leemos hasta 2000 registros y los invertimos en memoria
    # sin usar comandos que el DynamoDBService personalizado rechace.
    dynamo = current_app.extensions["services"]["dynamodb_service"]
    
    año_actual = datetime.now().year
    mes_actual = datetime.now().month
    audit_month = f"{año_actual:04d}-{mes_actual:02d}"
    
    # Quitamos el ScanIndexForward que hacía explotar el servicio, pero mantenemos el límite gigante
    logs, _ = dynamo.query(
        DYNAMODB_TABLE_AUDIT_LOGS,
        Key("PK").eq(f"AUDIT#{audit_month}"),
        limit=2000
    )
    
    # Invertimos la lista a la antigua: los últimos (más nuevos) quedan de primeros
    logs_ordenados = logs[::-1] if logs else []
    
    return jsonify({"items": logs_ordenados}), 200