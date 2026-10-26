"""HTTP-layer security tests: tenant isolation, role enforcement, stock invariant.

These tests exercise web_app.Handler directly (no socket), using small fake
request/response objects. They are the proof behind three claims:

  1. Shop A cannot read Shop B's data through the API.
  2. A staff session cannot reach owner-only routes.
  3. A rejected sale never changes available stock.
"""
import io
import json
import web_app
import main as inventory
import os
import tempfile
import unittest
from unittest import mock

# Resolve every web_app path (AUTH_FILE, TENANT_DIR, DB_FILE) inside a temp dir
# BEFORE importing web_app, because it reads these at import time.
_DATA_DIR = tempfile.mkdtemp(prefix="resourcehub-sec-")
os.environ["RESOURCEHUB_DATA_DIR"] = _DATA_DIR
os.environ["RESOURCEHUB_HOST"] = "127.0.0.1"

import web_app  # noqa: E402  (import order is deliberate)
import main as inventory  # noqa: E402


class FakeRequest:
    """Minimal stand-in for the fields Handler reads off a request."""

    def __init__(self, method="GET", path="/", body=None, headers=None, client_ip="127.0.0.1"):
        self.method = method
        self.path = path
        self.command = method
        self._body = json.dumps(body).encode("utf-8") if body is not None else b""
        self.rfile = io.BytesIO(self._body)
        self.headers = dict(headers or {})
        if body is not None:
            self.headers.setdefault("Content-Type", "application/json")
            self.headers.setdefault("Content-Length", str(len(self._body)))
        if method == "POST":
            self.headers.setdefault("Origin", "http://127.0.0.1")
            self.headers.setdefault("Host", "127.0.0.1")
        self.client_address = (client_ip, 0)


class FakeResponse:
    """Collects what Handler.send_response / send_header / wfile.write produced."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        self.headers[name.lower()] = value

    def end_headers(self):
        # Handler writes the body AFTER end_headers(), so don't snapshot here.
        pass

    def write(self, data):
        self.wfile.write(data)

    @property
    def body(self):
        return self.wfile.getvalue()

def call(handler, request):
    """Run one Handler method against fake request/response and return the response."""
    handler.headers = request.headers
    handler.rfile = request.rfile
    handler.client_address = request.client_address
    handler.requestline = f"{request.method} {request.path} HTTP/1.1"
    handler.command = request.method
    handler.path = request.path
    response = FakeResponse()
    handler.wfile = response.wfile
    handler.send_response = response.send_response
    handler.send_header = response.send_header
    handler.end_headers = response.end_headers
    if request.method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return response


def json_body(response):
    return json.loads(response.body.decode("utf-8"))


def cookie_from(response):
    """Pull the session cookie value out of a Set-Cookie header."""
    raw = response.headers.get("set-cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(web_app.SESSION_COOKIE + "="):
            return part.split("=", 1)[1]
    return ""


class SecurityTests(unittest.TestCase):
    def setUp(self):
        # Fresh temp dir per test, wired into BOTH modules before any DB use.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

        # web_app module globals
        web_app.AUTH_FILE = os.path.join(self.data_dir, "auth.json")
        web_app.TENANT_DIR = os.path.join(self.data_dir, "shops")
        web_app.ROOT_BACKUP_DIR = os.path.join(self.data_dir, "backups")
        web_app.users.clear()
        web_app.shops.clear()
        web_app.sessions.clear()
        web_app.email_challenges.clear()
        web_app.email_ip_sends.clear()
        web_app.LOGIN_ATTEMPTS.clear()
        web_app.INITIALIZED_SHOPS.clear()
        web_app.reset_challenges.clear()
        web_app.reset_ip_sends.clear()
        web_app.LOCAL_DEVELOPMENT = True
        web_app.TURNSTILE_SITE_KEY = "test-site"

        # inventory (main.py) module globals — this is what was missing.
        inventory.DATA_DIR = self.data_dir
        inventory.DB_FILE = os.path.join(self.data_dir, "campus_resources.db")
        inventory.BACKUP_DIR = os.path.join(self.data_dir, "backups")
        inventory.resources.clear()
        inventory.fellows.clear()
        inventory.borrow_records.clear()

        os.environ["TURNSTILE_SECRET_KEY"] = "test-secret"

        self._turnstile_patcher = mock.patch.object(web_app, "verify_turnstile", return_value=True)
        self._turnstile_patcher.start()
        self.addCleanup(self._turnstile_patcher.stop)

        self._smtp_patcher = mock.patch.object(
            web_app, "smtp_settings", return_value=("h", 25, "s", "u", "p")
        )
        self._smtp_patcher.start()
        self.addCleanup(self._smtp_patcher.stop)

        # Email is skipped: we inject the challenge code directly.
        self._smtp_patcher = mock.patch.object(
            web_app, "smtp_settings", return_value=("h", 25, "s", "u", "p")
        )
        self._smtp_patcher.start()
        self.addCleanup(self._smtp_patcher.stop)

    def _register(self, username, password, company):
        """Create a shop owner through Handler.handle_auth. Returns (cookie, shop_id)."""
        email = f"{username}@example.com"
        web_app.email_challenges[email] = {
            "code": "123456",
            "expires": web_app.time.time() + 600,
            "attempts": 0,
            "sent": web_app.time.time(),
        }
        handler = web_app.Handler.__new__(web_app.Handler)
        request = FakeRequest(
            method="POST",
            path="/api/register",
            body={
                "username": username,
                "password": password,
                "name": username.title(),
                "company_name": company,
                "products": "goods",
                "phone": "+2348000000000",
                "email": email,
                "email_code": "123456",
                "turnstile_token": "tok",
            },
        )
        response = call(handler, request)
        self.assertEqual(response.status, 200, response.body)
        cookie = cookie_from(response)
        self.assertTrue(cookie, "expected a session cookie from register")
        shop_id = web_app.users[username]["shop_id"]
        return cookie, shop_id

    def _login(self, username, password):
        handler = web_app.Handler.__new__(web_app.Handler)
        request = FakeRequest(
            method="POST",
            path="/api/login",
            body={"username": username, "password": password},
        )
        response = call(handler, request)
        self.assertEqual(response.status, 200, response.body)
        return cookie_from(response)

    def _get(self, path, cookie):
        handler = web_app.Handler.__new__(web_app.Handler)
        request = FakeRequest(
            method="GET",
            path=path,
            headers={"Cookie": f"{web_app.SESSION_COOKIE}={cookie}"},
        )
        return call(handler, request)

    def _post(self, path, body, cookie):
        handler = web_app.Handler.__new__(web_app.Handler)
        request = FakeRequest(
            method="POST",
            path=path,
            body=body,
            headers={"Cookie": f"{web_app.SESSION_COOKIE}={cookie}"},
        )
        return call(handler, request)

    # ---------- Test 1: tenant isolation ----------

    def test_shop_a_cannot_read_shop_b_asset(self):
        cookie_a, _ = self._register("alice", "AlicePass123", "Alice Shop")
        cookie_b, _ = self._register("bob", "BobPass12345", "Bob Shop")

        # Bob adds an item and creates an asset passport for it.
        r = self._post(
            "/api/resources",
            {"id": "B-001", "name": "Bob Laptop", "category": "Electronics", "total": 2},
            cookie_b,
        )
        self.assertEqual(r.status, 200, r.body)
        r = self._post("/api/assets", {"resource_id": "B-001", "serial_number": "SN-B"}, cookie_b)
        self.assertEqual(r.status, 201, r.body)
        bob_tag = json_body(r)["asset"]["asset_tag"]

        # Alice must not see Bob's asset or inventory.
        r = self._get(f"/api/asset?tag={bob_tag}", cookie_a)
        self.assertEqual(r.status, 404, r.body)
        r = self._get("/api/state", cookie_a)
        self.assertEqual(r.status, 200)
        body = json_body(r)
        self.assertEqual(body["resources"], [], "Alice saw Bob's inventory")

    # ---------- Test 2: role enforcement ----------

    def test_staff_cannot_reach_owner_routes(self):
        owner_cookie, _ = self._register("owner1", "OwnerPass123", "Owner Shop")

        # Create a staff account through the owner's session.
        r = self._post(
            "/api/staff",
            {"name": "Staff One", "username": "staff1", "password": "StaffPass123"},
            owner_cookie,
        )
        self.assertEqual(r.status, 201, r.body)

        staff_cookie = self._login("staff1", "StaffPass123")

        for path, body in (
            ("/api/settings", {"shop_name": "X", "brand_color": "#000000", "currency": "USD"}),
            ("/api/resources", {"id": "S-1", "name": "N", "category": "C", "total": 1}),
            ("/api/restock", {"resource_id": "X", "quantity": 1}),
            ("/api/stocktake", {"counts": [{"resource_id": "X", "expected": 0, "counted": 0}], "note": "n"}),
            ("/api/delete-resource", {"resource_id": "X"}),
        ):
            r = self._post(path, body, staff_cookie)
            self.assertEqual(r.status, 403, f"{path} should be 403 for staff, got {r.status}")

        for path in ("/api/audit", "/api/reports", "/api/backup"):
            r = self._get(path, staff_cookie)
            self.assertEqual(r.status, 403, f"{path} should be 403 for staff, got {r.status}")

    # ---------- Test 3: stock invariant ----------

    def test_rejected_sale_does_not_change_stock(self):
        cookie, _ = self._register("carol", "CarolPass123", "Carol Shop")

        r = self._post(
            "/api/resources",
            {"id": "C-001", "name": "Carol Widget", "category": "Widgets", "total": 5},
            cookie,
        )
        self.assertEqual(r.status, 200, r.body)

        r = self._post("/api/sale", {"resource_id": "C-001", "quantity": 999}, cookie)
        self.assertEqual(r.status, 400, r.body)

        r = self._get("/api/state", cookie)
        self.assertEqual(r.status, 200)
        resources = {item["id"]: item for item in json_body(r)["resources"]}
        self.assertEqual(resources["C-001"]["available"], 5)


if __name__ == "__main__":
    unittest.main()