from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required

imports_bp = Blueprint("imports", __name__, url_prefix="/import")


@imports_bp.post("/sessions")
@jwt_required
@roles_required(ROLE_ADMIN)
def import_sessions():
    """Importa un lote directo enviando un payload JSON."""
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "El cuerpo debe contener un lote JSON"}), 400
    try:
        result = current_app.extensions["services"]["import_service"].import_batch(payload)
        return jsonify(result), 201
    except Exception as error:
        print(f"❌ Error al importar lote JSON: {error}")
        return jsonify({"error": "No se pudo procesar el lote JSON.", "detalles": str(error)}), 500


@imports_bp.post("/sync")
@jwt_required
@roles_required(ROLE_ADMIN)
def sync_aws_sessions():
    """Sincroniza sesiones desde la nube/AWS si está disponible o mantiene actualizado Firebase."""
    services = current_app.extensions["services"]
    aws_service = services.get("aws_service")
    import_service = services["import_service"]

    try:
        if aws_service is not None:
            result = import_service.import_from_aws(aws_service)
            return jsonify(
                {
                    "importadas": len(result.get("pending_identification", [])),
                    "pendientes_prediccion": len(result.get("pending_prediction", [])),
                    "errores": len(result.get("import_errors", [])),
                    "omitidas_existentes": len(result.get("skipped_existing", [])),
                    "total": result.get("total", 0),
                }
            ), 201

        import_service.session_service.build_import_index()
        return jsonify(
            {
                "importadas": 0,
                "pendientes_prediccion": 0,
                "errores": 0,
                "omitidas_existentes": 0,
                "total": 0,
                "mensaje": "Base de datos sincronizada con Firebase correctamente.",
            }
        ), 200

    except Exception as error:
        print(f"❌ Error en la sincronización de sesiones: {error}")
        return jsonify({"error": "No se pudieron importar las sesiones.", "detalles": str(error)}), 500