from app import create_app


def _client(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://allowed.example")
    app = create_app(services={"storage_backend": "test"})
    app.config["TESTING"] = True
    return app.test_client()


def test_cors_allows_only_configured_origin_without_credentials(monkeypatch):
    response = _client(monkeypatch).get(
        "/health", headers={"Origin": "https://allowed.example"}
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://allowed.example"
    assert response.headers.get("Access-Control-Allow-Credentials") != "true"


def test_cors_does_not_reflect_unconfigured_origin(monkeypatch):
    response = _client(monkeypatch).get(
        "/health", headers={"Origin": "https://unapproved.invalid"}
    )
    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") is None
