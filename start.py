#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start.py — ダブルクリックで起動する入口。

これ 1 つで、
  ・座席表の自動取得（大範囲走査 + 開映 30 分前 / 5 分前）
  ・閲覧用のサーバ（http://localhost:8000/）
の両方が動く。止めたくなったら黒い窓を閉じるか Ctrl-C。

タスクスケジューラも .bat も要らない。PC を使っている間だけ動けばよい、
という前提の作り。付けっぱなしにしないなら、朝いちで一度立ち上げておくと
その日ぶんはだいたい拾える。
"""

from __future__ import annotations

import ctypes
import functools
import http.server
import importlib.util
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8000
OPEN_BROWSER = True          # 起動時にブラウザを開くか
KEEP_AWAKE = True            # 動いている間だけ PC のスリープを止めるか


# ─────────────────────────────────────────────────────────────
#  ダブルクリック起動の後始末
# ─────────────────────────────────────────────────────────────

def hold(code: int = 1) -> None:
    """窓が一瞬で消えないように待つ。

    ダブルクリックで起動すると、落ちた瞬間に窓ごと消えてエラーが読めない。
    入力が繋がっていない場合（パイプ経由など）は EOFError で素通りする。
    """
    try:
        input("\nEnter で閉じます... ")
    except (EOFError, KeyboardInterrupt, OSError):
        pass
    sys.exit(code)


def load_hub():
    path = ROOT / "hub.py"
    if not path.exists():
        print(f"hub.py が見つかりません（{path}）")
        print("start.py は cinema フォルダの直下に置いてください。")
        hold()
    spec = importlib.util.spec_from_file_location("hub", path)
    mod = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
    sys.modules["hub"] = mod
    spec.loader.exec_module(mod)                         # type: ignore[union-attr]
    return mod


def check_playwright() -> bool:
    try:
        import playwright                                # noqa: F401
        return True
    except ImportError:
        print("Playwright が入っていません。取得はできませんが、")
        print("すでにあるデータの閲覧だけなら続けられます。")
        print("入れるなら:  pip install playwright  →  playwright install chromium\n")
        return False


def set_title(text: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(text)   # type: ignore[attr-defined]
        except Exception:
            pass


def keep_awake(on: bool) -> None:
    """動いている間だけスリープを抑える（Windows のみ）。

    撮影は開映 5 分前という細かい時刻を当てにいくので、途中でスリープに
    入られるとその回は取り返しがつかない。ES_CONTINUOUS だけを渡し直すと
    解除になるので、終了時に必ず呼んで元へ戻す。画面は消えてよいから
    ES_DISPLAY_REQUIRED は立てない。
    """
    if os.name != "nt" or not KEEP_AWAKE:
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(     # type: ignore[attr-defined]
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  閲覧用サーバ
# ─────────────────────────────────────────────────────────────

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """アクセスログを出さない。取得のログと混ざると読めなくなるので。"""

    def log_message(self, fmt, *args):
        pass


def free_port(start: int) -> int:
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def serve_in_background(port: int) -> None:
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────

BANNER = """
╔════════════════════════════════════════════════════════╗
║  梅田 二館 座席表アーカイブ                            ║
╚════════════════════════════════════════════════════════╝
  閲覧   http://localhost:{port}/
  停止   この窓を閉じる、または Ctrl-C
  記録   logs/ と data/captures/ に残ります
"""


def main() -> None:
    os.chdir(ROOT)          # ダブルクリック時は作業フォルダが別の場所になる
    set_title("座席表アーカイブ")
    hub = load_hub()
    check_playwright()

    port = free_port(PORT)
    try:
        serve_in_background(port)
    except OSError as exc:
        print(f"サーバを立てられませんでした（{exc}）。取得だけ続けます。")
        port = 0

    print(BANNER.format(port=port or PORT))
    keep_awake(True)
    if KEEP_AWAKE and os.name == "nt":
        print("  電源   動いている間はスリープしません")

    if port and OPEN_BROWSER:
        threading.Timer(1.0, webbrowser.open, [f"http://localhost:{port}/"]).start()

    # 起動直後の状態を出しておく。何も起きない時間が長いので、
    # 「動いているのか分からない」を防ぐ。
    try:
        hub.cmd_status()
    except Exception as exc:
        print(f"（状態の表示に失敗: {exc}）")
    print("\n" + "─" * 58)

    last_beat = 0.0
    while True:
        try:
            # 待機中の表示は hub 側が 15 分に 1 回に間引く
            hub.cmd_tick(quiet=time.time() - last_beat < 900)
            if time.time() - last_beat >= 900:
                last_beat = time.time()
            time.sleep(hub.TICK_SEC)
        except KeyboardInterrupt:
            keep_awake(False)
            set_title("座席表アーカイブ — 停止")
            print("\n停止しました。データは保存済みです。")
            print("次に開いたときは、抜けたぶんから自動で追いつきます。")
            return
        except Exception:
            # 1 回の失敗で丸ごと止めない。次の周回で立て直す。
            traceback.print_exc()
            print("↑ この回は飛ばして続けます\n")
            time.sleep(hub.TICK_SEC)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        hold()
    finally:
        keep_awake(False)
