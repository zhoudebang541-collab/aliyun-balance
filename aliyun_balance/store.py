"""本地账户存储：Windows DPAPI 加密 Secret，原子写入 JSON。

设计取舍：
  * AK 是标识符，明文保存以便展示掩码；SK 用当前 Windows 用户的 DPAPI 加密，
    只有同一台机器的同一用户能解开。文件被拷走也无法解密。
  * 前台接口永远不回传 SK，只回 hasSk。更新时 SK 留空表示保持原值。
  * 非 Windows 或 DPAPI 不可用时退回 base64 混淆（prefix 明确标记），
    并在 /api/health 里报告 protection，让界面可以提示风险。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid

try:  # ctypes.wintypes 只在 Windows 上可导入；其它平台走 base64 回退
    from ctypes import wintypes
except Exception:  # pragma: no cover - 非 Windows
    wintypes = None

from .bss import BssError, mask_ak, validate_endpoint

LOG = logging.getLogger("aliyun_balance.store")

DPAPI_PREFIX = "dpapi:v1:"
B64_PREFIX = "b64:v1:"
_ENTROPY = b"aliyun-balance-board/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_IS_WINDOWS = sys.platform == "win32"
_DWORD = wintypes.DWORD if wintypes is not None else ctypes.c_ulong


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_ready() -> bool:
    return _IS_WINDOWS


def _dpapi(data: bytes, protect: bool) -> bytes:
    if not _IS_WINDOWS:
        raise OSError("DPAPI 仅在 Windows 上可用")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_blob, _in_buf = _blob(data)
    entropy_blob, _entropy_buf = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()

    if protect:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "Aliyun Balance Board",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    else:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )

    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_secret(plain: str) -> str:
    raw = str(plain).encode("utf-8")
    if _dpapi_ready():
        try:
            return DPAPI_PREFIX + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except OSError as exc:
            LOG.warning("DPAPI 加密失败，退回 base64 混淆：%s", exc)
    return B64_PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect_secret(token: str) -> str:
    token = str(token or "")
    try:
        if token.startswith(DPAPI_PREFIX):
            return _dpapi(base64.b64decode(token[len(DPAPI_PREFIX):]), False).decode("utf-8")
        if token.startswith(B64_PREFIX):
            return base64.b64decode(token[len(B64_PREFIX):]).decode("utf-8")
        # 兼容早期明文格式
        return token
    except Exception as exc:  # DPAPI 失败通常意味着换了机器 / 换了用户
        raise StoreError(
            "无法解密保存的 Secret（可能换了 Windows 用户或机器）。请重新输入该账户的 SK。"
        ) from exc


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

class StoreError(Exception):
    pass


def _clean_ak(ak) -> str:
    ak = str(ak or "").strip()
    if not ak:
        raise StoreError("AccessKey ID 不能为空")
    if len(ak) < 8 or len(ak) > 64 or any(ch.isspace() for ch in ak):
        raise StoreError("AccessKey ID 格式不正确")
    return ak


def _clean_sk(sk) -> str:
    sk = str(sk or "").strip()
    if not sk:
        raise StoreError("AccessKey Secret 不能为空")
    if len(sk) < 8 or len(sk) > 128 or any(ch.isspace() for ch in sk):
        raise StoreError("AccessKey Secret 格式不正确")
    return sk


def _clean_name(name, ak: str) -> str:
    name = str(name or "").strip()
    if not name:
        name = f"账户 {ak[:4]}…{ak[-4:]}" if len(ak) > 8 else "账户 " + ak[-4:]
    if len(name) > 60:
        name = name[:60]
    return name


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class AccountStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._accounts: list[dict] = []
        self.load()

    # ---- 文件 ----
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                self._accounts = []
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                accounts = data.get("accounts") if isinstance(data, dict) else None
                if not isinstance(accounts, list):
                    raise ValueError("accounts 不是数组")
                self._accounts = [a for a in accounts if isinstance(a, dict) and a.get("id")]
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                backup = f"{self.path}.corrupt-{int(time.time())}"
                try:
                    os.replace(self.path, backup)
                    LOG.error("账户文件损坏，已备份到 %s：%s", backup, exc)
                except OSError:
                    LOG.error("账户文件损坏且无法备份：%s", exc)
                self._accounts = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "version": 1,
            "app": "aliyun-balance-board",
            "savedAt": int(time.time() * 1000),
            "accounts": self._accounts,
        }
        fd, tmp = tempfile.mkstemp(prefix=".accounts-", suffix=".json", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- 查询 ----
    def protection(self) -> str:
        tokens = [str(a.get("sk") or "") for a in self._accounts]
        if not tokens:
            return "dpapi" if _dpapi_ready() else "base64"
        if all(t.startswith(DPAPI_PREFIX) for t in tokens):
            return "dpapi"
        if all(t.startswith(B64_PREFIX) for t in tokens):
            return "base64"
        return "mixed"

    def public_list(self) -> list[dict]:
        with self._lock:
            return [self._public(a) for a in self._accounts]

    @staticmethod
    def _public(account: dict) -> dict:
        ak = str(account.get("ak") or "")
        return {
            "id": account.get("id"),
            "name": account.get("name") or "",
            "akMasked": mask_ak(ak),
            "akTail": ak[-4:] if len(ak) >= 4 else ak,
            "hasSk": bool(account.get("sk")),
            "endpoint": account.get("endpoint") or "business.aliyuncs.com",
            "enabled": bool(account.get("enabled", True)),
            "createdAt": account.get("createdAt"),
            "updatedAt": account.get("updatedAt"),
        }

    def get(self, account_id: str) -> dict | None:
        with self._lock:
            for account in self._accounts:
                if account.get("id") == account_id:
                    return dict(account)
        return None

    def credentials(self, account_id: str) -> dict:
        with self._lock:
            account = self.get(account_id)
            if not account:
                raise StoreError("账户不存在")
            try:
                sk = unprotect_secret(account.get("sk") or "")
            except StoreError:
                raise
            return {
                "id": account["id"],
                "name": account.get("name") or "",
                "ak": account.get("ak") or "",
                "sk": sk,
                "endpoint": account.get("endpoint") or "business.aliyuncs.com",
            }

    def credentials_for(self, accounts: list[dict]) -> list[dict]:
        return [self.credentials(a["id"]) for a in accounts]

    # ---- 写入 ----
    def upsert_many(self, items: list[dict]) -> dict:
        added, updated, errors = 0, 0, []
        with self._lock:
            for index, item in enumerate(items):
                try:
                    if not isinstance(item, dict):
                        raise StoreError("格式不正确")
                    ak = _clean_ak(item.get("ak"))
                    sk = str(item.get("sk") or "").strip()
                    endpoint = validate_endpoint(item.get("endpoint") or "")
                    name = _clean_name(item.get("name"), ak)
                    existing = next((a for a in self._accounts if a.get("ak") == ak), None)
                    now = int(time.time() * 1000)
                    if existing:
                        if sk:
                            existing["sk"] = protect_secret(_clean_sk(sk))
                        existing["name"] = name
                        existing["endpoint"] = endpoint
                        if "enabled" in item:
                            existing["enabled"] = bool(item.get("enabled"))
                        existing["updatedAt"] = now
                        updated += 1
                    else:
                        self._accounts.append(
                            {
                                "id": uuid.uuid4().hex[:12],
                                "name": name,
                                "ak": ak,
                                "sk": protect_secret(_clean_sk(sk)),
                                "endpoint": endpoint,
                                "enabled": bool(item.get("enabled", True)),
                                "createdAt": now,
                                "updatedAt": now,
                            }
                        )
                        added += 1
                except (StoreError, BssError, ValueError) as exc:
                    errors.append({"index": index, "message": str(exc)})
            if added or updated:
                self._save()
        return {"added": added, "updated": updated, "errors": errors}

    def update(self, account_id: str, fields: dict) -> dict:
        with self._lock:
            account = next((a for a in self._accounts if a.get("id") == account_id), None)
            if not account:
                raise StoreError("账户不存在")
            if "ak" in fields and fields["ak"] is not None:
                ak = _clean_ak(fields["ak"])
                if ak != account.get("ak") and any(a.get("ak") == ak for a in self._accounts):
                    raise StoreError("该 AccessKey 已在其他账户中使用")
                account["ak"] = ak
            if fields.get("sk"):
                account["sk"] = protect_secret(_clean_sk(fields["sk"]))
            if "name" in fields:
                account["name"] = _clean_name(fields.get("name"), account.get("ak", ""))
            if "endpoint" in fields and fields["endpoint"]:
                account["endpoint"] = validate_endpoint(fields["endpoint"])
            if "enabled" in fields:
                account["enabled"] = bool(fields["enabled"])
            account["updatedAt"] = int(time.time() * 1000)
            self._save()
            return self._public(account)

    def delete(self, account_id: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.get("id") != account_id]
            if len(self._accounts) == before:
                return False
            self._save()
            return True

    def clear(self) -> int:
        with self._lock:
            count = len(self._accounts)
            self._accounts = []
            self._save()
            return count

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)
