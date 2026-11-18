import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aliyun_balance import server as server_module
from aliyun_balance.server import BalanceApp, make_server
from aliyun_balance.store import AccountStore


def fake_fetcher(credentials):
    out = []
    for item in credentials:
        out.append({
            "id": item["id"],
            "name": item["name"],
            "akMasked": "LTAI****TEST",
            "currency": "CNY",
            "ok": True,
            "partial": False,
            "balance": {"available": "10.00", "cash": "10.00", "credit": "0.00",
                        "mybankCredit": None, "quotaLimit": None, "currency": "CNY"},
            "coupons": {"availableBalance": "5.00", "availableCount": 1, "totalCount": 1,
                        "earliestExpiry": None, "earliestExpiryTs": None,
                        "expiringSoonCount": 0, "expiringSoonBalance": "0.00", "items": []},
            "total": "15.00",
            "errors": [],
            "latencyMs": 1,
            "fetchedAt": 1700000000000,
        })
    return out


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        self.app = BalanceApp(self.store, fetcher=fake_fetcher)
        self.server = make_server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, csrf=True, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if csrf and method in ("POST", "PUT", "DELETE"):
            request.add_header("X-Balance-Board", "1")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def add_account(self, **overrides):
        payload = {"name": "测试号", "ak": "LTAI5tTest12345678", "sk": "Secret1234567890abcdef"}
        payload.update(overrides)
        return self.request("/api/accounts", "POST", {"accounts": [payload]})

    def test_health_and_static(self):
        status, data = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], "aliyun-balance-board")
        self.assertIn(data["protection"], ("dpapi", "base64"))

        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("阿里云余额批量看板", html)
        self.assertIn("Content-Security-Policy", response.headers)

    def test_add_list_hides_secret(self):
        status, data = self.add_account()
        self.assertEqual(status, 201)
        self.assertEqual(data["added"], 1)

        status, data = self.request("/api/accounts")
        self.assertEqual(status, 200)
        account = data["accounts"][0]
        self.assertTrue(account["hasSk"])
        self.assertNotIn("sk", account)
        self.assertNotIn("Secret1234567890abcdef", json.dumps(data))

    def test_duplicate_updates(self):
        self.add_account()
        status, data = self.add_account(name="改名")
        self.assertEqual(status, 201)
        self.assertEqual(data["updated"], 1)
        self.assertEqual(data["accounts"][0]["name"], "改名")

    def test_update_and_delete(self):
        _, data = self.add_account()
        account_id = data["accounts"][0]["id"]

        status, data = self.request(f"/api/accounts/{account_id}", "PUT", {"name": "新名字", "enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(data["account"]["name"], "新名字")
        self.assertFalse(data["account"]["enabled"])

        status, data = self.request(f"/api/accounts/{account_id}", "DELETE")
        self.assertEqual(status, 200)
        _, data = self.request("/api/accounts")
        self.assertEqual(data["accounts"], [])

    def test_query_and_aggregate(self):
        self.add_account()
        status, data = self.request("/api/query", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["total"], "15.00")
        self.assertEqual(data["aggregate"]["byCurrency"][0]["total"], "15.00")

    def test_query_no_accounts(self):
        status, data = self.request("/api/query", "POST", {})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_csrf_header_required(self):
        status, data = self.request("/api/accounts", "POST", {"accounts": []}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "Forbidden")

    def test_host_header_rejected(self):
        # urllib 会强制用 URL 的 host 覆盖 Host 头，这里直接走 http.client
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("GET", "/api/health", skip_host=True)
        conn.putheader("Host", "evil.example.com")
        conn.endheaders()
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(body["error"]["code"], "ForbiddenHost")

    def test_test_endpoint_returns_detail(self):
        fake_result = {
            "ok": False,
            "currency": "CNY",
            "errors": [{"scope": "balance", "code": "InvalidAccessKeyId.NotFound", "message": "not found"}],
            "balance": None, "coupons": None, "total": None,
        }
        with mock.patch.object(server_module.bss, "fetch_account", return_value=fake_result) as patched:
            status, data = self.request("/api/test", "POST", {"ak": "LTAI5tFake", "sk": "Secret1234567890"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["errors"][0]["code"], "InvalidAccessKeyId.NotFound")
        patched.assert_called_once()

    def test_invalid_endpoint_reported_per_row(self):
        status, data = self.add_account(endpoint="https://evil.example.com")
        self.assertEqual(status, 201)
        self.assertEqual(data["added"], 0)
        self.assertEqual(len(data["errors"]), 1)

    def test_clear(self):
        self.add_account()
        status, data = self.request("/api/accounts/clear", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 1)


if __name__ == "__main__":
    unittest.main()
