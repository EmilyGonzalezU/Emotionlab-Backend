import os
import unittest

os.environ.setdefault("JWT_SECRET", "test-secret-only-for-unit-tests-32chars")

from app import create_app


class FakeAuthService:
    def login(self, email, password):
        if email == "admin@test" and password == "secret":
            return {"token": "valid-token", "user": {"uid": "u1", "rol": "admin"}}
        raise ValueError("Credenciales invalidas")

    def get_current_user(self, token):
        if token != "valid-token":
            raise ValueError("Token invalido")
        return {"uid": "u1", "rol": "admin"}


class FakeDashboardService:
    def get_date_overview(self, **kwargs):
        return {"today": {"fecha": "2026-08-01", "total": 3}, "dates": []}

    def get_recent_sessions(self, **kwargs):
        return []

    def get_sessions_by_date(self, **kwargs):
        return []


class ApiTests(unittest.TestCase):
    def setUp(self):
        services = {
            "auth_service": FakeAuthService(),
            "dashboard_service": FakeDashboardService(),
        }
        self.client = create_app(services).test_client()

    def test_login_and_protected_dashboard(self):
        login = self.client.post("/auth/login", json={"email": "admin@test", "password": "secret"})
        dashboard = self.client.get("/dashboard", headers={"Authorization": "Bearer valid-token"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.get_json()["today"]["total"], 3)

    def test_dashboard_requires_token(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 401)

    def test_health_is_public(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "healthy"})
