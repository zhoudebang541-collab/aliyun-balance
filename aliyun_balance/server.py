"""本地 HTTP 服务：静态页面 + JSON API。

安全边界：
  * 只监听 127.0.0.1；校验 Host / Origin，降低 DNS rebinding 风险；
  * 所有写操作要求自定义请求头 X-Balance-Board: 1（跨站表单无法伪造）；
  * 密钥永不出现在响应里，只有 hasSk 与 AK 掩码；
  * 静态资源只暴露 web/ 下固定几个文件，不做目录遍历。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from . import bss
from .store import AccountStore, StoreError

LOG = logging.getLogger("aliyun_balance.server")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/favicon.svg": "favicon.svg",
}
ACCOUNT_ID_RE = re.compile(r"^/api/accounts/([a-f0-9]{12})$")
MAX_BODY = 2 * 1024 * 1024
MAX_BATCH = 300
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "BadRequest"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class BalanceApp:
    def __init__(self, store: AccountStore, fetcher=None):
        self.store = store
        self.fetcher = fetcher or bss.fetch_accounts
        self._query_lock = threading.Lock()

    # ---- API ----
    def health(self) -> dict:
        return {
            "ok": True,
            "app": "aliyun-balance-board",
            "version": __version__,
            "protection": self.store.protection(),
            "accounts": self.store.count(),
        }

    def add_accounts(self, payload) -> dict:
        items = payload.get("accounts") if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not items:
            raise ApiError("没有可添加的账户")
        if len(items) > MAX_BATCH:
            raise ApiError(f"单次最多添加 {MAX_BATCH} 个账户")
        result = self.store.upsert_many(items)
        result["accounts"] = self.store.public_list()
        return {"ok": True, **result}

    def update_account(self, account_id: str, payload: dict) -> dict:
        fields = payload if isinstance(payload, dict) else {}
        try:
            account = self.store.update(account_id, fields)
        except StoreError as exc:
            raise ApiError(str(exc), 404 if "不存在" in str(exc) else 400) from exc
        return {"ok": True, "account": account}

    def delete_account(self, account_id: str) -> dict:
        if not self.store.delete(account_id):
            raise ApiError("账户不存在", 404)
        return {"ok": True}

    def clear_accounts(self) -> dict:
        return {"ok": True, "removed": self.store.clear()}

    def _select(self, payload: dict) -> list[dict]:
        public = {a["id"]: a for a in self.store.public_list()}
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if ids is None:
            selected = [a for a in public.values() if a["enabled"]]
        elif isinstance(ids, list):
            selected = [public[i] for i in ids if i in public]
        else:
            raise ApiError("ids 必须是数组")
        if not selected:
            if not public:
                raise ApiError("还没有账户，请先添加")
            raise ApiError("没有可查询的账户（可能都已停用）", code="NoAccounts")
        if len(selected) > MAX_BATCH:
            raise ApiError(f"单次最多查询 {MAX_BATCH} 个账户")
        return selected

    def query(self, payload: dict) -> dict:
        selected = self._select(payload or {})
        credentials = []
        for account in selected:
            try:
                credentials.append(self.store.credentials(account["id"]))
            except StoreError as exc:
                raise ApiError(str(exc)) from exc
        with self._query_lock:
            results = self.fetcher(credentials)
        return {
            "ok": True,
            "at": bss.now_ms(),
            "results": results,
            "aggregate": bss.aggregate(results),
        }

    def test(self, payload: dict) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        account_id = payload.get("id")
        if account_id:
            try:
                creds = self.store.credentials(account_id)
            except StoreError as exc:
                raise ApiError(str(exc), 404) from exc
            if payload.get("ak"):
                creds["ak"] = str(payload["ak"]).strip()
            if payload.get("sk"):
                creds["sk"] = str(payload["sk"]).strip()
            if payload.get("endpoint"):
                creds["endpoint"] = str(payload["endpoint"]).strip()
        else:
            ak = str(payload.get("ak") or "").strip()
            sk = str(payload.get("sk") or "").strip()
            endpoint = str(payload.get("endpoint") or bss.DEFAULT_ENDPOINT).strip()
            if not ak or not sk:
                raise ApiError("请填写 AccessKey ID 和 Secret")
            creds = {"id": "test", "name": "连接测试", "ak": ak, "sk": sk, "endpoint": endpoint}
        try:
            result = bss.fetch_account(creds)
        except bss.BssError as exc:
            raise ApiError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - 防御
            LOG.exception("credential test failed")
            raise ApiError(f"测试失败：{exc}", 500) from exc
        # 凭据错误也属于「测试完成」，用 200 把明细交给前端展示
        return {"ok": True, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = f"AliyunBalanceBoard/{__version__}"
    protocol_version = "HTTP/1.1"
    app: BalanceApp = None  # 由 make_server 绑定

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _headers(self, status: int, content_type: str, length: int, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, payload: dict, status: int = 200):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw))
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, message: str, status: int = 400, code: str = "BadRequest"):
        self._json({"ok": False, "error": {"code": code, "message": message}}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Content-Length 无效")
        if length > MAX_BODY:
            raise ApiError("请求体过大", 413)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("请求体不是合法 JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise ApiError("请求体格式不正确")
        return payload

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.split(":")[0]
        if name not in ALLOWED_HOSTS:
            self._error("非法 Host", 403, "ForbiddenHost")
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.netloc.lower() != host:
                self._error("非法 Origin", 403, "ForbiddenOrigin")
                return False
        return True

    def _check_csrf(self) -> bool:
        if self.headers.get("X-Balance-Board") != "1":
            self._error("缺少请求标记", 403, "Forbidden")
            return False
        return True

    # ---- 路由 ----
    def do_GET(self):
        if not self._check_host():
            return
        try:
            path = urlparse(self.path).path
            if path == "/api/health":
                self._json(self.app.health())
                return
            if path == "/api/accounts":
                self._json({"ok": True, "accounts": self.app.store.public_list()})
                return
            if path.startswith("/api/"):
                self._error("接口不存在", 404, "NotFound")
                return
            self._static(path)
        except Exception as exc:  # pragma: no cover - 防御
            LOG.error("GET failed: %s", exc)
            LOG.debug(traceback.format_exc())
            self._error("服务器内部错误", 500, "InternalError")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._check_host() or not self._check_csrf():
            return
        try:
            path = urlparse(self.path).path
            payload = self._body()
            if path == "/api/accounts":
                self._json(self.app.add_accounts(payload), 201)
            elif path == "/api/accounts/clear":
                self._json(self.app.clear_accounts())
            elif path == "/api/query":
                self._json(self.app.query(payload if isinstance(payload, dict) else {}))
            elif path == "/api/test":
                self._json(self.app.test(payload if isinstance(payload, dict) else {}))
            else:
                self._error("接口不存在", 404, "NotFound")
        except ApiError as exc:
            self._error(exc.message, exc.status, exc.code)
        except (StoreError, bss.BssError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - 防御
            LOG.error("POST failed: %s", exc)
            LOG.debug(traceback.format_exc())
            self._error("服务器内部错误", 500, "InternalError")

    def do_PUT(self):
        if not self._check_host() or not self._check_csrf():
            return
        try:
            path = urlparse(self.path).path
            match = ACCOUNT_ID_RE.match(path)
            if not match:
                self._error("接口不存在", 404, "NotFound")
                return
            self._json(self.app.update_account(match.group(1), self._body()))
        except ApiError as exc:
            self._error(exc.message, exc.status, exc.code)
        except (StoreError, bss.BssError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - 防御
            LOG.error("PUT failed: %s", exc)
            self._error("服务器内部错误", 500, "InternalError")

    def do_DELETE(self):
        if not self._check_host() or not self._check_csrf():
            return
        try:
            path = urlparse(self.path).path
            match = ACCOUNT_ID_RE.match(path)
            if not match:
                self._error("接口不存在", 404, "NotFound")
                return
            self._json(self.app.delete_account(match.group(1)))
        except ApiError as exc:
            self._error(exc.message, exc.status, exc.code)
        except Exception as exc:  # pragma: no cover - 防御
            LOG.error("DELETE failed: %s", exc)
            self._error("服务器内部错误", 500, "InternalError")

    def _static(self, path: str):
        name = STATIC_FILES.get(path)
        if not name:
            self._error("页面不存在", 404, "NotFound")
            return
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            self._error("前端资源缺失，请重新解压完整目录", 500, "MissingAsset")
            return
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(raw))
        if self.command != "HEAD":
            self.wfile.write(raw)


def make_server(host: str, port: int, app: BalanceApp) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
