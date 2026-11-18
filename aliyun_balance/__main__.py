"""启动本地看板：python -m aliyun_balance"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from . import __app_name__, __version__
from .server import BalanceApp, make_server
from .store import AccountStore

DEFAULT_PORT = 17860
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logging(data_dir: str, debug: bool = False) -> None:
    os.makedirs(data_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if root.handlers:
        root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            os.path.join(data_dir, "app.log"), maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass


def banner(url: str, data_dir: str, protection: str) -> None:
    line = "─" * 54
    print()
    print(line)
    print(f"  {__app_name__}  v{__version__}")
    print(line)
    print(f"  本机地址   {url}")
    print(f"  数据目录   {data_dir}")
    print(f"  密钥保护   {'Windows DPAPI（当前用户）' if protection == 'dpapi' else 'base64 混淆（未使用 DPAPI）'}")
    print(line)
    print("  浏览器未自动打开时，手动复制上面的地址访问。")
    print("  关闭本窗口或按 Ctrl+C 即可停止服务。")
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aliyun-balance-board", description=__app_name__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("ABB_PORT", DEFAULT_PORT)))
    parser.add_argument("--data-dir", default=os.environ.get("ABB_DATA_DIR") or os.path.join(PROJECT_ROOT, "data"))
    parser.add_argument("--no-browser", action="store_true", default=os.environ.get("ABB_NO_BROWSER") == "1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    data_dir = os.path.abspath(args.data_dir)
    setup_logging(data_dir, args.debug)
    log = logging.getLogger("aliyun_balance")

    store = AccountStore(os.path.join(data_dir, "accounts.json"))
    app = BalanceApp(store)

    server = None
    port = args.port
    for offset in range(0, 20):
        candidate = args.port + offset
        try:
            server = make_server("127.0.0.1", candidate, app)
            port = candidate
            break
        except OSError as exc:
            if getattr(exc, "errno", None) in (10048, 48, 98) or "address already in use" in str(exc).lower():
                log.warning("端口 %s 被占用，尝试 %s", candidate, candidate + 1)
                continue
            raise

    if server is None:
        log.error("连续 20 个端口都被占用，无法启动")
        return 1

    url = f"http://127.0.0.1:{port}"
    protection = store.protection()
    banner(url, data_dir, protection)
    log.info("listening on %s (data: %s)", url, data_dir)

    if not args.no_browser:
        def open_browser():
            time.sleep(0.5)
            try:
                webbrowser.open_new_tab(url)
            except Exception:
                log.info("自动打开浏览器失败，请手动访问 %s", url)

        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
