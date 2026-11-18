"""阿里云 BSS OpenAPI 只读客户端。

只做两件事：
  * QueryAccountBalance —— 账户可用余额（现金 / 信控）
  * QueryCashCoupons   —— 现金代金券列表（面值 / 余额 / 状态 / 失效时间）

签名使用阿里云 ACS3-HMAC-SHA256（v3）。所有金额在服务端以 Decimal 精确到分，
输出为两位小数字符串，避免浮点误差。

本模块不读取、不缓存任何凭据；AK/SK 由调用方按次传入。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

LOG = logging.getLogger("aliyun_balance.bss")

BSS_VERSION = "2017-12-14"
DEFAULT_ENDPOINT = "business.aliyuncs.com"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CENT = Decimal("0.01")
EXPIRING_SOON_DAYS = 30
USER_AGENT = "AliyunBalanceBoard/1.0 (+https://github.com/wai549077/dsh-aliyun-balance)"

_ENDPOINT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.aliyuncs\.com$")
_AVAILABLE_STATUS = {"available", "availabletouse", "usable", "unused", "valid"}


class BssError(Exception):
    """参数或配置层面的错误（不发起网络请求）。"""


# ---------------------------------------------------------------------------
# 金额 / 时间
# ---------------------------------------------------------------------------

def to_money(value) -> Decimal:
    """任意输入 → 精确到分的 Decimal；非法值按 0。"""
    if value is None or value == "":
        return Decimal("0.00")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if not d.is_finite():
        return Decimal("0.00")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value) -> str:
    return f"{to_money(value):.2f}"


def money_or_null(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return f"{d.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def parse_time_ms(value):
    """阿里云时间字符串 → epoch 毫秒；无法解析返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_ak(ak: str) -> str:
    ak = str(ak or "")
    if len(ak) <= 8:
        return ak[:2] + "****" if len(ak) > 3 else "****"
    return f"{ak[:4]}****{ak[-4:]}"


# ---------------------------------------------------------------------------
# ACS3 签名
# ---------------------------------------------------------------------------

def _rfc3986(value) -> str:
    # quote() 默认不会编码 ~，且会把空格编码为 %20；再补上 Java encodeURIComponent
    # 之外阿里云要求编码的字符（!'()*）。
    return urllib.parse.quote(str(value), safe="-_.~")


def canonical_query(params: dict | None) -> str:
    if not params:
        return ""
    pairs = []
    for key, value in params.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        pairs.append(f"{_rfc3986(key)}={_rfc3986(value)}")
    pairs.sort()
    return "&".join(pairs)


def canonical_request_hash(method: str, uri: str, query: str, headers: dict, body_sha256: str) -> str:
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_headers = "".join(f"{name}:{str(headers[name]).strip()}\n" for name in names)
    # 注意：canonicalHeaders 自身以 \n 结尾，join 后再补一个 \n —— 即头块与
    # SignedHeaders 之间是一个空行。这个换行结构已用阿里云服务端返回的
    # CanonicalRequest 哈希验证（见 tests/test_bss.py 的黄金用例）。
    canonical = "\n".join([method, uri, query, canonical_headers, signed_headers, body_sha256])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_signed_request(
    ak: str,
    sk: str,
    action: str,
    params: dict | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    now_ms: int | None = None,
    nonce: str | None = None,
    param_location: str = "query",
    method: str = "POST",
    uri: str = "/",
) -> dict:
    """构造已签名的请求（不发起网络调用）。"""
    if not ak or not sk:
        raise BssError("缺少 AccessKey ID 或 Secret")
    validate_endpoint(endpoint)

    params = dict(params or {})
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if nonce is None:
        nonce = secrets.token_hex(16)

    query = canonical_query(params) if param_location == "query" else ""
    body = canonical_query(params) if param_location == "body" else ""
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else EMPTY_SHA256

    headers = {
        "host": endpoint,
        "x-acs-action": action,
        "x-acs-version": BSS_VERSION,
        "x-acs-date": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-acs-signature-nonce": str(nonce),
        "x-acs-content-sha256": body_sha256,
    }
    if body:
        headers["content-type"] = "application/x-www-form-urlencoded"

    cr_hash = canonical_request_hash(method, uri, query, headers, body_sha256)
    string_to_sign = f"ACS3-HMAC-SHA256\n{cr_hash}"
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    signed_headers = ";".join(sorted(headers))

    return {
        "method": method,
        "url": f"https://{endpoint}{uri}",
        "query": query,
        "body": body,
        "headers": {
            **headers,
            "authorization": (
                f"ACS3-HMAC-SHA256 Credential={ak},"
                f"SignedHeaders={signed_headers},Signature={signature}"
            ),
        },
    }


def validate_endpoint(endpoint: str) -> str:
    endpoint = str(endpoint or "").strip().lower()
    if not endpoint:
        return DEFAULT_ENDPOINT
    if not _ENDPOINT_RE.match(endpoint):
        raise BssError("Endpoint 必须是 *.aliyuncs.com 域名（例如 business.aliyuncs.com）")
    return endpoint


# ---------------------------------------------------------------------------
# HTTP 调用
# ---------------------------------------------------------------------------

def call_bss(
    ak: str,
    sk: str,
    action: str,
    params: dict | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 15.0,
    retries: int = 1,
) -> dict:
    """调用 BSS OpenAPI，返回 {ok, data|code, message, status}。"""
    endpoint = validate_endpoint(endpoint)
    last = {"ok": False, "code": "NetworkError", "message": "请求未发出"}
    for attempt in range(retries + 1):
        signed = build_signed_request(ak, sk, action, params, endpoint)
        url = signed["url"] + ("?" + signed["query"] if signed["query"] else "")
        headers = {k: v for k, v in signed["headers"].items() if k != "host"}
        headers["user-agent"] = USER_AGENT
        headers["accept"] = "application/json"
        data = signed["body"].encode("utf-8") if signed["body"] else None

        started = time.perf_counter()
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=signed["method"])
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except Exception:  # pragma: no cover - 极少见
                raw = b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            message = _short_error(e)
            last = {"ok": False, "code": "NetworkError", "message": message}
            if attempt < retries:
                time.sleep(0.6)
                continue
            return last

        elapsed = int((time.perf_counter() - started) * 1000)
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            payload = None

        if payload is not None and payload.get("Success") is not True:
            code = str(payload.get("Code") or f"HTTP {status}")
            message = str(payload.get("Message") or "")
            last = {
                "ok": False,
                "code": code,
                "message": message,
                "status": status,
                "request_id": payload.get("RequestId"),
                "latency_ms": elapsed,
            }
        elif status >= 400:
            last = {
                "ok": False,
                "code": f"HTTP {status}",
                "message": (raw[:300].decode("utf-8", "replace") if raw else ""),
                "status": status,
                "latency_ms": elapsed,
            }
        elif payload is None:
            last = {
                "ok": False,
                "code": "BadResponse",
                "message": "阿里云返回了无法解析的响应",
                "status": status,
                "latency_ms": elapsed,
            }
        else:
            return {"ok": True, "data": payload, "status": status, "latency_ms": elapsed}

        if attempt < retries and _retryable(last):
            time.sleep(0.6)
            continue
        return last
    return last


def _retryable(result: dict) -> bool:
    code = str(result.get("code") or "")
    status = result.get("status") or 0
    return (
        code in {"Throttling", "Throttling.User", "ServiceUnavailable"}
        or status >= 500
        or status == 429
    )


def _short_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and getattr(exc, "reason", None):
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# 响应归一化
# ---------------------------------------------------------------------------

def normalize_balance(payload: dict) -> dict:
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    return {
        "available": money_str(data.get("AvailableAmount")),
        "cash": money_str(data.get("AvailableCashAmount")),
        "credit": money_str(data.get("CreditAmount")),
        "mybankCredit": money_or_null(
            data.get("MybankCreditAmount", data.get("MyBankCreditAmount", data.get("MybankCredit")))
        ),
        "quotaLimit": money_or_null(data.get("QuotaLimit", data.get("AvailableQuotaLimit"))),
        "currency": str(data.get("Currency") or "CNY").upper(),
    }


def _coupon_items(payload: dict) -> list:
    if not isinstance(payload, dict):
        return []
    data = payload.get("Data")
    candidates = [
        data,
        data.get("CashCoupon") if isinstance(data, dict) else None,
        payload.get("CashCoupon"),
        data.get("CashCouponList") if isinstance(data, dict) else None,
        data.get("CashCoupons") if isinstance(data, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict) and ("CashCouponId" in candidate or "CashCouponNo" in candidate):
            # 单张券的形态：包一层
            return [candidate]
    return []


def _is_available(status) -> bool:
    return str(status or "").strip().lower() in _AVAILABLE_STATUS


def normalize_coupons(payload: dict, now_ms: int | None = None) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    items = []
    for raw in _coupon_items(payload):
        if not isinstance(raw, dict):
            continue
        expiry_ms = parse_time_ms(raw.get("ExpiryTime"))
        items.append(
            {
                "id": "" if raw.get("CashCouponId") is None else str(raw.get("CashCouponId")),
                "no": str(raw.get("CashCouponNo") or ""),
                "nominalValue": money_str(raw.get("NominalValue")),
                "balance": money_str(raw.get("Balance")),
                "status": str(raw.get("Status") or ""),
                "effectiveTime": raw.get("EffectiveTime") or None,
                "expiryTime": raw.get("ExpiryTime") or None,
                "expiryTs": expiry_ms,
                "grantedTime": raw.get("GrantedTime") or None,
                "applicableProducts": raw.get("ApplicableProducts") or "通用",
                "applicableScenarios": raw.get("ApplicableScenarios") or "",
                "description": raw.get("Description") or "",
                "available": _is_available(raw.get("Status")),
            }
        )

    items.sort(
        key=lambda it: (
            0 if it["available"] else 1,
            it["expiryTs"] if it["expiryTs"] is not None else float("inf"),
            it["no"],
        )
    )

    available = [it for it in items if it["available"]]
    available_balance = sum((to_money(it["balance"]) for it in available), Decimal("0.00"))
    expiries = [it["expiryTs"] for it in available if it["expiryTs"] is not None]
    earliest = min(expiries) if expiries else None

    soon_limit = now_ms + EXPIRING_SOON_DAYS * 86400 * 1000
    soon = [
        it
        for it in available
        if it["expiryTs"] is not None and now_ms <= it["expiryTs"] <= soon_limit
    ]
    soon_balance = sum((to_money(it["balance"]) for it in soon), Decimal("0.00"))

    return {
        "availableBalance": f"{available_balance:.2f}",
        "availableCount": len(available),
        "totalCount": len(items),
        "earliestExpiry": iso_utc(earliest),
        "earliestExpiryTs": earliest,
        "expiringSoonCount": len(soon),
        "expiringSoonBalance": f"{soon_balance:.2f}",
        "items": items,
    }


# ---------------------------------------------------------------------------
# 单账户查询
# ---------------------------------------------------------------------------

def _coupons_with_fallback(ak: str, sk: str, endpoint: str, timeout: float) -> dict:
    """QueryCashCoupons；空列表时按 EffectiveOrNot true/false 再各查一次。"""
    result = call_bss(ak, sk, "QueryCashCoupons", {}, endpoint, timeout)
    if result.get("ok"):
        items = normalize_coupons(result["data"])["items"]
        if not items:
            merged = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        call_bss, ak, sk, "QueryCashCoupons", {"EffectiveOrNot": flag}, endpoint, timeout
                    )
                    for flag in ("true", "false")
                ]
                for future in futures:
                    try:
                        part = future.result()
                    except Exception:  # pragma: no cover - call_bss 不抛异常
                        continue
                    if part.get("ok"):
                        merged.extend(_coupon_items(part["data"]))
            if merged:
                result = {"ok": True, "data": {"Data": merged}}
    return result


def fetch_account(account: dict, timeout: float = 15.0) -> dict:
    """查询一个账户的余额 + 代金券，返回可直接给前端的结构。"""
    started = time.perf_counter()
    ak = account.get("ak", "")
    sk = account.get("sk", "")
    endpoint = account.get("endpoint") or DEFAULT_ENDPOINT

    balance_res = {}
    coupons_res = {}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fb = pool.submit(call_bss, ak, sk, "QueryAccountBalance", {}, endpoint, timeout)
            fc = pool.submit(_coupons_with_fallback, ak, sk, endpoint, timeout)
            balance_res = fb.result()
            coupons_res = fc.result()
    except Exception as exc:  # pragma: no cover - 防御
        LOG.warning("query crashed: %s", exc.__class__.__name__)
        balance_res = balance_res or {"ok": False, "code": "InternalError", "message": str(exc)}
        coupons_res = coupons_res or {"ok": False, "code": "InternalError", "message": str(exc)}

    errors = []
    balance = None
    if balance_res.get("ok"):
        balance = normalize_balance(balance_res["data"])
    else:
        errors.append(
            {
                "scope": "balance",
                "code": balance_res.get("code") or "Error",
                "message": balance_res.get("message") or "",
            }
        )

    coupons = None
    if coupons_res.get("ok"):
        coupons = normalize_coupons(coupons_res["data"])
    else:
        errors.append(
            {
                "scope": "coupons",
                "code": coupons_res.get("code") or "Error",
                "message": coupons_res.get("message") or "",
            }
        )

    currency = (balance or {}).get("currency") or "CNY"
    total = None
    if balance is not None:
        total_d = to_money(balance["available"]) + to_money((coupons or {}).get("availableBalance"))
        total = f"{total_d:.2f}"

    return {
        "id": account.get("id"),
        "name": account.get("name") or "",
        "akMasked": mask_ak(ak),
        "currency": currency,
        "ok": balance is not None,
        "partial": balance is not None and coupons is None,
        "balance": balance,
        "coupons": coupons,
        "total": total,
        "errors": errors,
        "latencyMs": int((time.perf_counter() - started) * 1000),
        "fetchedAt": int(time.time() * 1000),
    }


def fetch_accounts(accounts: list[dict], timeout: float = 15.0, max_workers: int = 6) -> list[dict]:
    if not accounts:
        return []
    workers = max(1, min(max_workers, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_account, acc, timeout) for acc in accounts]
        return [f.result() for f in futures]


def test_credentials(ak: str, sk: str, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 15.0) -> dict:
    return fetch_account(
        {"id": "test", "name": "连接测试", "ak": ak, "sk": sk, "endpoint": endpoint},
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def aggregate(results: list[dict]) -> dict:
    now_ms = int(time.time() * 1000)
    by_currency: dict[str, dict] = {}
    ok = 0
    partial = 0
    failed = 0
    nearest = None
    coupon_total_count = 0
    expiring_soon_count = 0
    expiring_soon_balance = Decimal("0.00")

    for res in results:
        currency = res.get("currency") or "CNY"
        bucket = by_currency.setdefault(
            currency,
            {
                "currency": currency,
                "accounts": 0,
                "available": Decimal("0.00"),
                "coupons": Decimal("0.00"),
                "total": Decimal("0.00"),
                "incomplete": 0,
            },
        )
        bucket["accounts"] += 1

        if res.get("balance"):
            ok += 1
            bucket["available"] += to_money(res["balance"].get("available"))
        else:
            failed += 1
            bucket["incomplete"] += 1
        if res.get("partial"):
            partial += 1

        coupons = res.get("coupons")
        if coupons:
            bucket["coupons"] += to_money(coupons.get("availableBalance"))
            coupon_total_count += int(coupons.get("availableCount") or 0)
            expiring_soon_count += int(coupons.get("expiringSoonCount") or 0)
            expiring_soon_balance += to_money(coupons.get("expiringSoonBalance"))
            exp = coupons.get("earliestExpiryTs")
            if exp is not None and exp >= now_ms and (nearest is None or exp < nearest["expiryTs"]):
                nearest = {
                    "accountId": res.get("id"),
                    "accountName": res.get("name"),
                    "expiryTs": exp,
                    "amount": coupons.get("availableBalance"),
                }

        if res.get("total") is not None:
            bucket["total"] += to_money(res["total"])

    groups = []
    for bucket in by_currency.values():
        groups.append(
            {
                "currency": bucket["currency"],
                "accounts": bucket["accounts"],
                "available": f"{bucket['available']:.2f}",
                "coupons": f"{bucket['coupons']:.2f}",
                "total": f"{bucket['total']:.2f}",
                "incomplete": bucket["incomplete"],
            }
        )
    groups.sort(key=lambda g: (g["currency"] != "CNY", g["currency"]))

    return {
        "accounts": len(results),
        "ok": ok,
        "partial": partial,
        "failed": failed,
        "couponCount": coupon_total_count,
        "expiringSoonCount": expiring_soon_count,
        "expiringSoonBalance": f"{expiring_soon_balance:.2f}",
        "nearestExpiry": nearest,
        "byCurrency": groups,
        "at": now_ms,
    }
