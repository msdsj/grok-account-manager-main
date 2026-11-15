import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from grok_account_manager.api.routers import admin_auth
from grok_account_manager.core import database as db


def _request(method: str, path: str, *, headers=None, cookies: dict | None = None) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if cookies:
        raw_headers.append((b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": raw_headers,
            "query_string": b"",
        }
    )


def _json(response):
    if hasattr(response, "body"):
        return json.loads(response.body)
    return response


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "app.db"
        self._patchers = [
            patch.object(db, "DB_PATH", self._db_path),
            patch.object(db, "OUTPUT_DIR", Path(self._tmpdir.name) / "output"),
            patch.dict("os.environ", {"ADMIN_PASSWORD": "admin"}),
        ]
        for patcher in self._patchers:
            patcher.start()
        db.init_db()
        admin_auth.ensure_admin()

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def _login(self, password: str = "admin"):
        response = admin_auth.login(_request("POST", "/api/admin/v1/auth/login"), {"username": "admin", "password": password})
        return response

    def test_login_refresh_and_me(self) -> None:
        failed = self._login("wrong")
        self.assertEqual(failed.status_code, 401)

        response = self._login()
        self.assertEqual(response.status_code, 200)
        payload = _json(response)["data"]
        self.assertEqual(payload["admin"]["username"], "admin")
        token = payload["tokens"]["accessToken"]

        me = admin_auth.me(
            _request("GET", "/api/admin/v1/me", headers={"authorization": f"Bearer {token}"})
        )
        self.assertEqual(_json(me)["data"]["id"], "admin")

        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        refreshed = admin_auth.refresh(_request("POST", "/api/admin/v1/auth/refresh", cookies={"admin_refresh": cookie}))
        self.assertIn("accessToken", _json(refreshed)["data"])

        anonymous = admin_auth.me(_request("GET", "/api/admin/v1/me"))
        self.assertEqual(anonymous.status_code, 401)

    def test_change_password(self) -> None:
        token = _json(self._login())["data"]["tokens"]["accessToken"]
        request = _request("PUT", "/api/admin/v1/me/password", headers={"authorization": f"Bearer {token}"})
        changed = admin_auth.change_password(request, {"currentPassword": "admin", "newPassword": "secret123"})
        self.assertEqual(_json(changed)["data"]["updated"], True)

        self.assertEqual(self._login().status_code, 401)
        self.assertEqual(self._login("secret123").status_code, 200)


if __name__ == "__main__":
    unittest.main()
