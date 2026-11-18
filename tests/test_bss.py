import datetime
import unittest

from aliyun_balance import bss


class SignatureTest(unittest.TestCase):
    def _cr_hash(self, action, nonce, when="2026-10-02T18:43:14Z"):
        ms = int(datetime.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() * 1000)
        request = bss.build_signed_request(
            "AKIDEXAMPLE", "SECRETEXAMPLE", action, {}, now_ms=ms, nonce=nonce
        )
        headers = {k: v for k, v in request["headers"].items() if k != "authorization"}
        return bss.canonical_request_hash(
            "POST", "/", request["query"], headers, headers["x-acs-content-sha256"]
        )

    def test_golden_canonical_request_hashes(self):
        # 这两个哈希来自阿里云服务端实际返回的 CanonicalRequest（2026-10-03 抓取）。
        self.assertEqual(
            self._cr_hash("QueryCashCoupons", "5f43cbdada74105f329391f0de02e0d0"),
            "05697a12923d5070b61e2145f59509bc01693d7eed967f51f3e10809bbf3d846",
        )
        self.assertEqual(
            self._cr_hash("QueryAccountBalance", "ed5b9e1d797dda0041263873b5accbc7"),
            "1b12f48fe6dbe3a7b4ad2fafa881c34d53438812a506ce0dc0cb9327257fdca1",
        )

    def test_authorization_shape(self):
        request = bss.build_signed_request("AKIDEXAMPLE", "SECRETEXAMPLE", "QueryAccountBalance", {})
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://business.aliyuncs.com/")
        self.assertEqual(request["headers"]["x-acs-version"], "2017-12-14")
        self.assertEqual(request["headers"]["x-acs-content-sha256"], bss.EMPTY_SHA256)
        auth = request["headers"]["authorization"]
        self.assertRegex(
            auth,
            r"^ACS3-HMAC-SHA256 Credential=AKIDEXAMPLE,SignedHeaders=[a-z0-9;-]+,Signature=[0-9a-f]{64}$",
        )
        self.assertIn("host", auth.split("SignedHeaders=")[1].split(",")[0].split(";"))

    def test_query_encoding_and_order(self):
        request = bss.build_signed_request(
            "AK", "SK", "QueryAccountBalance", {"BillingCycle": "2026-09", "EffectiveOrNot": True}
        )
        self.assertEqual(request["query"], "BillingCycle=2026-09&EffectiveOrNot=true")
        request = bss.build_signed_request("AK", "SK", "QueryAccountBalance", {"Name": "中文 值"})
        self.assertEqual(request["query"], "Name=%E4%B8%AD%E6%96%87%20%E5%80%BC")

    def test_endpoint_allowlist(self):
        self.assertEqual(bss.validate_endpoint("business.ap-southeast-1.aliyuncs.com"),
                         "business.ap-southeast-1.aliyuncs.com")
        for bad in ("evil.com", "business.aliyuncs.com.evil.com", "http://business.aliyuncs.com", ""):
            if bad == "":
                self.assertEqual(bss.validate_endpoint(bad), bss.DEFAULT_ENDPOINT)
            else:
                with self.assertRaises(bss.BssError):
                    bss.validate_endpoint(bad)


class NormalizeTest(unittest.TestCase):
    def test_balance(self):
        payload = {"Data": {
            "AvailableAmount": "12.4",
            "AvailableCashAmount": 10,
            "CreditAmount": "2.40",
            "MybankCreditAmount": None,
            "Currency": "cny",
        }}
        res = bss.normalize_balance(payload)
        self.assertEqual(res["available"], "12.40")
        self.assertEqual(res["cash"], "10.00")
        self.assertEqual(res["credit"], "2.40")
        self.assertIsNone(res["mybankCredit"])
        self.assertEqual(res["currency"], "CNY")

    def test_coupons_sort_sum_and_expiry(self):
        now = int(datetime.datetime(2026, 10, 30, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        payload = {"Data": {"CashCoupon": [
            {"CashCouponId": 2, "CashCouponNo": "B", "NominalValue": "50", "Balance": "20.00",
             "Status": "Used", "ExpiryTime": "2026-11-01T00:00:00Z"},
            {"CashCouponId": 1, "CashCouponNo": "A", "NominalValue": "300", "Balance": "158.50",
             "Status": "Available", "ExpiryTime": "2026-11-17T23:59:59Z", "Description": "学生券"},
            {"CashCouponId": 3, "CashCouponNo": "C", "NominalValue": "10", "Balance": "5.00",
             "Status": "Available", "ExpiryTime": "2026-12-30T00:00:00Z"},
        ]}}
        res = bss.normalize_coupons(payload, now_ms=now)
        self.assertEqual(res["availableBalance"], "163.50")
        self.assertEqual(res["availableCount"], 2)
        self.assertEqual(res["totalCount"], 3)
        self.assertEqual([item["no"] for item in res["items"]], ["A", "C", "B"])
        self.assertEqual(res["earliestExpiry"], "2026-11-17T23:59:59Z")
        self.assertEqual(res["expiringSoonCount"], 1)
        self.assertEqual(res["expiringSoonBalance"], "158.50")

    def test_coupons_empty_and_malformed(self):
        self.assertEqual(bss.normalize_coupons({})["items"], [])
        res = bss.normalize_coupons({"Data": []})
        self.assertEqual(res["availableBalance"], "0.00")

    def test_time_parse(self):
        self.assertIsNotNone(bss.parse_time_ms("2026-11-30T23:59:59Z"))
        self.assertIsNotNone(bss.parse_time_ms("2026-11-30 23:59:59"))
        self.assertIsNone(bss.parse_time_ms("not-a-date"))


class AggregateTest(unittest.TestCase):
    def _result(self, account_id, total, available, coupons, expiry=None, ok=True):
        now = bss.now_ms()
        soon = bool(expiry and now <= expiry <= now + 30 * 86400000 and coupons != "0.00")
        return {
            "id": account_id,
            "name": account_id,
            "currency": "CNY",
            "ok": ok,
            "balance": {"available": available, "cash": available, "credit": "0.00", "currency": "CNY"} if ok else None,
            "coupons": {
                "availableBalance": coupons,
                "availableCount": 1 if coupons != "0.00" else 0,
                "earliestExpiryTs": expiry,
                "expiringSoonCount": 1 if soon else 0,
                "expiringSoonBalance": coupons if soon else "0.00",
            } if ok else None,
            "total": total if ok else None,
            "errors": [] if ok else [{"scope": "balance", "code": "NoPermission", "message": ""}],
        }

    def test_totals_and_nearest(self):
        now = bss.now_ms()
        results = [
            self._result("a", "170.90", "12.40", "158.50", now + 18 * 86400000),
            self._result("b", "150.50", "88.00", "62.50", now + 46 * 86400000),
            self._result("c", None, None, None, ok=False),
        ]
        agg = bss.aggregate(results)
        self.assertEqual(agg["accounts"], 3)
        self.assertEqual(agg["ok"], 2)
        self.assertEqual(agg["failed"], 1)
        self.assertEqual(agg["couponCount"], 2)
        self.assertEqual(agg["expiringSoonCount"], 1)
        self.assertEqual(agg["nearestExpiry"]["accountId"], "a")
        group = agg["byCurrency"][0]
        self.assertEqual(group["total"], "321.40")
        self.assertEqual(group["available"], "100.40")
        self.assertEqual(group["coupons"], "221.00")
        self.assertEqual(group["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
