from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


sessions_bp = Blueprint(
    "sessions",
    __name__,
    url_prefix="/sessions"
)


@sessions_bp.get("")
@jwt_required
@roles_required(ROLE_TEACHER)
def list_sessions():

    service = current_app.extensions[
        "services"
    ]["session_service"]

    name = request.args.get(
        "name"
    )

    if name is not None:

        return jsonify([
            _session_summary(item)
            for item in service.search_by_name(
                name
            )
        ])

    fecha = (
        request.args.get(
            "date"
        )
        or request.args.get(
            "fecha"
        )
    )

    sessions = (
        service.list_sessions_by_date(
            fecha
        )
        if fecha
        else service.list_sessions(
            date_from=request.args.get(
                "date_from"
            ),

            date_to=request.args.get(
                "date_to"
            ),

            status=request.args.get(
                "status"
            ),
        )
    )

    return jsonify([
        _session_summary(
            session
        )
        for session in sessions
    ])


@sessions_bp.get(
    "/<session_id>"
)
@jwt_required
@roles_required(ROLE_TEACHER)
def get_session(
    session_id
):

    service = current_app.extensions[
        "services"
    ]["session_service"]

    session = service.get_session_detail(
        session_id
    )

    if not session:

        return jsonify({
            "error": "Sesion no encontrada"
        }), 404

    return jsonify({

        "session_id": session.get(
            "session_id"
        ),

        "nombre": session.get(
            "nombre"
        ),

        "observaciones": session.get(
            "observaciones"
        ),

        "estado": session.get(
            "estado",
            session.get(
                "status"
            )
        ),

        "status": session.get(
            "status"
        ),

        "prediction": session.get(
            "prediction"
        ),

        # ======================================================
        # FECHA REAL
        # ======================================================

        "fecha": (
            service._get_date(
                session
            )
        ),

        "hora": (
            service._get_time(
                session
            )
        ),

        "edad": session.get(
            "edad"
        ),

        "genero": session.get(
            "genero"
        ),

        "difficulty": session.get(
            "difficulty"
        ),

        "duracion_estimada": session.get(
            "duracion_estimada",
            False
        ),

        "prediction_error": session.get(
            "prediction_error"
        ),

        "prediction_missing_fields": session.get(
            "prediction_missing_fields",
            []
        ),

        "raw_data": session.get(
            "raw_json"
        ),
    })


@sessions_bp.patch(
    "/<session_id>"
)
@jwt_required
@roles_required(ROLE_TEACHER)
def update_session(
    session_id
):

    body = request.get_json(
        silent=True
    ) or {}

    current_app.extensions[
        "services"
    ]["session_service"].update_session(
        session_id,
        body
    )

    return jsonify({
        "session_id": session_id,
        "updated": True
    })


def _session_summary(
    session
):

    service = current_app.extensions[
        "services"
    ]["session_service"]

    prediction = (
        session.get(
            "prediction"
        )
        or {}
    )

    # ==========================================================
    # FECHA/HORA REAL
    # ==========================================================
    #
    # El servicio se encarga de buscar:
    #
    # fecha
    # fecha_inicio
    # raw_json.session_data.fechaInicio
    # raw_json.s3_key
    #
    # ==========================================================

    fecha = service._get_date(
        session
    )

    hora = service._get_time(
        session
    )

    return {

        "session_id": session.get(
            "session_id"
        ),

        "fecha": fecha,

        "hora": hora,

        "created_at": session.get(
            "created_at"
        ),

        "updated_at": session.get(
            "updated_at"
        ),

        "edad": session.get(
            "edad"
        ),

        "genero": session.get(
            "genero"
        ),

        "difficulty": session.get(
            "difficulty"
        ),

        "bienestar": prediction.get(
            "bienestar"
        ),

        "probabilidad": prediction.get(
            "probabilidad"
        ),

        "confianza": prediction.get(
            "confianza"
        ),

        "nombre": session.get(
            "nombre"
        ),

        "estado": session.get(
            "estado",
            session.get(
                "status"
            )
        ),

        "status": session.get(
            "status"
        ),

        "duracion_estimada": session.get(
            "duracion_estimada",
            False
        ),

        "prediction_error": session.get(
            "prediction_error"
        ),

        "prediction_missing_fields": session.get(
            "prediction_missing_fields",
            []
        ),
    }


@sessions_bp.post(
    "/import"
)
@jwt_required
@roles_required(ROLE_TEACHER)
def import_sessions():

    import_service = current_app.extensions[
        "services"
    ]["import_service"]

    aws_service = current_app.extensions[
        "services"
    ]["aws_service"]

    try:

        result = (
            import_service.import_from_aws(
                aws_service
            )
        )

        return jsonify(
            result
        ), 200

    except ValueError as error:

        return jsonify({
            "error": str(error)
        }), 400

    except Exception:

        current_app.logger.exception(
            "Error importando sesiones desde AWS"
        )

        return jsonify({
            "error": "No se pudieron importar las sesiones"
        }), 500


@sessions_bp.patch(
    "/<session_id>/name"
)
@jwt_required
@roles_required(ROLE_TEACHER)
def update_session_name(
    session_id
):

    body = request.get_json(
        silent=True
    ) or {}

    nombre = body.get(
        "nombre"
    )

    if nombre is None:

        return jsonify({
            "error": "El nombre es obligatorio"
        }), 400

    if not isinstance(
        nombre,
        str
    ):

        return jsonify({
            "error": "El nombre debe ser texto"
        }), 400

    nombre = nombre.strip()

    if not nombre:

        return jsonify({
            "error": "El nombre no puede estar vacío"
        }), 400

    try:

        session_service = current_app.extensions[
            "services"
        ]["session_service"]

        session_service.update_name(
            session_id,
            nombre
        )

        return jsonify({
            "session_id": session_id,
            "nombre": nombre,
            "updated": True
        }), 200

    except Exception as error:

        current_app.logger.exception(
            "Error actualizando nombre de sesión"
        )

        if (
            "no existe"
            in str(error).lower()
        ):

            return jsonify({
                "error": "Sesión no encontrada"
            }), 404

        return jsonify({
            "error": "No se pudo actualizar el nombre"
        }), 500