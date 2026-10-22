import json
import logging

from utils.predictor import Predictor

logger = logging.getLogger()
logger.setLevel(logging.INFO)

predictor = Predictor()


def lambda_handler(event, context):
    try:
        body = event.get("body", event)

        if isinstance(body, str):
            body = json.loads(body)

        resultado = predictor.predecir(body)

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "ok": True,
                    "resultado": resultado
                },
                ensure_ascii=False
            )
        }

    except ValueError as error:
        logger.warning("Solicitud de predicción inválida (tipo=%s)", type(error).__name__)

        return {
            "statusCode": 400,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "ok": False,
                    "error": "La solicitud de predicción no es válida."
                },
                ensure_ascii=False
            )
        }

    except Exception as error:
        logger.error("Error interno en Lambda (tipo=%s)", type(error).__name__)

        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "ok": False,
                    "error": "Error interno del servicio"
                },
                ensure_ascii=False
            )
        }
