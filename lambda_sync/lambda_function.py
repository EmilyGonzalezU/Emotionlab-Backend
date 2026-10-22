import json
import logging

from services.aws_service import AWSService
from services.dynamodb_service import DynamoDBService
from services.import_service import ImportService
from services.prediction_service import PredictionService
from services.session_service import SessionService


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("EmotionLab Sync iniciado")

    try:
        dynamodb = DynamoDBService()
        session_service = SessionService(
            dynamodb_service=dynamodb,
            legacy_data_service=None,
        )
        prediction_service = PredictionService()
        aws_service = AWSService()
        import_service = ImportService(
            session_service=session_service,
            prediction_service=prediction_service,
        )

        result = import_service.import_from_aws(aws_service)
        summary = {
            "ok": True,
            "total": result.get("total", 0),
            "nuevas": len(result.get("new", [])),
            "predichas": len(result.get("predicted", [])),
            "pendientes_identificacion": len(result.get("pending_identification", [])),
            "pendientes_prediccion": len(result.get("pending_prediction", [])),
            "errores": len(result.get("import_errors", [])),
            "omitidas_existentes": len(result.get("skipped_existing", [])),
            "reintentadas": len(result.get("retried_pending", [])),
        }
        logger.info("EmotionLab Sync finalizado: %s", json.dumps(summary, ensure_ascii=False))
        return {"statusCode": 200, "body": json.dumps(summary, ensure_ascii=False)}

    except Exception as error:
        logger.error("Error fatal en EmotionLab Sync (tipo=%s)", type(error).__name__)
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"ok": False, "error": "No se pudo completar la sincronización."},
                ensure_ascii=False,
            ),
        }
