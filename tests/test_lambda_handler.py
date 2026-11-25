from __future__ import annotations

import json

from lambda_handler import app, handler


def http_api_v2_event(path: str = "/health") -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "host": "example.execute-api.us-east-1.amazonaws.com",
        },
        "requestContext": {
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
            "stage": "$default",
        },
        "body": None,
        "isBase64Encoded": False,
    }


def test_lambda_handler_health_http_api_v2():
    response = handler(http_api_v2_event(), None)

    assert response["statusCode"] == 200
    assert response["isBase64Encoded"] is False
    assert response["headers"]["content-type"].startswith("application/json")

    body = json.loads(response["body"])
    assert body["status"] == "healthy"


def test_lambda_handler_matches_flask_test_client_for_health():
    with app.test_client() as client:
        flask_response = client.get("/health")

    lambda_response = handler(http_api_v2_event(), None)

    assert lambda_response["statusCode"] == flask_response.status_code
    assert json.loads(lambda_response["body"]) == flask_response.get_json()
    assert lambda_response["headers"]["content-type"].startswith(
        flask_response.content_type
    )
