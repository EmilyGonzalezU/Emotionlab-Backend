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
        logger.warning("Error de validación: %s", error)

        return {
            "statusCode": 400,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "ok": False,
                    "error": str(error)
                },
                ensure_ascii=False
            )
        }

    except Exception:
        logger.exception("Error interno en Lambda")

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
