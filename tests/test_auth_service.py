import os
import unittest

os.environ.setdefault("JWT_SECRET", "test-secret-only-for-unit-tests-32chars")

import jwt

from core.exceptions import AuthenticationError, InvalidCredentials
from core.jwt_config import JWT_ALGORITHM
from services.auth_service import AuthService
from services.crypto_service import CryptoService


class FakeUserService:
    def __init__(self, user):
        self.user = user
        self.last_login_uid = None
        self.logged_activity = []

    def get_by_email(self, email):
        if self.user and self.user["email"] == email.lower():
            return self.user.copy()
        return None

    def update_last_login(self, uid):
        self.last_login_uid = uid

    def log_activity(self, actor_uid, action, target_uid=None, details=None):
        self.logged_activity.append(
            {
                "actor_uid": actor_uid,
                "action": action,
                "target_uid": target_uid,
                "details": details,
            }
        )

    def get_user(self, uid):
        user_uid = self.user.get("uid") if self.user else None
        user_id = self.user.get("id") if self.user else None
        if self.user and (
            user_uid == uid
            or user_id == uid
            or (user_uid is None and user_id is None)
        ):
            return self.user.copy()
        return None


class AuthServiceTests(unittest.TestCase):
    secret = "test-secret-only-for-unit-tests-32chars"

    def setUp(self):
        self.crypto = CryptoService()
        self.user = {
            "uid": "admin-1",
            "nombre": "Administrador",
            "email": "admin@emotionlab.test",
            "rol": "admin",
            "activo": True,
            "password_hash": self.crypto.hash_password("ClaveSegura123!"),
        }

    def _service(self, user=None):
        return AuthService(
            user_service=FakeUserService(self.user if user is None else user),
            crypto_service=self.crypto,
            jwt_secret=self.secret,
        )

    def test_login_returns_public_user_and_valid_jwt(self):
        service = self._service()

        result = service.login("ADMIN@emotionlab.test", "ClaveSegura123!")

        self.assertNotIn("password_hash", result["user"])
        self.assertEqual(service.user_service.last_login_uid, "admin-1")
        payload = jwt.decode(result["token"], self.secret, algorithms=[JWT_ALGORITHM])
        self.assertEqual(payload["sub"], "admin-1")
        self.assertEqual(payload["rol"], "admin")

    def test_login_rejects_invalid_credentials(self):
        with self.assertRaises(InvalidCredentials):
            self._service().login("admin@emotionlab.test", "incorrecta")

    def test_login_rejects_inactive_user(self):
        inactive_user = self.user | {"activo": False}
        with self.assertRaises(AuthenticationError):
            self._service(inactive_user).login("admin@emotionlab.test", "ClaveSegura123!")

    def test_verify_token_and_get_current_user(self):
        service = self._service()
        token = service.login("admin@emotionlab.test", "ClaveSegura123!")["token"]

        self.assertEqual(service.verify_token(token)["sub"], "admin-1")
        self.assertEqual(service.get_current_user(token)["uid"], "admin-1")

    def test_get_current_user_normalizes_legacy_id_field(self):
        legacy_user = self.user.copy()
        legacy_user.pop("uid")
        legacy_user["id"] = "legacy-admin-1"
        service = self._service(legacy_user)

        token = service.generate_token(legacy_user)

        self.assertEqual(service.get_current_user(token)["uid"], "legacy-admin-1")

    def test_get_current_user_injects_uid_when_document_has_no_uid_or_id(self):
        legacy_user = self.user.copy()
        legacy_user.pop("uid")
        service = self._service(legacy_user)

        token = service.generate_token({**legacy_user, "uid": "admin-1"})

        self.assertEqual(service.get_current_user(token)["uid"], "admin-1")
