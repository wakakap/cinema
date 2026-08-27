#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cinema.py — T・ジョイ梅田 上映スケジュール & 座席表アーカイバ

各回の開映前に座席選択画面へ入り、その時点の埋まり具合を画像と数値で記録する。

    python cinema.py doctor      まず これ。環境と取得経路を一通り自己診断する
    python cinema.py run         通常運転（sync + capture）
    python cinema.py log         直近の動作ログを見る

詳しい使い方は README.md、コマンド一覧は `python cinema.py --help`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.robotparser import RobotFileParser

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:                                   # tzdata が無い環境向け
    JST = timezone(timedelta(hours=9))


# ═══════════════════════════════════════════════════════════════════
#  設定
# ═══════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent

DATA = ROOT / "data"
CAPTURES = DATA / "captures"
MEDIA = ROOT / "media"
SEATS = MEDIA / "seats"
POSTERS = MEDIA / "posters"
SCREENS = MEDIA / "screens"
LOGS = ROOT / "logs"

SCHEDULE_FILE = DATA / "schedule.json"      # 待ち行列（未取得の回だけ）
FILMS_FILE = DATA / "films.json"            # 作品台帳
INDEX_FILE = DATA / "index.json"            # フロント用の目次
STATE_FILE = DATA / "state.json"            # 実行状態（クールダウン等）
LOG_FILE = LOGS / "cinema.log"

THEATER = {
    "id": "tjoyumeda",
    "code": "320",
    "name": "T・ジョイ梅田",
    "url": "https://tjoy.jp/t-joy_umeda",
    "schedule_url": "https://tjoy.jp/t-joy_umeda#schedule-content",
    "seat_url_hint": "choice_seat",         # 座席選択画面はこの固定 URL
    "robots_url": "https://tjoy.jp/robots.txt",
}

# --- 取得タイミング -------------------------------------------------
# 窓は cron の**実際の**間隔より広くないと、回が丸ごと漏れる。
# 実測：設定 10 分に対し実間隔 14〜69 分、数時間空くこともある。
CAPTURE_LEAD_MIN = 90       # 開映何分前から対象にするか（＝取りこぼさないための窓）
LATE_GRACE_MIN = 5          # 開映後の猶予
TARGET_LEAD_MIN = 25        # 2 枚目以降はここまで引きつけてから撮る
FINAL_LEAD_MIN = 15         # これ以内で撮れたら確定
MAX_CAPTURES = 3            # 1 回あたりの最大撮影回数

# 窓が広いこと自体は害ではないが、「窓に入った端から撮る」と撮影予算を
# 誰も買っていない時間帯で使い切ってしまう（90→75→60 分前で打ち止め）。
# そこで 1 枚目だけ保険として即撮り、2 枚目以降は TARGET_LEAD_MIN まで待つ。
# 結果：90 分前（保険）→ 24 分前 → 12 分前（確定）のように予算が活きる。
# 窓＝取りこぼさないため、撮影時機＝正確さのため、と役割を分けている。
MAX_ATTEMPTS = 3            # 失敗の上限。超えたら failed にして行列から外す

# --- スケジュール同期 -----------------------------------------------
# 待ち行列に要るのは今日・明日だけ（フロントは captures/ を見るので
# 先の日付は使われない）。全件走査は手動の `sync --full` に降格。
LIGHT_SYNC_TABS = 2
SCHEDULE_TTL_MIN = 360

# --- 相手サイトへの配慮 ---------------------------------------------
REQUEST_DELAY_SEC = 3.0
REQUEST_JITTER_SEC = 2.0
COOLDOWN_MIN_ON_BLOCK = 180     # 403/429/5xx を踏んだら全面停止する時間
NAV_TIMEOUT_MS = 30_000
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 1 ページ開くと CSS/JS/画像/広告/計測で 50+ リクエストになる。
# 無関係なものは落とす。座席ページの画像だけは撮影対象なので残す。
BLOCK_RESOURCE_TYPES = {"media", "font", "websocket", "manifest"}
BLOCK_URL_KEYWORDS = (
    "google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
    "googleadservices", "adservice", "facebook.net", "facebook.com",
    "criteo", "rubiconproject", "amazon-adsystem", "hotjar", "clarity.ms",
    "newrelic", "nr-data.net", "sentry.io", "twitter.com", "youtube.com", "ytimg.com",
)

# --- 保存 -----------------------------------------------------------
SAVE_SEAT_SHOT = True       # False にすると数値だけ記録（リポジトリが太らない）
SEAT_SHOT_QUALITY = 80      # JPEG。座席図は平坦な色面なので劣化は見えない
DEDUPE_SCREENS = True       # スクリーン背景図は内容ハッシュで 1 枚だけ持つ
KEEP_PAST_DAYS = 2          # 行列に過去分を残す日数

# --- ログ -----------------------------------------------------------
MAX_LOG_LINES = 3000
GAP_WARN_MIN = 60           # 前回実行からこれ以上空いていたら警告を出す


# ═══════════════════════════════════════════════════════════════════
#  サイト固有の知識（実測で確定済み。仕様変更時はここだけ見ればよい）
# ═══════════════════════════════════════════════════════════════════

SELECTORS = {
    # 日付タブ。上下に同じカレンダーが 2 本あるので日付で重複排除する。
    # .calendar-disable は未公開の日で、押しても中身が無い。
    "day_tabs": [".calendar-item:not(.calendar-disable)", ".calendar-item"],
    # 作品ブロック（折りたたみの中身）。作品名は**この中に無い**ので注意。
    "film_block": [".film-content", ".film-item", ".card-body"],
    # 作品名は .card-header 側にある兄弟要素。film_block と個数・順序が
    # 一致するのでインデックスで対応付ける（中から探すと「作品詳細」を拾う）。
    "film_title": [".film-title", ".js-title-film", "h4"],
    "showing_row": [".schedule-box", ".time-film li", "li"],
    # 座席選択画面
    # .seat-area は「シアター1〜7 座席表」の説明モーダル（7 個）なので使わない。
    "seat_container": [".js-map-seat", ".map-seat", "[class*='map-seat']"],
    "movie_date": [".movie-date.font-weight-bold", ".movie-date"],
}

# 座席の状態は背景図の上に重なった要素で表現される。
# 実測（シアター1）：空席 157 + 販売済 219 = 376 で公称座席数と一致。
SEAT_VACANT = "area.seat-select"
SEAT_SOLD = "img.sold-out, .sold-out"

# 各シアターの座席数（公式「座席表」表示。車椅子席は含まず）
SCREEN_SEATS = {"01": 376, "02": 146, "03": 207, "04": 95,
                "05": 95, "06": 330, "07": 141}

TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[～~〜\-]\s*(\d{1,2}):(\d{2})")
DAY_LABEL_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
AVAIL_RE = re.compile(r"icon_label(\d)")
# 予約先は .schedule-status の onclick に直接書かれている。
#   /t-joy_umeda/reservation/index/785622/C4463060/1/2026-08-28?type=film
#   = 回ID / 作品番号 / シアター番号 / 上映日
RESERVE_RE = re.compile(
    r"/[\w-]+/reservation/index/(\d+)/([A-Za-z0-9]+)/(\d+)/(\d{4}-\d{2}-\d{2})[^\s'\"]*")
# シアター番号の最も確実な出所（PC/SP で 2 度描画される館名テキストより堅い）
SCREEN_RE = re.compile(r'data-target\s*=\s*["\']#screen(\d+)["\']')
# 作品名の取得に失敗したとき、ボタン文字を掴まないための番人
BUTTON_TEXT_RE = re.compile(r"^(作品詳細|予約|購入|上映スケジュール|座席表|詳細)\s*$")

AVAIL_MAP = {   # 凡例の並び：◎ ○ △ 満席
    "2": {"code": "plenty", "mark": "◎", "label": "空席に余裕があります"},
    "3": {"code": "available", "mark": "○", "label": "空席があります"},
    "4": {"code": "few", "mark": "△", "label": "空席がわずかです"},
    "5": {"code": "full", "mark": "×", "label": "満席"},
}

VERSION_TAGS = ["DolbyCinema", "Dolby Cinema", "Dolby Atmos", "IMAX", "4DX", "MX4D",
                "ScreenX", "字幕", "吹替", "2D", "3D", "バリアフリー", "レイトショー"]


# ═══════════════════════════════════════════════════════════════════
#  ログ
# ═══════════════════════════════════════════════════════════════════

LOG = logging.getLogger("cinema")


class EventLog:
    """要点だけを 1 行 1 件で残すログ。

    デバッグ出力とは別物で、「いつ何が起きたか」を後から俯瞰するためのもの。
    特に **RUN 行の間隔を見れば cron が本当に回っているか一目で分かる**。
    """

    def __init__(self, path: Path = LOG_FILE):
        self.path = path
        self.buffer: list[str] = []

    def add(self, kind: str, status: str, detail: str = "") -> None:
        stamp = now_jst().strftime("%m-%d %H:%M:%S")
        self.buffer.append(f"{stamp}  {kind:<5} {status:<9} {detail}".rstrip())
        LOG.info("%-5s %-9s %s", kind, status, detail)

    def flush(self) -> None:
        if not self.buffer:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        merged = old + self.buffer
        if len(merged) > MAX_LOG_LINES * 1.2:        # まとめて刈る
            merged = merged[-MAX_LOG_LINES:]
        self.path.write_text("\n".join(merged) + "\n", encoding="utf-8")
        self.buffer.clear()


EV = EventLog()


# ═══════════════════════════════════════════════════════════════════
#  基本ユーティリティ
# ═══════════════════════════════════════════════════════════════════

def now_jst() -> datetime:
    return datetime.now(JST)


def read_json(path: Path, default: Any) -> Any:
    """存在しなければ default。**存在するのに壊れていたら例外**。

    ここで黙って default を返すと、たとえば captures/2026-08.json が壊れた
    ときに「その月は 0 件」として上書きしてしまい、1 か月分が消える。
    読めないなら止まるほうが遥かにましなので、握り潰さない。
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"{path} が読めない（{exc}）。"
                           f"手で直すか削除するまで処理を止める") from exc


def write_json(path: Path, payload: Any) -> None:
    """一時ファイルに書いてから差し替える（途中で落ちても壊れない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def fmt_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{int(minutes // 60)}h{int(minutes % 60):02d}m"


def polite_sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC))


# ── 作品名の解析 ───────────────────────────────────────────────────

_RUNTIME_RE = re.compile(r"[（(]\s*本編\s*[：:]\s*[0-9０-９]+\s*分\s*[）)]")
_RUNTIME_NUM_RE = re.compile(r"本編\s*[：:]\s*([0-9]+)\s*分")
_ENDNOTE_RE = re.compile(r"[0-9]{1,2}[.．][0-9]{1,2}\s*[（(][月火水木金土日][）)]\s*上映(?:終了|開始)")
_BRACKET_RE = re.compile(r"【([^】]*)】")
_RATING_RE = re.compile(r"PG-?12|R15\+|R18\+")


def _canon(tag: str) -> str:
    return tag.replace(" ", "").replace("・", "").replace("-", "").lower()


def parse_title(raw: str) -> dict:
    """「字幕 8.27(木)上映終了【字幕】Michael／マイケル （本編：131分）」を
    作品名・版タグ・上映時間に分解する。

    タイトル全体に NFKC をかけないこと。「奇々怪々！」が「奇々怪々!」に、
    「バケ～ション」が「バケ~ション」になってしまう。正規表現側で全角半角
    どちらも拾うようにしてある。
    """
    text = (raw or "").strip()

    m = _RUNTIME_NUM_RE.search(text)
    runtime = int(m.group(1)) if m else None
    text = _ENDNOTE_RE.sub("", _RUNTIME_RE.sub("", text))

    tags: list[str] = []

    def add(tag: str) -> None:
        if _canon(tag) not in {_canon(t) for t in tags}:
            tags.append(tag)

    for inner in _BRACKET_RE.findall(text):
        for tag in VERSION_TAGS:
            if _canon(tag) in _canon(inner):
                add(tag)

    changed = True
    while changed:                       # 先頭の【…】と裸の版タグを剥がし切る
        changed = False
        stripped = re.sub(r"^\s*【[^】]*】", "", text)
        if stripped != text:
            text, changed = stripped, True
            continue
        head = text.lstrip()
        for tag in sorted(VERSION_TAGS, key=len, reverse=True):
            if _canon(head).startswith(_canon(tag)):
                add(tag)
                text, changed = head[len(tag):], True
                break

    for rating in _RATING_RE.findall(text):
        add(rating)
    text = re.sub(r"\s*(?:PG-?12|R15\+|R18\+)\s*$", "", text)

    base = re.sub(r"\s+", " ", text).strip(" \u3000-–—")
    return {"base_title": base or (raw or "").strip(), "tags": tags, "runtime_min": runtime}


def film_key(base_title: str) -> str:
    """ASCII で読める安定キー。日本語だけの題は短いハッシュにする。

    作品番号（film_code）はキーに使わない。同一作品でも字幕版・吹替版・
    Dolby 版で番号が別々に振られ、共通接頭辞も無いため（実例
    C4943100 / C4943200）。題名から版タグを剥がす方式なら 1 作品にまとまる。
    """
    latin = re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"[^\x00-\x7F]+", " ", base_title))
    latin = re.sub(r"-{2,}", "-", latin).strip("-").lower()[:40].strip("-")
    digest = hashlib.sha1(base_title.encode("utf-8")).hexdigest()[:6]
    return f"{latin}-{digest}" if re.search(r"[a-z]", latin) else f"film-{digest}"


def showing_id(date: str, start: str, screen: str, fkey: str) -> str:
    return f"{date.replace('-', '')}-{start.replace(':', '')}-s{screen}-{fkey}"


# ═══════════════════════════════════════════════════════════════════
#  実行状態（クールダウン・前回実行時刻）
# ═══════════════════════════════════════════════════════════════════

class Blocked(Exception):
    """相手側に拒否・制限された。そのラウンドは即座に打ち切る。"""


def load_state() -> dict:
    return read_json(STATE_FILE, {})


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)


def in_cooldown() -> bool:
    until = load_state().get("cooldown_until")
    if not until:
        return False
    try:
        left = (datetime.fromisoformat(until) - now_jst()).total_seconds() / 60
    except ValueError:
        return False
    if left > 0:
        EV.add("RUN", "cooldown", f"残り {fmt_duration(left)}")
        return True
    return False


def enter_cooldown(reason: str) -> None:
    state = load_state()
    state["cooldown_until"] = (now_jst() + timedelta(minutes=COOLDOWN_MIN_ON_BLOCK)).isoformat()
    state["cooldown_reason"] = reason
    save_state(state)
    EV.add("BLOCK", "stop", f"{reason} → {COOLDOWN_MIN_ON_BLOCK}分 停止")


def mark_run_start() -> None:
    """前回からの間隔を記録する。ここが cron の実態を映す唯一の手がかり。"""
    state = load_state()
    prev = state.get("last_run_at")
    gap = ""
    if prev:
        try:
            mins = (now_jst() - datetime.fromisoformat(prev)).total_seconds() / 60
            gap = f"gap={fmt_duration(mins)}"
            if mins > GAP_WARN_MIN:
                gap += "   ← 前回から大きく空いた"
        except ValueError:
            pass
    EV.add("RUN", "start", gap)
    state["last_run_at"] = now_jst().isoformat()
    save_state(state)


# ═══════════════════════════════════════════════════════════════════
#  ブラウザ
# ═══════════════════════════════════════════════════════════════════

def check_response(resp, url: str) -> None:
    if resp is None:
        return
    if resp.status in (403, 429) or resp.status >= 500:
        raise Blocked(f"HTTP {resp.status} @ {url}")


def robots_allows(url: str) -> bool:
    rp = RobotFileParser()
    rp.set_url(THEATER["robots_url"])
    try:
        rp.read()
    except Exception:
        return True          # 取れないときは制限なしとみなす
    return rp.can_fetch(USER_AGENT, url)


def browser_session():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
        viewport={"width": 1440, "height": 1800}, device_scale_factor=1,
    )
    ctx.set_default_timeout(NAV_TIMEOUT_MS)

    flags = {"images": True}

    def router(route):
        req = route.request
        if req.resource_type in BLOCK_RESOURCE_TYPES:
            return route.abort()
        if req.resource_type == "image" and not flags["images"]:
            return route.abort()
        if any(k in req.url for k in BLOCK_URL_KEYWORDS):
            return route.abort()
        return route.continue_()

    ctx.route("**/*", router)
    ctx.image_flags = flags
    return pw, browser, ctx


def first_match(scope, selectors: Iterable[str]):
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, None


# ═══════════════════════════════════════════════════════════════════
#  スケジュール取得
# ═══════════════════════════════════════════════════════════════════

def resolve_date(label: str, today: datetime) -> str | None:
    m = DAY_LABEL_RE.search(label or "")
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year = today.year
    if month < today.month - 6:          # 年またぎ
        year += 1
    elif month > today.month + 6:
        year -= 1
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def schedule_fingerprint(page) -> str | None:
    """今表示されている内容の指紋（件数＋先頭末尾の本文）。"""
    try:
        return page.evaluate(r"""
          () => {
            const els = document.querySelectorAll('.schedule-box');
            const t = e => e ? e.innerText.trim().replace(/\s+/g,' ').slice(0,60) : '';
            return els.length + '|' + t(els[0]) + '|' + t(els[els.length-1]);
          }""")
    except Exception:
        return None


def wait_schedule_ready(page, before: str | None, timeout_ms: int = 12_000) -> bool:
    """日付切り替えが本当に完了するまで待つ。

    固定 sleep では足りない。Ajax なので、遅いと前日の内容や読み込み途中の
    リストを解析してしまう（症状：ある日が突然 8 件になり、次の同期で全部戻る）。
    指紋が変わり、かつ 2 回続けて動かなくなったら完了とみなす。
    """
    deadline = time.time() + timeout_ms / 1000
    changed = before is None
    last, stable = None, 0
    while time.time() < deadline:
        page.wait_for_timeout(500)
        fp = schedule_fingerprint(page)
        if not changed and fp != before:
            changed = True
        if changed and fp and fp == last:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        last = fp
    return changed


def click_tab(page, tab) -> bool:
    for target in (tab, tab.locator("a").first, tab.locator("button").first):
        try:
            target.click(timeout=6000)
            page.wait_for_timeout(300)
            return True
        except Exception:
            continue
    return False


def scrape_day(page, date: str) -> list[dict]:
    """今表示されている 1 日分を解析する。

    film_block の候補は順に試し、**実際に回が取れた**ものを採用する。
    要素が存在するだけでは足りない（.film-content が当たっても中に
    時間表が無いことがある）。
    """
    for sel in SELECTORS["film_block"]:
        try:
            if page.locator(sel).count() == 0:
                continue
        except Exception:
            continue
        rows = _scrape_blocks(page, date, sel)
        if rows:
            return rows
    return []


def _scrape_blocks(page, date: str, block_selector: str) -> list[dict]:
    blocks = page.locator(block_selector)
    n = blocks.count()
    result: list[dict] = []

    # 作品名はブロックの外（.card-header 側）にある。個数と順序が一致するので
    # インデックスで対応付ける。ブロック内から探すと「作品詳細」ボタンを掴む。
    titles: list[str] = []
    for sel in SELECTORS["film_title"]:
        try:
            loc = page.locator(sel)
            if n == 0 or loc.count() != n:
                continue
            titles = [(loc.nth(k).inner_text(timeout=3000) or "").strip() for k in range(n)]
        except Exception:
            titles = []
            continue
        if any(titles):
            break
        titles = []

    for i in range(n):
        block = blocks.nth(i)
        try:
            block_text = block.inner_text(timeout=5000)
        except Exception:
            continue
        if not TIME_RANGE_RE.search(block_text):
            continue

        raw_title = titles[i] if i < len(titles) else ""
        if not raw_title or BUTTON_TEXT_RE.match(raw_title):
            try:                            # 保険：直前の .film-title を拾う
                near = block.locator(
                    "xpath=preceding::*[contains(concat(' ', normalize-space(@class), ' '),"
                    " ' film-title ')][1]")
                if near.count():
                    raw_title = (near.first.inner_text(timeout=3000) or "").strip()
            except Exception:
                pass
        if not raw_title or BUTTON_TEXT_RE.match(raw_title):
            continue

        info = parse_title(raw_title)
        rows, _ = first_match(block, SELECTORS["showing_row"])
        if rows is None:
            continue

        for j in range(rows.count()):
            row = rows.nth(j)
            try:
                row_text = row.inner_text(timeout=3000)
                row_html = row.inner_html(timeout=3000)
            except Exception:
                continue
            tm = TIME_RANGE_RE.search(row_text)
            if not tm:
                continue

            start = f"{int(tm.group(1)):02d}:{tm.group(2)}"
            end = f"{int(tm.group(3)):02d}:{tm.group(4)}"

            sm = SCREEN_RE.search(row_html)
            screen = sm.group(1).zfill(2) if sm else "00"

            reserve_url = schedule_id = film_code = None
            rm = RESERVE_RE.search(row_html)
            if rm:
                reserve_url = "https://tjoy.jp" + rm.group(0).replace("&amp;", "&")
                schedule_id, film_code = rm.group(1), rm.group(2)
                if screen == "00":
                    screen = rm.group(3).zfill(2)

            am = AVAIL_RE.search(row_html)
            fkey = film_key(info["base_title"])

            result.append({
                "id": showing_id(date, start, screen, fkey),
                "date": date, "start": start, "end": end,
                "start_at": f"{date}T{start}:00+09:00",
                "film_key": fkey,
                "film_title": info["base_title"],
                "tags": info["tags"],
                "runtime_min": info["runtime_min"],
                "screen": f"シアター{int(screen)}" if screen != "00" else "",
                "screen_no": screen,
                "screen_seats": SCREEN_SEATS.get(screen),
                "availability": AVAIL_MAP.get(am.group(1)) if am else None,
                "reserve_url": reserve_url,
                "schedule_id": schedule_id,
                "film_code": film_code,
                "status": "pending", "attempts": 0, "captures": 0,
                "last_attempt_at": None,
            })
    return result


def scrape_schedule(page, max_tabs: int | None) -> dict:
    today = now_jst()
    resp = page.goto(THEATER["schedule_url"], wait_until="domcontentloaded",
                     timeout=NAV_TIMEOUT_MS)
    check_response(resp, THEATER["schedule_url"])
    page.wait_for_timeout(2500)

    tabs, _ = first_match(page, SELECTORS["day_tabs"])
    if tabs is None:
        raise RuntimeError("日付タブが見つからない（`cinema.py doctor` で確認）")

    days: dict[str, dict] = {}
    seen: set[str] = set()
    for i in range(tabs.count()):
        tab = tabs.nth(i)
        try:
            label = (tab.inner_text(timeout=5000) or "").strip()
        except Exception:
            continue
        date = resolve_date(label, today)
        if not date or date in seen:
            continue                      # カレンダーは上下 2 本あるので 1 回だけ
        if max_tabs is not None and len(seen) >= max_tabs:
            break

        before = schedule_fingerprint(page)
        if not click_tab(page, tab):
            EV.add("SYNC", "skip", f"{date} タブを押せない")
            continue
        if not wait_schedule_ready(page, before if seen else None):
            EV.add("SYNC", "skip", f"{date} 内容が切り替わらない")
            continue

        seen.add(date)
        showings = scrape_day(page, date)
        if not showings:
            EV.add("SYNC", "skip", f"{date} 0 件（既存データを守るため書かない）")
            continue

        m = re.search(r"OPEN\s*(\d{1,2}:\d{2})", label)
        note_m = re.search(r"(水曜サービスデー|KINEZO会員デー|ファーストデー|映画の日)", label)
        open_time = m.group(1) if m else None
        note = note_m.group(1) if note_m else None
        for s in showings:
            s["day_open"], s["day_note"] = open_time, note

        days[date] = {"open": open_time, "note": note, "showings": showings}
        polite_sleep()

    return days


def merge_schedule(old: dict, days: dict) -> tuple[dict, list[str]]:
    """今回実際に見た日付だけを差し替える。

    見ていない日付まで作り直すと、軽量同期のたびに先の日程が消える。
    """
    old_days = old.get("days", {})
    merged: dict[str, dict] = dict(old_days)
    changes: list[str] = []

    for date, payload in days.items():
        prev = {s["id"]: s for s in old_days.get(date, {}).get("showings", [])}
        rows = []
        for s in payload["showings"]:
            if s["id"] in prev:
                keep = prev[s["id"]]
                for field in ("status", "attempts", "captures", "last_attempt_at"):
                    s[field] = keep.get(field, s[field])
            rows.append(s)

        if date in old_days:      # 既知の日付が変わった＝週途中の改訂かもしれない
            before, after = set(prev), {s["id"] for s in rows}
            changes += [f"{date} +{sid}" for sid in sorted(after - before)]
            changes += [f"{date} -{sid}" for sid in sorted(before - after)
                        if prev[sid].get("status") in ("pending", "retry", "provisional")]

        merged[date] = {**payload, "showings": rows}

    cutoff = (now_jst() - timedelta(days=KEEP_PAST_DAYS)).date().isoformat()
    merged = {d: p for d, p in merged.items() if d >= cutoff}
    return {"theater": THEATER, "fetched_at": now_jst().isoformat(),
            "days": dict(sorted(merged.items()))}, changes


# ═══════════════════════════════════════════════════════════════════
#  座席取得
# ═══════════════════════════════════════════════════════════════════

def should_capture(showing: dict, lead: float) -> bool:
    """今このタイミングで撮るべきか。

    1 枚目は窓に入った瞬間に撮る（cron が次いつ来るか分からないので保険）。
    2 枚目以降は開映が近づくまで待つ。直前に買う客が少なくないため、
    遅く撮るほど実態に近い。
    """
    n = showing.get("captures", 0)
    if n == 0:
        return True
    if n >= MAX_CAPTURES:
        return True # False
    return lead <= TARGET_LEAD_MIN


def due_showings(schedule: dict, now: datetime, lead_min: int) -> list[dict]:
    out = []
    for payload in schedule.get("days", {}).values():
        for s in payload.get("showings", []):
            if s.get("status") not in ("pending", "retry", "provisional"):
                continue
            if s.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            try:
                start = datetime.fromisoformat(s["start_at"])
            except (ValueError, KeyError):
                continue
            lead = (start - now).total_seconds() / 60
            if -LATE_GRACE_MIN <= lead <= lead_min and should_capture(s, lead):
                out.append(s)
    out.sort(key=lambda s: (s["start_at"], s["screen_no"]))
    return out


def finalize_expired(schedule: dict, now: datetime) -> tuple[set[str], list[str]]:
    """窓を過ぎた回の後始末。

    provisional は既に撮って保存済み（もっと開映に近い読みを待っていただけ）
    なので、そのまま確定として行列から外す。pending / retry は本当に撮れて
    いないので missed にする。
    """
    finalized: set[str] = set()
    missed: list[str] = []
    for payload in schedule.get("days", {}).values():
        for s in payload.get("showings", []):
            status = s.get("status")
            if status not in ("pending", "retry", "provisional"):
                continue
            try:
                start = datetime.fromisoformat(s["start_at"])
            except (ValueError, KeyError):
                continue
            if (now - start).total_seconds() / 60 <= LATE_GRACE_MIN:
                continue
            if status == "provisional":
                finalized.add(s["id"])
            else:
                s["status"] = "missed"
                missed.append(f"{s['date'][5:]} {s['start']}")
    return finalized, missed


def open_seat_page(page, showing: dict):
    """座席選択画面へ移動する。

    予約先は一覧ページの onclick に書かれた普通の GET リンク。
    クリックを真似る必要はない（折りたたみの中の要素はそもそも押せない）。
    """
    url = showing.get("reserve_url")
    if not url:
        return None
    try:
        resp = page.goto(url, wait_until="domcontentloaded",
                         referer=THEATER["schedule_url"], timeout=NAV_TIMEOUT_MS)
        check_response(resp, url)
    except Blocked:
        raise
    except Exception:
        return None

    for _ in range(30):          # 中継ページや待合室を挟むことがある
        if THEATER["seat_url_hint"] in page.url:
            page.wait_for_timeout(1200)
            return page
        page.wait_for_timeout(500)
    return None


def read_seat_page(page, shot_path: Path | None, expected_seats: int | None) -> dict:
    page.wait_for_timeout(1500)
    record: dict = {}

    loc, _ = first_match(page, SELECTORS["movie_date"])
    if loc is not None:
        try:
            record["movie_date_text"] = (loc.first.inner_text(timeout=3000) or "").strip()
        except Exception:
            pass

    # img.img-fluid は 26 個ある（アイコンやボタンも含む）。作品画像は _up/cinema/ 配下。
    try:
        record["poster_url"] = page.evaluate(r"""
          () => Array.from(document.images).map(i => i.src)
                   .find(s => /images\/_up\/cinema\//.test(s)) || null""")
        record["screen_image_url"] = page.evaluate(r"""
          () => Array.from(document.images).map(i => i.src)
                   .find(s => /images\/_up\/theater\//.test(s)) || null""")
    except Exception:
        pass

    try:
        vacant = page.locator(SEAT_VACANT).count()
    except Exception:
        vacant = 0
    try:
        sold = page.locator(SEAT_SOLD).count()
    except Exception:
        sold = 0
    total = vacant + sold
    record["seat_counts"] = {"vacant": vacant, "sold": sold,
                             "total": total, "expected": expected_seats}
    record["occupancy"] = round(sold / total, 4) if total else None

    # 見張り：合計が公称座席数と合わなければ構造変化を疑う
    if total == 0:
        record["seat_warning"] = "座席が 0 件"
    elif expected_seats and total != expected_seats:
        record["seat_warning"] = f"座席数 {total} ≠ 公称 {expected_seats}"

    if shot_path is not None:
        container, _ = first_match(page, SELECTORS["seat_container"])
        shot_path.parent.mkdir(parents=True, exist_ok=True)
        opts = {"path": str(shot_path), "timeout": 15_000,
                "type": "jpeg", "quality": SEAT_SHOT_QUALITY}
        try:
            if container is not None:
                container.first.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(400)
                container.first.screenshot(**opts)
            else:
                page.screenshot(full_page=True, **opts)
            record["seat_image"] = rel(shot_path)
            record["seat_image_bytes"] = shot_path.stat().st_size
        except Exception as exc:
            LOG.warning("座席図の撮影に失敗: %s", exc)
    return record


def download(page, url: str | None, dest: Path,
             dedupe_dir: Path | None = None, prefix: str = "") -> str | None:
    if not url:
        return None
    try:
        resp = page.context.request.get(url, timeout=20_000)
        if not resp.ok:
            return None
        body = resp.body()
    except Exception:
        return None

    if dedupe_dir is not None:
        digest = hashlib.sha256(body).hexdigest()[:8]
        ext = Path(url.split("?")[0]).suffix or ".png"
        dest = dedupe_dir / f"{prefix}{digest}{ext}"
        if dest.exists():
            return rel(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return rel(dest)


# ═══════════════════════════════════════════════════════════════════
#  保存
# ═══════════════════════════════════════════════════════════════════

# 記録には 2 種類ある。
#   source="schedule" … 一覧ページから作った予定だけの記録（画像なし）
#   source="seat"     … 座席選択画面まで入って撮った記録
# 同じ id で衝突したらこの順位で勝ち負けを決める。純粋な比較なので、
# ローカルとクラウドがどちらの順で合流しても同じ結果になる（冪等）。
STATUS_RANK = {"pending": 0, "retry": 1, "missed": 2, "failed": 2,
               "provisional": 3, "captured": 4}


def record_source(rec: dict) -> str:
    """記録の種別。`source` が無い古い記録も中身から判定する。

    ここを `rec.get("source") == "seat"` だけで判定していたため、
    source 欄を持たない旧記録が schedule 扱いになり、あとから作った
    予定だけの記録に上書きされて実際の撮影結果が消えた。
    中身（画像・座席数・撮影時刻）があるなら、それは撮影済みの記録。
    """
    if rec.get("source"):
        return rec["source"]
    if (rec.get("seat_image") or rec.get("occupancy") is not None
            or rec.get("lead_minutes") is not None or rec.get("seat_counts")):
        return "seat"
    return "schedule"


def record_rank(rec: dict) -> tuple:
    """大きいほど優先。seat > schedule、seat 同士は開映に近いほうが勝つ。

    段が違えば下の段は決して上を追い越さない。つまり予定だけの記録が
    撮影済みの記録を上書きすることは、どんな時刻であってもあり得ない。
    """
    if record_source(rec) == "seat":
        lead = rec.get("lead_minutes")
        return (1, -(lead if lead is not None else 9999), rec.get("captured_at") or "")
    return (0, 0, rec.get("updated_at") or "")


def archive_many(records: list[dict]) -> int:
    """月ファイルへまとめて反映する。既存より優先度が低い記録は捨てる。"""
    if not records:
        return 0
    written = 0
    by_month: dict[str, list[dict]] = {}
    for r in records:
        by_month.setdefault(r["date"][:7], []).append(r)

    for month, batch in by_month.items():
        path = CAPTURES / f"{month}.json"
        data = read_json(path, {"month": month, "captures": []})
        index = {c["id"]: c for c in data["captures"]}
        changed = False
        for rec in batch:
            cur = index.get(rec["id"])
            if cur is None or record_rank(rec) > record_rank(cur):
                index[rec["id"]] = rec
                changed = True
                written += 1
        if not changed:
            continue
        data["captures"] = sorted(index.values(),
                                  key=lambda c: (c["date"], c["start"], c["screen_no"]))
        data["updated_at"] = now_jst().isoformat()
        write_json(path, data)
    return written


def archive_capture(record: dict) -> None:
    archive_many([record])


def schedule_records(days: dict) -> list[dict]:
    """一覧ページの情報だけで記録を作る。

    座席表が撮れなくても「その回が存在した」ことと空席記号（◎○△満席）は
    残る。あとで実際に撮れたら source="seat" の記録が上書きする。
    """
    stamp = now_jst().isoformat()
    out = []
    for payload in days.values():
        for s in payload.get("showings", []):
            rec = {k: v for k, v in s.items()
                   if k not in ("status", "attempts", "captures", "last_attempt_at")}
            rec.update(source="schedule", updated_at=stamp,
                       theater=THEATER["id"], theater_name=THEATER["name"],
                       seat_image=None, occupancy=None)
            out.append(rec)
    return out


def build_films(records: list[dict]) -> dict:
    """作品台帳を記録から**毎回作り直す**。

    以前は撮るたびに capture_count += 1 する増分方式だったが、
    カウンタは合流できない（両側で 1 ずつ増えたとき、和なのか max なのか
    判断できない）。派生物にしておけば、合流時は捨てて作り直すだけで済む。
    """
    films: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: (x["date"], x["start"])):
        key = r["film_key"]
        e = films.setdefault(key, {
            "key": key, "title": r["film_title"], "poster": None, "tags": [],
            "runtime_min": r.get("runtime_min"), "codes": [],
            "first_seen": r["date"], "last_seen": r["date"],
            "showing_count": 0, "capture_count": 0,
        })
        if r.get("poster"):
            e["poster"] = r["poster"]
        for t in r.get("tags", []):
            if t not in e["tags"]:
                e["tags"].append(t)
        if r.get("film_code") and r["film_code"] not in e["codes"]:
            e["codes"].append(r["film_code"])
        if r.get("runtime_min"):
            e["runtime_min"] = r["runtime_min"]
        e["first_seen"] = min(e["first_seen"], r["date"])
        e["last_seen"] = max(e["last_seen"], r["date"])
        e["showing_count"] += 1
        if record_source(r) == "seat":
            e["capture_count"] += 1
    return films


def all_records() -> list[dict]:
    out = []
    for path in sorted(CAPTURES.glob("*.json")):
        out += read_json(path, {}).get("captures", [])
    return out


def drop_from_queue(schedule: dict, ids: set[str]) -> None:
    for payload in schedule.get("days", {}).values():
        payload["showings"] = [s for s in payload.get("showings", []) if s["id"] not in ids]


def rebuild_index() -> None:
    """films.json と index.json を captures/ から作り直す。

    どちらも派生物なので、合流のときは捨ててここで作り直せばよい。
    """
    CAPTURES.mkdir(parents=True, exist_ok=True)
    records = all_records()
    total = len(records)
    seat_total = sum(1 for r in records if record_source(r) == "seat")

    # 見張り：記録数が減るのは異常。復元失敗や月ファイル欠落を index に
    # 伝播させると、フロントから記録が消えたように見える。
    prev_idx = read_json(INDEX_FILE, {})
    prev_total = prev_idx.get("total_records", 0)
    prev_seat = prev_idx.get("total_captures", 0)
    if total < prev_total or seat_total < prev_seat:
        EV.add("WARN", "index",
               f"記録が減少 {prev_total}→{total}（うち撮影済み {prev_seat}→{seat_total}）。中断")
        raise RuntimeError(
            f"記録数の減少を検知（全体 {prev_total}→{total}、"
            f"撮影済み {prev_seat}→{seat_total}）。data/captures/ を確認すること")

    films = build_films(records)
    write_json(FILMS_FILE, films)

    months, dates = [], {}
    for path in sorted(CAPTURES.glob("*.json")):
        caps = read_json(path, {}).get("captures", [])
        if caps:
            months.append({"month": path.stem, "file": rel(path), "count": len(caps)})
    code2key: dict[str, set[str]] = {}
    for c in records:
        d = dates.setdefault(c["date"], {"date": c["date"], "count": 0,
                                         "captured": 0, "films": []})
        d["count"] += 1
        if record_source(c) == "seat":
            d["captured"] += 1
        if c["film_key"] not in d["films"]:
            d["films"].append(c["film_key"])
        if c.get("film_code"):
            code2key.setdefault(c["film_code"], set()).add(c["film_key"])

    # 同じ作品番号が複数キーに散る＝1 作品が分裂している。
    # 逆（1 作品に複数番号）は版違いなので正常であり、警告しない。
    warnings = [f"作品番号 {code} が複数キーに分裂: {sorted(keys)}"
                for code, keys in code2key.items() if len(keys) > 1]
    for w in warnings:
        EV.add("WARN", "films", w)

    schedule = read_json(SCHEDULE_FILE, {})
    pending = sum(1 for p in schedule.get("days", {}).values()
                  for s in p.get("showings", []) if s.get("status") in ("pending", "retry"))

    write_json(INDEX_FILE, {
        "theater": THEATER,
        "generated_at": now_jst().isoformat(),
        "total_records": total,        # 予定＋撮影済み
        "total_captures": seat_total,  # 実際に座席表を撮れた数
        "pending_showings": pending,
        "warnings": warnings,
        "months": months,
        "dates": sorted(dates.values(), key=lambda d: d["date"], reverse=True),
        "films": sorted(films.values(),
                        key=lambda f: (f.get("last_seen") or "", f["title"]), reverse=True),
    })



# ═══════════════════════════════════════════════════════════════════
#  リポジトリ同期（ローカルとクラウドのどちらから走らせても合流する）
# ═══════════════════════════════════════════════════════════════════
#
# 合流できるのは、記録の id が決定的（日付-時刻-シアター-作品）だから。
# 両側が独立に走っても同じ id を作るので、突き合わせは純粋な集合演算になる。
#
#   captures/*.json  id で和集合。衝突は record_rank（seat > schedule、
#                    seat 同士は開映に近いほう）で決める
#   media/           勝った記録の側のファイルを採用。片側にしか無ければ持ってくる
#   schedule.json    id で和集合。状態は進んだほう、回数は max
#   state.json       時刻は新しいほう、変更履歴は連結
#   logs/            行の和集合を時刻順に
#   films/index.json 合流しない。捨てて作り直す
#
# どの規則も可換かつ冪等なので、順番が違っても、二度流しても同じ結果になる。

DATA_BRANCH = "cinema-data"
SYNC_PATHS = ("data", "media", "logs")


def git(*args: str, cwd: Path | None = None, check: bool = True,
        binary: bool = False, stdin: bytes = b""):
    """git を呼ぶ。

    encoding を明示するのが要点。text=True だけだと Windows は既定の
    コードページ（日本語環境なら cp932、中国語環境なら gbk）で復号しようとし、
    日本語のファイル名を含む出力で UnicodeDecodeError になる。しかも例外は
    読み取りスレッド側で起きるため戻り値は空文字になり、
    `git status --porcelain` が空＝「変更なし」と誤判定して push を黙って
    やめる、という気づきにくい壊れ方をする。
    """
    import subprocess
    r = subprocess.run(
        ["git", *args], cwd=str(cwd or ROOT), input=stdin if binary else stdin.decode(),
        capture_output=True,
        **({} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}))
    if check and r.returncode != 0:
        err = r.stderr if not binary else (r.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args)} 失敗: {(err or '').strip()[:200]}")
    return r.stdout


def remote_ref() -> str:
    return f"origin/{DATA_BRANCH}"


def remote_exists() -> bool:
    out = git("ls-remote", "--heads", "origin", DATA_BRANCH, check=False)
    return bool(out and out.strip())


def remote_files() -> list[str]:
    # -z で NUL 区切りにすると、日本語などのパスが \346\234 形式に
    # エスケープされず、そのまま UTF-8 で返ってくる
    out = git("ls-tree", "-r", "-z", "--name-only", remote_ref(), "--",
              *SYNC_PATHS, check=False)
    if out:
        return [p for p in out.split("\0") if p.strip()]
    return []


def remote_json(path: str):
    out = git("show", f"{remote_ref()}:{path}", check=False)
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        EV.add("WARN", "merge", f"リモートの {path} が壊れている。無視する")
        return None


def _merge_captures(remote_paths: list[str]) -> set[str]:
    """月ファイルを突き合わせる。戻り値はリモートから取り直すべき画像のパス。"""
    take_remote: set[str] = set()
    months = {p.stem for p in CAPTURES.glob("*.json")}
    months |= {Path(p).stem for p in remote_paths if p.startswith("data/captures/")}

    for month in sorted(months):
        local_path = CAPTURES / f"{month}.json"
        local = read_json(local_path, {"month": month, "captures": []})
        remote = remote_json(f"data/captures/{month}.json")
        if remote is None:
            continue

        index = {c["id"]: c for c in local.get("captures", [])}
        changed = False
        for rec in remote.get("captures", []):
            cur = index.get(rec["id"])
            if cur is None or record_rank(rec) > record_rank(cur):
                index[rec["id"]] = rec
                changed = True
                if rec.get("seat_image"):
                    take_remote.add(rec["seat_image"])
        if changed:
            local["captures"] = sorted(index.values(),
                                       key=lambda c: (c["date"], c["start"], c["screen_no"]))
            local["month"] = month
            local["updated_at"] = now_jst().isoformat()
            write_json(local_path, local)
    return take_remote


def _merge_schedule_file() -> None:
    remote = remote_json("data/schedule.json")
    if remote is None:
        return
    local = read_json(SCHEDULE_FILE, {"days": {}})
    days = dict(local.get("days", {}))

    for date, payload in remote.get("days", {}).items():
        cur = {s["id"]: s for s in days.get(date, {}).get("showings", [])}
        for s in payload.get("showings", []):
            mine = cur.get(s["id"])
            if mine is None:
                cur[s["id"]] = s
                continue
            # 進んだほうの状態を採る（撮影済み > 暫定 > 未取得）
            if STATUS_RANK.get(s.get("status"), 0) > STATUS_RANK.get(mine.get("status"), 0):
                mine["status"] = s["status"]
            mine["captures"] = max(mine.get("captures", 0), s.get("captures", 0))
            mine["attempts"] = max(mine.get("attempts", 0), s.get("attempts", 0))
            for field in ("reserve_url", "schedule_id", "film_code"):
                if not mine.get(field) and s.get(field):
                    mine[field] = s[field]
        days[date] = {**payload, "showings": sorted(cur.values(),
                                                    key=lambda x: (x["start"], x["screen_no"]))}

    cutoff = (now_jst() - timedelta(days=KEEP_PAST_DAYS)).date().isoformat()
    days = {d: p for d, p in days.items() if d >= cutoff}
    fetched = max(filter(None, [local.get("fetched_at"), remote.get("fetched_at")]),
                  default=now_jst().isoformat())
    write_json(SCHEDULE_FILE, {"theater": THEATER, "fetched_at": fetched,
                               "days": dict(sorted(days.items()))})


def _merge_state() -> None:
    remote = remote_json("data/state.json")
    if remote is None:
        return
    local = load_state()
    for field in ("last_run_at", "cooldown_until"):
        a, b = local.get(field), remote.get(field)
        if b and (not a or b > a):
            local[field] = b
            if field == "cooldown_until":
                local["cooldown_reason"] = remote.get("cooldown_reason")
    seen, merged = set(), []
    for entry in (local.get("sync_changes", []) + remote.get("sync_changes", [])):
        if entry.get("at") in seen:
            continue
        seen.add(entry.get("at"))
        merged.append(entry)
    if merged:
        local["sync_changes"] = sorted(merged, key=lambda e: e.get("at") or "")[-40:]
    save_state(local)


def _merge_logs() -> None:
    out = git("show", f"{remote_ref()}:logs/cinema.log", check=False)
    if not out:
        return
    local = LOG_FILE.read_text(encoding="utf-8").splitlines() if LOG_FILE.exists() else []
    lines = sorted(set(local) | set(out.splitlines()))
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")


def _merge_media(remote_paths: list[str], take_remote: set[str]) -> int:
    """片側にしか無い画像を持ってくる。衝突は記録の勝者に従う。"""
    n = 0
    for path in remote_paths:
        if not path.startswith("media/"):
            continue
        local = ROOT / path
        if local.exists() and path not in take_remote:
            continue
        blob = git("show", f"{remote_ref()}:{path}", binary=True, check=False)
        if not blob:
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(blob)
        n += 1
    return n


def cmd_pull() -> None:
    """リモートのデータを取り込んで合流する。"""
    git("fetch", "origin", DATA_BRANCH, check=False)
    if not remote_exists():
        EV.add("SYNC", "pull", "リモートにデータブランチが無い")
        return
    paths = remote_files()
    take_remote = _merge_captures(paths)
    _merge_schedule_file()
    _merge_state()
    _merge_logs()
    media = _merge_media(paths, take_remote)
    rebuild_index()
    total = read_json(INDEX_FILE, {}).get("total_records", 0)
    EV.add("SYNC", "pull", f"合流完了  記録 {total} 件  画像 +{media}")


def cmd_push() -> int:
    """合流してから、データブランチへ push する。"""
    import shutil
    import tempfile

    git("fetch", "origin", DATA_BRANCH, check=False)
    if not remote_exists():
        # 空のツリーからデータ専用の孤児ブランチを作る（main の履歴を継がない）
        # /dev/null は Windows に無いので標準入力から空を渡す
        empty = git("hash-object", "-t", "tree", "--stdin").strip()
        commit = git("commit-tree", empty, "-m", "init data branch").strip()
        git("push", "origin", f"{commit}:refs/heads/{DATA_BRANCH}")
        git("fetch", "origin", DATA_BRANCH)
        EV.add("SYNC", "push", "データブランチを作成")

    for attempt in (1, 2, 3):
        cmd_pull()
        work = Path(tempfile.mkdtemp(prefix="cinema-push-"))
        shutil.rmtree(work)
        try:
            git("worktree", "add", "--detach", str(work), remote_ref())
            for name in SYNC_PATHS:
                srcdir = ROOT / name
                if not srcdir.exists():
                    continue
                for f in srcdir.rglob("*"):
                    if not f.is_file():
                        continue
                    dst = work / f.relative_to(ROOT)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)

            git("add", "-A", cwd=work)
            if not git("status", "--porcelain", "-z", cwd=work).strip():
                EV.add("SYNC", "push", "変更なし")
                return 0
            stamp = now_jst().strftime("%m-%d %H:%M")
            total = read_json(INDEX_FILE, {}).get("total_records", 0)
            git("commit", "-m", f"cinema {stamp} ({total} 件)", cwd=work)
            git("push", "origin", f"HEAD:{DATA_BRANCH}", cwd=work)
            EV.add("SYNC", "push", f"送信完了  記録 {total} 件")
            return 0
        except RuntimeError as exc:
            EV.add("SYNC", "retry", f"{attempt} 回目失敗: {str(exc)[:70]}")
            git("fetch", "origin", DATA_BRANCH, check=False)
            time.sleep(attempt * 5)
        finally:
            git("worktree", "remove", "--force", str(work), check=False)

    EV.add("SYNC", "push", "3 回とも失敗")
    return 1


# ═══════════════════════════════════════════════════════════════════
#  コマンド
# ═══════════════════════════════════════════════════════════════════

def cmd_sync(force: bool = False, full: bool = False) -> None:
    schedule = read_json(SCHEDULE_FILE, {"days": {}})
    fetched = schedule.get("fetched_at")
    if not force and fetched:
        try:
            age = (now_jst() - datetime.fromisoformat(fetched)).total_seconds() / 60
            if age < SCHEDULE_TTL_MIN:
                EV.add("SYNC", "skip", f"{fmt_duration(age)} 前に取得済み")
                return
        except ValueError:
            pass

    pw, browser, ctx = browser_session()
    try:
        ctx.image_flags["images"] = False        # 一覧は本文と属性しか要らない
        days = scrape_schedule(ctx.new_page(), None if full else LIGHT_SYNC_TABS)
    finally:
        ctx.close(); browser.close(); pw.stop()

    if not days:
        EV.add("SYNC", "empty", "1 件も取れなかった（既存データを維持）")
        return

    merged, changes = merge_schedule(schedule, days)
    write_json(SCHEDULE_FILE, merged)
    shows = sum(len(d["showings"]) for d in days.values())

    # 一覧の情報だけで先に記録を作っておく。座席表が撮れなくても
    # 「その回があった」ことと空席記号は残る。
    seeded = archive_many(schedule_records(days))
    rebuild_index()

    EV.add("SYNC", "full" if full else "light",
           f"{len(days)}日 {shows}件  予定記録 {seeded} 件"
           + (f"   変更 {len(changes)} 件" if changes else ""))

    if changes:
        for c in changes[:8]:
            EV.add("SYNC", "change", c)
        state = load_state()
        log = state.setdefault("sync_changes", [])
        log.append({"at": now_jst().isoformat(), "changes": changes})
        state["sync_changes"] = log[-40:]
        save_state(state)


def cmd_capture(lead_min: int = CAPTURE_LEAD_MIN, limit: int | None = None,
                dry_run: bool = False) -> None:
    schedule = read_json(SCHEDULE_FILE, {"days": {}})
    now = now_jst()
    due = due_showings(schedule, now, lead_min)
    if limit:
        due = due[:limit]

    if not due:
        finalized, missed = finalize_expired(schedule, now)
        if missed:
            EV.add("MISS", str(len(missed)), " ".join(missed[:8]))
        if (finalized or missed) and not dry_run:
            drop_from_queue(schedule, finalized)
            write_json(SCHEDULE_FILE, schedule)
        EV.add("CAP", "idle", "対象なし")
        return

    done: set[str] = set()
    ok = fail = 0
    t0 = time.time()

    pw, browser, ctx = browser_session()
    try:
        ctx.image_flags["images"] = False
        page = ctx.new_page()
        resp = page.goto(THEATER["schedule_url"], wait_until="domcontentloaded",
                         timeout=NAV_TIMEOUT_MS)
        check_response(resp, THEATER["schedule_url"])   # 先に cookie / session を作る
        page.wait_for_timeout(2000)
        ctx.image_flags["images"] = True     # 座席図と販売済マークは撮影対象

        for s in due:
            s["last_attempt_at"] = now_jst().isoformat()
            try:
                seat_page = open_seat_page(page, s)
                if seat_page is None:
                    s["attempts"] += 1
                    s["status"] = "retry" if s["attempts"] < MAX_ATTEMPTS else "failed"
                    fail += 1
                    EV.add("CAP", "no-page",
                           f"{s['date'][5:]} {s['start']} {s['film_title'][:14]}"
                           + ("" if s.get("reserve_url") else "  予約リンク無し"))
                    continue

                shot = None
                if SAVE_SEAT_SHOT and not dry_run:
                    name = (f"{s['film_key']}_{s['date'].replace('-', '')}"
                            f"_{s['start'].replace(':', '')}_{THEATER['id']}.jpg")
                    shot = SEATS / s["date"][:7] / name

                rec = read_seat_page(seat_page, shot, s.get("screen_seats"))
                lead = round(
                    (datetime.fromisoformat(s["start_at"]) - now_jst()).total_seconds() / 60, 1)

                poster = None
                if not dry_run and rec.get("poster_url"):
                    p = POSTERS / f"{s['film_key']}.jpg"
                    poster = rel(p) if p.exists() else download(seat_page, rec["poster_url"], p)
                screen_img = None
                if not dry_run:
                    screen_img = download(
                        seat_page, rec.get("screen_image_url"), SCREENS / "tmp.png",
                        dedupe_dir=SCREENS if DEDUPE_SCREENS else None,
                        prefix=f"{THEATER['id']}_{s['screen_no']}_")

                s["captures"] += 1
                final = lead <= FINAL_LEAD_MIN or s["captures"] >= MAX_CAPTURES
                record = {**s, **rec, "poster": poster, "screen_image": screen_img,
                          "source": "seat",
                          "lead_minutes": lead, "captured_at": now_jst().isoformat(),
                          "capture_round": s["captures"],
                          "status": "captured" if final else "provisional",
                          "theater": THEATER["id"], "theater_name": THEATER["name"]}

                if not dry_run:
                    archive_capture(record)
                s["status"] = "captured" if final else "provisional"
                if final:
                    done.add(s["id"])
                ok += 1

                occ = rec.get("occupancy")
                EV.add("CAP", "final" if final else f"{s['captures']}/{MAX_CAPTURES}",
                       f"{s['date'][5:]} {s['start']} s{s['screen_no']} "
                       f"{s['film_title'][:14]} lead={lead:.0f}m "
                       f"occ={f'{occ:.0%}' if occ is not None else '?'}"
                       + (f"   ⚠ {rec['seat_warning']}" if rec.get("seat_warning") else ""))

            except Blocked:
                raise                        # 制限されたら押し切らずに全体を止める
            except Exception as exc:
                s["attempts"] += 1
                s["status"] = "retry" if s["attempts"] < MAX_ATTEMPTS else "failed"
                fail += 1
                EV.add("CAP", "error", f"{s['date'][5:]} {s['start']} {str(exc)[:60]}")
            finally:
                polite_sleep()
    finally:
        ctx.close(); browser.close(); pw.stop()

    finalized, missed = finalize_expired(schedule, now_jst())
    if missed:
        EV.add("MISS", str(len(missed)), " ".join(missed[:8]))
    if not dry_run:
        drop_from_queue(schedule, done | finalized)
        write_json(SCHEDULE_FILE, schedule)
        rebuild_index()

    EV.add("CAP", "done", f"対象 {len(due)} / 成功 {ok} / 失敗 {fail} / {time.time() - t0:.0f}s"
                          + ("   [dry-run]" if dry_run else ""))



def cmd_repair() -> int:
    """media/seats/ に残っている画像を記録に繋ぎ直す。

    画像はどこでも削除されないので、JSON 側の関連が失われても
    ディスクには残っている。ファイル名から日付・時刻・作品を復元し、
    対応する記録を撮影済みに戻す。

    撮影時刻は**画像ファイルの更新時刻**から取る。ファイルが書かれた瞬間が
    撮影した瞬間なので、これが唯一の手掛かりになる。そこから開映までの
    分数も逆算できる。ただし pull で持ってきた画像は書き直された時点の
    時刻になってしまうため、開映前 24 時間〜開映直後という常識的な範囲に
    収まらなければ採用せず、時刻不明として残す。座席数と埋席率は
    どこにも残っていないので復元できない。
    """
    pattern = re.compile(r"^(?P<film>.+)_(?P<date>\d{8})_(?P<time>\d{4})_(?P<th>\w+)\.jpe?g$",
                         re.IGNORECASE)
    images: dict[tuple[str, str, str], Path] = {}
    for img in SEATS.rglob("*.jp*g"):
        m = pattern.match(img.name)
        if not m:
            EV.add("WARN", "repair", f"名前を解釈できない: {img.name}")
            continue
        d = m.group("date")
        key = (f"{d[:4]}-{d[4:6]}-{d[6:]}",
               f"{m.group('time')[:2]}:{m.group('time')[2:]}",
               m.group("film"))
        images[key] = img

    if not images:
        print("media/seats/ に画像がありません")
        return 0

    fixed = timed = 0
    for path in sorted(CAPTURES.glob("*.json")):
        data = read_json(path, {"captures": []})
        changed = False
        for rec in data.get("captures", []):
            key = (rec["date"], rec["start"], rec["film_key"])
            img = images.get(key)
            # 既に正規の撮影記録なら触らない。前回の復元分はやり直す
            if img is None or (record_source(rec) == "seat" and not rec.get("repaired")):
                continue
            images.pop(key, None)

            rec["source"] = "seat"
            rec["seat_image"] = rel(img)
            rec["seat_image_bytes"] = img.stat().st_size
            rec["repaired"] = True

            stamp = datetime.fromtimestamp(img.stat().st_mtime, JST)
            lead = None
            try:
                start = datetime.fromisoformat(rec["start_at"])
                lead = (start - stamp).total_seconds() / 60
            except (KeyError, ValueError):
                pass
            if lead is not None and -LATE_GRACE_MIN <= lead <= 24 * 60:
                rec["captured_at"] = stamp.replace(microsecond=0).isoformat()
                rec["lead_minutes"] = round(lead, 1)
                timed += 1
            else:
                # 画像の時刻が信用できない（pull で書き直された等）
                rec.pop("captured_at", None)
                rec.pop("lead_minutes", None)
            changed = True
            fixed += 1
        if changed:
            data["updated_at"] = now_jst().isoformat()
            write_json(path, data)

    rebuild_index()
    EV.add("SYNC", "repair",
           f"{fixed} 件に画像を復元（うち撮影時刻も復元 {timed} 件）"
           + (f"  対応する記録が無い画像 {len(images)} 枚" if images else ""))
    for key in list(images)[:10]:
        print(f"  記録なし: {key[0]} {key[1]} {key[2]}")
    return 0


def cmd_doctor() -> int:
    """環境と取得経路を一通り確認する。データは一切書かない。"""
    print("\n=== cinema.py doctor ===\n")
    ng = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal ng
        print(f"  {'OK ' if ok else 'NG '} {label:<24} {detail}")
        if not ok:
            ng += 1

    try:
        import playwright                                            # noqa: F401
        check("Playwright", True)
    except ImportError:
        check("Playwright", False, "pip install playwright / playwright install chromium")
        return 1

    check("robots.txt", robots_allows(THEATER["schedule_url"]), THEATER["robots_url"])
    cd = load_state().get("cooldown_until")
    check("クールダウン", not cd or datetime.fromisoformat(cd) < now_jst(), cd or "なし")

    pw, browser, ctx = browser_session()
    try:
        page = ctx.new_page()
        resp = page.goto(THEATER["schedule_url"], wait_until="domcontentloaded")
        check("一覧ページ", resp is None or resp.ok, f"HTTP {resp.status if resp else '?'}")
        page.wait_for_timeout(2500)

        for key in ("day_tabs", "film_block", "film_title", "showing_row"):
            loc, sel = first_match(page, SELECTORS[key])
            check(f"セレクタ {key}", loc is not None,
                  f"{sel} → {loc.count() if loc else 0} 件")

        rows = scrape_day(page, now_jst().date().isoformat())
        check("本日の解析", len(rows) > 10, f"{len(rows)} 件")
        if not rows:
            return 1

        titles = list(dict.fromkeys(r["film_title"] for r in rows))
        with_url = sum(1 for r in rows if r["reserve_url"])
        screens = sorted({r["screen_no"] for r in rows})
        print(f"      作品 {len(titles)} 本  例: {'、'.join(t[:16] for t in titles[:3])}")
        print(f"      シアター {' '.join(screens)}")
        check("予約リンク", with_url > 0, f"{with_url}/{len(rows)} 件（0 なら全回が窓口のみ）")

        target = next((r for r in rows if r["reserve_url"]), None)
        if target:
            ctx.image_flags["images"] = True
            seat = open_seat_page(page, target)
            check("座席ページ遷移", seat is not None, page.url)
            if seat:
                tmp = LOGS / "doctor_seat.jpg"
                rec = read_seat_page(seat, tmp, target.get("screen_seats"))
                c = rec["seat_counts"]
                check("座席カウント", c["total"] > 0 and not rec.get("seat_warning"),
                      f"空 {c['vacant']} + 売 {c['sold']} = {c['total']}"
                      f"（公称 {c['expected']}）")
                check("座席図の撮影", bool(rec.get("seat_image")),
                      f"{rec.get('seat_image_bytes', 0) / 1024:.0f} KB → {tmp}")
    finally:
        ctx.close(); browser.close(); pw.stop()

    print(f"\n  → {'すべて正常' if ng == 0 else f'{ng} 件の問題あり'}\n")
    return 0 if ng == 0 else 1


def cmd_log(n: int) -> int:
    if not LOG_FILE.exists():
        print("ログはまだありません")
        return 0
    print("\n".join(LOG_FILE.read_text(encoding="utf-8").splitlines()[-n:]))
    return 0


def cmd_inspect(url: str | None, live: str | None) -> None:
    """ページ構造を dump する。セレクタが効かなくなったときの調査用。"""
    dump = ROOT / "inspect"
    dump.mkdir(exist_ok=True)
    pw, browser, ctx = browser_session()
    pages = []
    try:
        page = ctx.new_page()
        page.goto(url or THEATER["schedule_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        pages.append(_dump(page, "schedule", dump))

        if live is not None:
            target = live or (now_jst() + timedelta(days=2)).date().isoformat()
            urls = []
            for m in RESERVE_RE.finditer(page.content()):
                u = "https://tjoy.jp" + m.group(0).replace("&amp;", "&")
                if u not in urls:
                    urls.append(u)
            print(f"予約リンク {len(urls)} 件（対象日 {target}）")
            for u in urls[:3]:
                if open_seat_page(page, {"reserve_url": u}):
                    pages.append(_dump(page, "seat", dump))
                    break
        write_json(dump / "report.json", {"pages": pages})
        print(f"{dump}/ に report.json と各ページの html / png を出力")
    finally:
        ctx.close(); browser.close(); pw.stop()


def _dump(page, name: str, dump: Path) -> dict:
    (dump / f"{name}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(dump / f"{name}.png"), full_page=True)
    rep = {"name": name, "url": page.url, "selectors": {}}
    for key, cands in SELECTORS.items():
        rep["selectors"][key] = [{"selector": s, "count": page.locator(s).count()}
                                 for s in cands]
    rep["class_histogram"] = page.evaluate(r"""
      () => {
        const h = {};
        for (const el of document.querySelectorAll('[class]'))
          for (const c of el.classList) h[c] = (h[c] || 0) + 1;
        return Object.fromEntries(Object.entries(h).sort((a,b)=>b[1]-a[1]).slice(0,150));
      }""")
    rep["seat_probe"] = page.evaluate(r"""
      () => {
        const at = e => Object.fromEntries(
          Array.from(e.attributes).map(a => [a.name, a.value.slice(0,90)]));
        const v = Array.from(document.querySelectorAll('area.seat-select'));
        const s = Array.from(document.querySelectorAll('img.sold-out, .sold-out'));
        return {vacant: v.length, sold: s.length,
                vacant_sample: v.slice(0,3).map(at), sold_sample: s.slice(0,3).map(at)};
      }""")
    return rep


# ═══════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(
        description="T・ジョイ梅田 座席表アーカイバ",
        epilog="まず `python cinema.py doctor` で環境を確認してください。")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "sync", "capture", "index", "doctor", "log",
                            "inspect", "pull", "push", "repair"])
    p.add_argument("--force", action="store_true", help="sync で TTL を無視する")
    p.add_argument("--full", action="store_true", help="sync で全日程を見る（既定は今日と明日）")
    p.add_argument("--lead", type=int, metavar="MIN",
                   help=f"capture の対象窓を一時的に変える（既定 {CAPTURE_LEAD_MIN} 分）")
    p.add_argument("--limit", type=int, metavar="N", help="capture で最初の N 件だけ処理する")
    p.add_argument("--dry-run", action="store_true", help="取得はするがファイルを書かない")
    p.add_argument("-n", type=int, default=40, help="log で表示する行数")
    p.add_argument("--url", help="inspect の対象 URL")
    p.add_argument("--live", nargs="?", const="", default=None, metavar="YYYY-MM-DD",
                   help="inspect で座席ページまで入る")
    p.add_argument("--no-sync", action="store_true",
                   help="run のとき、前後のリモート合流を行わない")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    for d in (DATA, CAPTURES, SEATS, POSTERS, SCREENS, LOGS):
        d.mkdir(parents=True, exist_ok=True)

    if args.command == "log":
        return cmd_log(args.n)
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "inspect":
        cmd_inspect(args.url, args.live)
        return 0
    if args.command == "index":
        rebuild_index()
        EV.flush()
        return 0
    if args.command == "repair":
        rc = cmd_repair()
        EV.flush()
        return rc
    if args.command == "pull":
        cmd_pull()
        EV.flush()
        return 0
    if args.command == "push":
        rc = cmd_push()
        EV.flush()
        return rc

    if in_cooldown():
        EV.flush()
        return 0
    if not robots_allows(THEATER["schedule_url"]):
        EV.add("RUN", "robots", "robots.txt により中止")
        EV.flush()
        return 0

    mark_run_start()
    sync_repo = args.command == "run" and not args.no_sync and not args.dry_run
    try:
        if sync_repo:
            cmd_pull()            # 走る前にリモートの成果を取り込む
        if args.command in ("run", "sync"):
            cmd_sync(force=args.force, full=args.full)
        if args.command in ("run", "capture"):
            cmd_capture(lead_min=args.lead if args.lead is not None else CAPTURE_LEAD_MIN,
                        limit=args.limit, dry_run=args.dry_run)
    except Blocked as exc:
        enter_cooldown(str(exc))
        EV.flush()
        return 0
    except Exception as exc:
        EV.add("RUN", "error", str(exc)[:120])
        EV.flush()
        LOG.exception("失敗")
        return 1

    if sync_repo:
        EV.flush()
        cmd_push()                # 走ったあとに合流して送り返す

    EV.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())