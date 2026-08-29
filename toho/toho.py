#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toho.py — TOHOシネマズ梅田（本館・別館）上映スケジュール & 座席表アーカイバ

各回の開映前に座席選択画面へ入り、その時点の埋まり具合を画像と数値で記録する。
cinema.py（T・ジョイ梅田）と同じ骨格だが、次の点が違う。

  * 1 つの「劇場コード」の下に **複数の館** がぶら下がる構造にした。
    TOHOシネマズ梅田は 1 ページ（コード 037）に本館スクリーン1〜8 と
    別館スクリーン9・10 が混在している。記録にはスクリーン番号から
    引いた館（venue）を必ず持たせる。
  * さらにその上に **複数の劇場** を並べられる（CINEMAS）。梅田となんばを
    1 つのリポジトリで回す、といった使い方を想定している。
  * TOHO 側のスケジュールは JavaScript で描画される。しかもクラス名が
    改装のたびに変わるので、**クラス名に依存しない抽出器**を使う
    （時刻レンジを含む最小の要素を上映回セルとみなす）。

    python toho.py doctor      まず これ。環境と取得経路を一通り自己診断する
    python toho.py run         通常運転（sync + capture）
    python toho.py inspect --live   構造が変わったときの調査用ダンプ

詳しい使い方は README.md、コマンド一覧は `python toho.py --help`。
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
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable
from urllib.robotparser import RobotFileParser

def _jst() -> tzinfo:
    """日本時間。tzdata が入っていない環境では固定の +09:00 で代用する。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Tokyo")
    except Exception:
        return timezone(timedelta(hours=9))


JST = _jst()


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
LOG_FILE = LOGS / "toho.log"

ORIGIN = "https://hlo.tohotheater.jp"

# --- 劇場と館 -------------------------------------------------------
#
# venues の screens は「そのスクリーン番号がどの館に属するか」の定義。
# 席数は公式「施設紹介」の値（車椅子席は含まない。括弧内は車椅子席）。
#
#   本館 SCREEN 1  724+(4)   SCREEN 5   96+(2)
#        SCREEN 2  471+(3)   SCREEN 6   99+(1)
#        SCREEN 3  466+(3)   SCREEN 7  132+(2)
#        SCREEN 4   99+(2)   SCREEN 8  152+(2)
#   別館 SCREEN 9  267+(2)   SCREEN 10 121+(2)
#
# 別館（スクリーン9・10）は耐震性の確認のため休館中。active:false にして
# あるので待ち行列には積まないが、再開したら true に戻すだけでよい。
CINEMAS = [
    {
        "id": "tohoumeda",
        "code": "037",
        "name": "TOHOシネマズ梅田",
        "url": f"{ORIGIN}/net/schedule/037/TNPI2000J01.do",
        "schedule_url": f"{ORIGIN}/net/schedule/037/TNPI2000J01.do",
        "robots_url": f"{ORIGIN}/robots.txt",
        "venues": [
            {"id": "honkan", "name": "本館", "active": True,
             "screens": ["01", "02", "03", "04", "05", "06", "07", "08"]},
            {"id": "bekkan", "name": "別館", "active": False,
             "note": "耐震性確認のため休館中（2026-08 時点）",
             "screens": ["09", "10"]},
        ],
        "screen_seats": {"01": 724, "02": 471, "03": 466, "04": 99, "05": 96,
                         "06": 99, "07": 132, "08": 152, "09": 267, "10": 121},
        "wheelchair_seats": {"01": 4, "02": 3, "03": 3, "04": 2, "05": 2,
                             "06": 1, "07": 2, "08": 2, "09": 2, "10": 2},
        "active": True,
    },
    # ── 2 館目を足すときはこの形で並べるだけ（例：TOHOシネマズなんば）──
    # {
    #     "id": "tohonamba",
    #     "code": "032",
    #     "name": "TOHOシネマズなんば",
    #     "url": f"{ORIGIN}/net/schedule/032/TNPI2000J01.do",
    #     "schedule_url": f"{ORIGIN}/net/schedule/032/TNPI2000J01.do",
    #     "robots_url": f"{ORIGIN}/robots.txt",
    #     "venues": [
    #         {"id": "honkan", "name": "本館", "active": True,
    #          "screens": ["01","02","03","04","05","06","07","08","09"]},
    #         {"id": "bekkan", "name": "別館", "active": True,
    #          "screens": ["10","11"]},
    #     ],
    #     "screen_seats": {},          # 施設紹介ページから埋める
    #     "active": True,
    # },
]

# --- 取得タイミング -------------------------------------------------
# 窓は cron の**実際の**間隔より広くないと、回が丸ごと漏れる。
# GitHub Actions の schedule は指定どおりには来ない（実測 14〜69 分間隔）。
# ローカル運転版。GitHub Actions の cron は指定どおりに来なかった
# （実測 14〜69 分間隔、数時間空くこともある）ので、「広い窓で待ち構える」
# のをやめ、**開映からの逆算で撮る時刻を決め打ち**する方式に変えた。
# 起こす役は hub.py。この 3 点を撮る。
#
#   slot 0 … 1 日 1 回の大範囲走査。今日から SWEEP_DAYS 日先までの全回に
#            ついて、その時点の座席を 1 枚（＝ベースライン）
#   slot 1 … 開映 30 分前
#   slot 2 … 開映  5 分前   ← 実態に一番近い読み
#
# 点を増やすなら CAPTURE_PLAN に足す（降順で書くこと）。
CAPTURE_PLAN = [30, 5]
PLAN_TOLERANCE_MIN = 2      # 起こされるのが多少早くても撮ってよい幅
                            # （hub.py の刻みより広く、点の間隔より狭く）
LATE_GRACE_MIN = 5          # 開映後の猶予
SWEEP_DAYS = 2              # 大範囲走査で何日先まで見るか（今日 + N 日）
SWEEP_DAYS_NIGHT = 1        # 深夜〜早朝はここまで（先の日付はまだ売っていない）
NIGHT_HOURS = (1, 7)        # 「深夜〜早朝」の範囲（JST の時、終端は含まない）


def sweep_days(now) -> int:
    """その時刻に走査で何日先まで見るか。

    01:00〜07:00 に回すぶんには、先の日付を舐めても実りが少ない。TOHO は
    3 日先までしか売らず、朝の販売開始前は翌々日の回に選座画面から入れない。
    空振りに数十分かけるより射程を 1 日縮めたほうが、同じ時間で今日と明日を
    丁寧に撮れる。日中に回したときは従来どおり 2 日先まで。
    """
    lo, hi = NIGHT_HOURS
    return SWEEP_DAYS_NIGHT if lo <= now.hour < hi else SWEEP_DAYS


# 座席図を計画点ごとに別ファイルで残すか。False なら 1 回につき 1 枚を
# 上書きし、数値だけが記録の history に全点ぶん残る（画像が 3 倍にならない）。
KEEP_SLOT_SHOTS = False

CAPTURE_LEAD_MIN = CAPTURE_PLAN[0] + PLAN_TOLERANCE_MIN   # capture の既定の窓
MAX_CAPTURES = 1 + len(CAPTURE_PLAN)

# --- 撮影対象の絞り込み ---------------------------------------------
# TOHO は 1 回撮るのに「一覧 → 日付タブ → 上映回 → 中間ページ → 座席表」の
# 5 手が要る。全 44 回を毎ラウンド追うと、購入フローへの出入りが 1 日で
# 数百回になる。相手にとっても自分にとっても重いので、要るものだけ撮る
# 余地を残してある。空の条件は「絞らない」の意味。
CAPTURE_ONLY: dict[str, Any] = {
    "screens": [],          # 例: ["01", "02", "03"] 大きい箱だけ
    "film_keys": [],        # 例: 特定作品の film_key
    "min_seats": None,      # 例: 400  これ未満の箱は撮らない
    "max_per_round": None,  # 例: 8    1 ラウンドで撮る上限
}
MAX_ATTEMPTS = 3            # 失敗の上限。超えたら failed にして行列から外す

# --- スケジュール同期 -----------------------------------------------
# 待ち行列に要るのは販売中の日だけ。実測では今日を含めて 3 日分が
# 「販売中」で、4 日目以降は全回「販売期間外」だった（8/29・30・31 が販売中、
# 9/1 以降は 44 件すべて販売期間外）。大範囲走査の射程と揃えておく。
SCHEDULE_TTL_MIN = 360

# --- 相手サイトへの配慮 ---------------------------------------------
REQUEST_DELAY_SEC = 3.0
REQUEST_JITTER_SEC = 2.0
COOLDOWN_MIN_ON_BLOCK = 180     # 403/429/5xx を踏んだら全面停止する時間
NAV_TIMEOUT_MS = 30_000
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BLOCK_RESOURCE_TYPES = {"media", "font", "websocket", "manifest"}
BLOCK_URL_KEYWORDS = (
    "google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
    "googleadservices", "adservice", "facebook.net", "facebook.com",
    "criteo", "rubiconproject", "amazon-adsystem", "hotjar", "clarity.ms",
    "newrelic", "nr-data.net", "sentry.io", "twitter.com", "youtube.com", "ytimg.com",
)

# --- 保存 -----------------------------------------------------------
SAVE_SEAT_SHOT = True       # False にすると数値だけ記録（リポジトリが太らない）
SEAT_SHOT_QUALITY = 80
DEDUPE_SCREENS = True
KEEP_PAST_DAYS = 2

# --- ログ -----------------------------------------------------------
MAX_LOG_LINES = 3000
GAP_WARN_MIN = 60


# ═══════════════════════════════════════════════════════════════════
#  サイト固有の知識
# ═══════════════════════════════════════════════════════════════════
#
# TOHO の上映スケジュールは JS で描画され、クラス名は改装のたびに変わる。
# そこで「クラス名を当てにいく」のをやめ、次の性質だけを頼りに抽出する。
#
#   * 1 回の上映は「10:00〜12:15」のような時刻レンジを含む
#   * その近くに SCREEN 番号がある
#   * 販売中なら /net/ticket/ へのリンクがある
#
# 下の SELECTORS は「当たれば使う」候補集合であって、外れても抽出は動く。
# 仕様変更のときは `python toho.py inspect` の class_histogram を見て
# ここに 1 行足せば済むようにしてある。

# 実測で確定した TOHO の構造（2026-08 時点）。
#
#   div.schedule-tab-item#20260828[.is-selected]      日付タブ（id が素の日付）
#   div.schedule-body-section-item#0371-029244        作品ブロック
#     h5.schedule-body-title                          作品名
#     .schedule-body-info .time                       [上映時間: 135分]
#     div.schedule-item                               1 回
#       p.time > span.start / span.end                10:00 ～ 12:35
#       p.status.is-status-04                         「販売期間外」など（文字が権威）
#       p.info                                        「特別料金」など
#       span.screen-name                              「本館スクリーン６ (100席)」
#
# 1 日分しか描画されない。日付を変えるにはタブを押す（Ajax で差し替わる）。
SELECTORS = {
    "day_tab": [".schedule-tab-item"],
    "day_tab_selected": [".schedule-tab-item.is-selected"],
    "film_block": [".schedule-body-section-item"],
    "film_title": [".schedule-body-title"],
    "showing": [".schedule-item"],
    "schedule_body": [".schedule-body", "#theater-schedule"],
    # 座席選択画面（未確定。inspect --live で確かめる）
    "seat_container": ["#seat-map", "[class*='seat-map']", "[class*='seatMap']",
                       "[class*='seatArea']", "[id*='seat']", "[class*='seat']"],
}

# 空席状況は p.status の **class** で決まる。文字は「販売中」「販売期間外」の
# 2 通りしか出ず、空席の程度は持っていない（実測：同じ「販売中」44 件が
# 01 / 02 / 03 に分かれた）。
#
# そして中の <i> のアイコン class が意味そのものを書いてくれている。
#   is-status-01  glyphicon-icon_circle-double  ◎
#   is-status-02  glyphicon-icon_circle         ○
#   is-status-03  glyphicon-icon_triangle       △
# 数字より図形名のほうが読み違えようがないので、こちらを第一の根拠にする。
# 数字コードは番号が振り直されたときのための第二候補として残す。
AVAIL_ICON_MAP = {
    "circle-double": ("plenty",    "◎", "空席に余裕あり", "確認"),
    "circle":        ("available", "○", "空席あり",       "確認"),
    "triangle":      ("few",       "△", "残りわずか",     "確認"),
    "cross":         ("full",      "×", "満席",           "推定"),
    "batsu":         ("full",      "×", "満席",           "推定"),
}

AVAIL_STATUS_MAP = {
    "01": ("plenty",    "◎", "空席に余裕あり", "確認"),
    "02": ("available", "○", "空席あり",       "確認"),
    "03": ("few",       "△", "残りわずか",     "確認"),
    "04": ("outside",   "…", "販売期間外",     "確認"),
    "05": ("full",      "×", "満席",           "推定"),
    "06": ("closed",    "—", "販売終了",       "推定"),
}

# class が両方とも未知だったときの保険。文字だけでも「売っているか」は分かる。
AVAIL_TEXT_RULES = [
    ("full",      ["満席"], "×", "満席"),
    ("few",       ["わずか", "残り少"], "△", "残りわずか"),
    ("available", ["空席あり", "空席", "販売中"], "○", "販売中"),
    ("closed",    ["販売終了", "上映終了", "受付終了"], "—", "販売終了"),
    ("outside",   ["販売期間外", "販売開始前", "販売前", "準備中"], "…", "販売期間外"),
    ("counter",   ["窓口", "劇場にて"], "窓", "劇場窓口のみ"),
]

ICON_RE = re.compile(r"glyphicon-icon_([a-z0-9_-]+)")

# 座席要素の状態判定。上から順に当てはめ、最初に一致した種別にする。
# 判定材料は className / id / src / alt / aria-label / title /
# background-image を連結した小文字の 1 本の文字列。
# ok / ng / on / off のような短い語は、前後が英字でないときだけ一致させる。
SEAT_STATE_RULES = [
    ("sold",   ["soldout", "sold_out", "sold-out", "sold", "reserved", "occupied",
                "nosel", "no-sel", "noselect", "disabled", "販売済", "満席",
                "選択不可", "予約済", "ng", "off"]),
    ("wheelchair", ["wheel", "wheelchair", "車椅子", "車いす"]),
    # 座席図はテーブル。通路や空きマスの <td> が座席と同数近く混ざる。
    # これを「判定できなかった座席」と混同すると常に警告が出続ける。
    ("spacer", ["blank", "spacer", "aisle", "通路", "空きマス", "seat-none"]),
    ("blocked", ["block", "closed", "空け", "使用不可", "利用不可", "空席なし"]),
    ("vacant", ["seat-select", "seatselect", "selectable", "available", "vacant",
                "empty", "空席", "選択可", "ok", "on"]),
]

# 座席表に着くまでに挟まる中間ページで押してよいボタン。
#
# ここは**許可した文言だけ**を押す。「次へ」の類を機械的に押し進めると、
# その先には購入確定がある。座席表を見るのに必要な 1〜2 手より先へは
# 絶対に進まないよう、文言を列挙する方式にしてある。
ADVANCE_BUTTON_TEXTS = [
    "ログインせずに購入する",
    "ログインせずに進む",
]
MAX_ADVANCE_STEPS = 2       # 中間ページを何枚まで通るか

# 押してはいけない文言（保険）。上の許可リストに無いものは押さないので
# 本来不要だが、許可リストを増やすときの歯止めとして明示しておく。
FORBIDDEN_BUTTON_TEXTS = ["購入", "決済", "確定", "支払", "予約する", "同意して購入"]

# 「本館スクリーン６ (100席)」を館・番号・席数に分ける。
# 数字は全角なので、判定の前に半角へ寄せる。席数はページ側の値が
# 車椅子席込みで、座席図に描かれる数と一致する（施設紹介の値より 1〜4 多い）。
SCREEN_NAME_RE = re.compile(
    r"(?P<venue>本館|別館|アネックス)?\s*(?:SCREEN|スクリーン)\s*(?P<no>\d{1,2})"
    r"(?:\s*[（(]\s*(?P<seats>\d{2,4})\s*席\s*[）)])?", re.I)

RUNTIME_RE = re.compile(r"上映時間\s*[:：]\s*(\d{1,3})\s*分")
SECTION_ID_RE = re.compile(r"^(?P<theater>\d{4})-(?P<sakuhin>\d{4,8})$")

TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[〜~～\-–—]\s*(\d{1,2}):(\d{2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
DATE8_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")

VERSION_TAGS = ["IMAX レーザー", "IMAXレーザー", "IMAX", "Dolby Cinema", "DolbyCinema",
                "Dolby Atmos", "DOLBY ATMOS", "ATMOS", "TCX", "MX4D", "4DX",
                "ScreenX", "SCREEN X", "轟音", "プレミアシアター",
                "プレミアボックスシート", "プレミアラグジュアリーシート",
                "字幕", "吹替", "日本語字幕", "2D", "3D", "バリアフリー",
                "レイトショー", "応援上映", "絶叫上映", "ライブビューイング"]


# ═══════════════════════════════════════════════════════════════════
#  ログ
# ═══════════════════════════════════════════════════════════════════

LOG = logging.getLogger("toho")


class EventLog:
    """要点だけを 1 行 1 件で残すログ。

    RUN 行の間隔を見れば cron が本当に回っているか一目で分かる。
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
        if len(merged) > MAX_LOG_LINES * 1.2:
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

    ここで黙って default を返すと、captures/2026-08.json が壊れたときに
    「その月は 0 件」として上書きしてしまい、1 か月分が消える。
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


def active_cinemas() -> list[dict]:
    return [c for c in CINEMAS if c.get("active", True)]


def cinema_by_id(cid: str) -> dict | None:
    return next((c for c in CINEMAS if c["id"] == cid), None)


def venue_of(cinema: dict, screen_no: str) -> dict:
    """スクリーン番号から館を引く。未知なら「不明」を返す（捨てない）。"""
    for v in cinema.get("venues", []):
        if screen_no in v.get("screens", []):
            return v
    return {"id": "unknown", "name": "館不明", "active": True, "screens": []}


# ── 作品名の解析 ───────────────────────────────────────────────────

_BRACKET_RE = re.compile(r"[【\[]([^】\]]*)[】\]]")

# 丸括弧の中身。〈〉《》は**外さない**——「まどか☆マギカ〈ワルプルギスの廻天〉」の
# ように作品名の一部であることが多い。丸括弧だけを、しかも中身が版の説明
# だと確認できたときだけ外す。
_PAREN_RE = re.compile(r"[（(]([^（()）]{1,40})[)）]")

# 丸括弧の中身がこれらを含むなら「版」の注記とみなす。
# 「（1954）」「（第1作）」のような本当のタイトルの一部は素通りする。
VERSION_PAREN_WORDS = [
    "字幕", "吹替", "日本語", "原語", "英語", "韓国語", "中国語",
    "上映", "応援", "絶叫", "発声", "スタンディング", "歌声",
    "ライブビューイング", "ディレイビューイング", "舞台挨拶", "先行",
    "IMAX", "4DX", "MX4D", "ScreenX", "ATMOS", "Dolby", "TCX", "轟音",
    "3D", "2D", "4K", "デジタルリマスター", "リマスター",
]
_RATING_RE = re.compile(r"PG-?12|R15\+|R18\+|G指定")
_NOTE_RE = re.compile(r"[0-9]{1,2}[./][0-9]{1,2}\s*[（(][月火水木金土日][）)]\s*"
                      r"(?:上映終了|上映開始|公開)")


# 全角英数 → 半角。判定にだけ使い、表示するタイトルには適用しない。
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}


def to_hankaku_full(text: str) -> str:
    """全角英数記号をすべて半角へ。鍵の計算にだけ使う。"""
    return (text or "").replace("\u3000", " ").translate(_FULLWIDTH)


def _canon(tag: str) -> str:
    """版タグ照合用の正規化。

    TOHO は【ＩＭＡＸ】のように全角で書く。ここで畳まないと IMAX と
    一致せず、タグが 1 つも取れない。タイトル本体には掛けないこと
    （「奇々怪々！」のような表記が壊れる）。
    """
    return (tag.translate(_FULLWIDTH)
            .replace(" ", "").replace("　", "").replace("・", "").replace("-", "")
            .replace("ー", "").lower())


def _is_version_paren(inner: str) -> bool:
    c = _canon(inner)
    return any(_canon(w) in c for w in VERSION_PAREN_WORDS)


def trim_tags(tags: list[str]) -> list[str]:
    """同じ版を指すタグの重なりを均す。

    「ATMOS」は「Dolby Atmos」に丸ごと含まれる。「字幕」は「日本語字幕」に、
    「IMAX」は「IMAX レーザー」に含まれる。片方がもう片方の中に入っている
    なら、長いほうだけを残す。
    """
    return [tag for tag in tags
            if not any(other is not tag and _canon(tag) != _canon(other)
                       and _canon(tag) in _canon(other) for other in tags)]


def parse_title(raw: str, extra_tags: Iterable[str] = ()) -> dict:
    """作品名から版タグを剥がす。

    タイトル全体に NFKC をかけないこと。「奇々怪々！」が「奇々怪々!」に
    化ける。正規表現側で全角半角どちらも拾うようにしてある。

    TOHO は版を 2 通りで書く。
      * 見出しの頭に【ＩＭＡＸ】
      * 見出しの末尾に（吹替版）（発声＆スタンディング応援上映）
    どちらも剥がしてタグにする。剥がさないと、同じ作品が字幕版・吹替版・
    IMAX 版で別作品として台帳に並んでしまう。

    ただし〈〉《》は触らない。「まどか☆マギカ〈ワルプルギスの廻天〉」の
    ように作品名そのものであることが多い。丸括弧も、中身が版の説明だと
    確かめてからでないと外さない（「（1954）」まで消してしまう）。
    """
    text = _NOTE_RE.sub("", (raw or "").strip())
    tags: list[str] = []

    def add(tag: str) -> None:
        if tag and _canon(tag) not in {_canon(t) for t in tags}:
            tags.append(tag)

    for inner in _BRACKET_RE.findall(text):
        for tag in VERSION_TAGS:
            if _canon(tag) in _canon(inner):
                add(tag)

    changed = True
    while changed:                       # 先頭の【…】と裸の版タグを剥がし切る
        changed = False
        stripped = re.sub(r"^\s*[【\[][^】\]]*[】\]]", "", text)
        if stripped != text:
            text, changed = stripped, True
            continue
        head = text.lstrip()
        for tag in sorted(VERSION_TAGS, key=len, reverse=True):
            if _canon(head).startswith(_canon(tag)):
                add(tag)
                text, changed = head[len(tag):], True
                break

    # 末尾（や途中）の（吹替版）などを剥がす。中身が版の説明のときだけ。
    def _strip_paren(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not _is_version_paren(inner):
            return m.group(0)
        for tag in VERSION_TAGS:         # 正規のタグ名だけを拾う
            if _canon(tag) in _canon(inner):
                add(tag)
        return " "

    text = _PAREN_RE.sub(_strip_paren, text)

    for rating in _RATING_RE.findall(text):
        add(rating)
    text = re.sub(r"\s*(?:PG-?12|R15\+|R18\+|G指定)\s*$", "", text)

    for tag in extra_tags:
        add(tag)

    trimmed = trim_tags(tags)

    base = re.sub(r"\s+", " ", text).strip(" \u3000-–—")
    return {"base_title": base or (raw or "").strip(), "tags": trimmed}


def film_key(base_title: str) -> str:
    """ASCII で読める安定キー。日本語だけの題は短いハッシュにする。

    作品コードはキーに使わない。同じ作品でも字幕版・吹替版・IMAX 版で
    別コードが振られるため。題名から版タグを剥がす方式なら 1 つにまとまる。

    鍵を作る前に全角を半角へ畳む。TOHO は「Ｍｉｃｈａｅｌ／マイケル」と
    全角で書き、T・ジョイは半角で書く。畳まないと同じ作品が 2 つの鍵を
    持ち、劇場をまたいで突き合わせられない（ポスターも共有できない）。
    表示に使うタイトルは畳まないこと——別の話。
    """
    norm = to_hankaku_full(base_title)
    latin = re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"[^\x00-\x7F]+", " ", norm))
    latin = re.sub(r"-{2,}", "-", latin).strip("-").lower()[:40].strip("-")
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6]
    return f"{latin}-{digest}" if re.search(r"[a-z]", latin) else f"film-{digest}"


def showing_id(cinema_id: str, date: str, start: str, screen: str, fkey: str) -> str:
    """決定的な ID。これが決定的だからこそ、ローカルとクラウドが独立に
    走っても結果を集合演算で合流できる。劇場 ID を含めるのを忘れないこと
    （2 館運用にすると、日付＋時刻＋スクリーンは容易に衝突する）。"""
    return f"{cinema_id}-{date.replace('-', '')}-{start.replace(':', '')}-s{screen}-{fkey}"


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


_ROBOTS_CACHE: dict[str, RobotFileParser | None] = {}


def robots_allows(cinema: dict, url: str) -> bool:
    key = cinema["robots_url"]
    if key not in _ROBOTS_CACHE:
        rp = RobotFileParser()
        rp.set_url(key)
        try:
            rp.read()
        except Exception:
            rp = None            # 取れないときは制限なしとみなす
        _ROBOTS_CACHE[key] = rp
    rp = _ROBOTS_CACHE[key]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def browser_session():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
        viewport={"width": 1440, "height": 2200}, device_scale_factor=1,
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


def dismiss_overlays(page) -> None:
    """Cookie 同意バナーが座席表に被ると撮影が崩れる。あれば消す。"""
    for sel in ("#onetrust-accept-btn-handler", "button:has-text('全てのCookieを受け入れる')",
                "button:has-text('すべてのCookieを受け入れる')", ".cookie-accept"):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible(timeout=800):
                loc.first.click(timeout=2000)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


# ═══════════════════════════════════════════════════════════════════
#  スケジュール抽出
# ═══════════════════════════════════════════════════════════════════
#
# TOHO は 1 日分しか描画しない。日付を変えるにはタブ（div.schedule-tab-item、
# id が素の日付）を押す。押すと .schedule-body の中身が差し替わる。
#
# ブラウザ内で 1 回だけ走らせて 1 日分を丸ごと持ち帰る。locator を何百回も
# 往復させるより速い。

EXTRACT_JS = r"""
(cfg) => {
  // &nbsp; が席数の直前に入るので、空白扱いに寄せてから畳む
  const norm = s => (s || '').replace(/\u00a0/g, ' ').replace(/[\u3000\s]+/g, ' ').trim();
  const txt = el => el ? (norm(el.innerText || '') || norm(el.textContent || '')) : '';
  const one = (el, sels) => {
    for (const sel of sels) { const n = el.querySelector(sel); if (n) return n; }
    return null;
  };

  // ── 日付タブ ────────────────────────────────────────────────
  const tabs = [...document.querySelectorAll(cfg.dayTab.join(','))]
    .filter(e => /^20\d{6}$/.test(e.id))
    .map(e => ({date: e.id, label: txt(e).slice(0, 24),
                selected: /is-selected|is-current|is-active/.test(e.className)}));
  const sel = tabs.find(t => t.selected);
  const date = sel ? sel.date : null;

  // ── 作品ブロック → 上映回 ───────────────────────────────────
  const rows = [];
  const blocks = [...document.querySelectorAll(cfg.filmBlock.join(','))];
  for (const blk of blocks) {
    const title = txt(one(blk, cfg.filmTitle));
    const info = txt(blk.querySelector('.schedule-body-info'));
    const items = [...blk.querySelectorAll(cfg.showing.join(','))];
    items.forEach((it, idx) => {
      const start = txt(it.querySelector('.start'));
      if (!start) return;
      const st = it.querySelector('.status');
      // 販売中でないときの wrapper は href="#" のダミー。実 URL だけ拾う。
      const w = it.querySelector('[href]');
      let href = w ? (w.getAttribute('href') || '') : '';
      if (href === '#' || /^javascript:/i.test(href) || !href) href = null;
      rows.push({
        date, section_id: blk.id || '', item_index: idx,
        title_raw: title, info_raw: info,
        start, end: txt(it.querySelector('.end')) || null,
        status_text: txt(st), status_class: st ? st.className : '',
        // 空席の程度は class にしか出ない。アイコンの class まで持ち帰って
        // おけば、対応表を後から突き合わせられる。
        status_html: st ? st.outerHTML.replace(/\s+/g, ' ').slice(0, 180) : '',
        item_class: it.className,
        screen_raw: txt(it.querySelector('.screen-name'))
                 || txt(it.querySelector('.screen-info')),
        note: txt(it.querySelector('.info')),
        href,
      });
    });
  }

  return {rows, tabs, debug: {
    date, tabs: tabs.length, blocks: blocks.length, rows: rows.length,
    with_href: rows.filter(r => r.href).length,
    statuses: rows.reduce((a, r) => (a[r.status_text] = (a[r.status_text] || 0) + 1, a), {}),
    sample: rows.slice(0, 3).map(r =>
      `${r.start}-${r.end} | ${r.screen_raw} | ${r.status_text} | ${r.title_raw.slice(0, 22)}`),
  }};
}
"""


def js_config() -> dict:
    return {
        "dayTab": SELECTORS["day_tab"],
        "filmBlock": SELECTORS["film_block"],
        "filmTitle": SELECTORS["film_title"],
        "showing": SELECTORS["showing"],
    }


# ── 全角と表記ゆれ ─────────────────────────────────────────────

_ZEN = str.maketrans("０１２３４５６７８９（）　", "0123456789() ")


def to_hankaku(text: str) -> str:
    """数字と括弧だけ半角に寄せる。

    タイトル全体に NFKC をかけてはいけない（「奇々怪々！」が壊れる）ので、
    判定に使う場所でだけ、必要な文字種だけ変換する。
    """
    return (text or "").replace("\u00a0", " ").translate(_ZEN)


def hhmm(raw: str) -> str | None:
    """「8:20」→「08:20」。

    ゼロ詰めしないと ID（…-820-…）も並び順も壊れる。文字列比較で
    時刻を並べている以上、桁を揃えるのは必須。
    """
    t = to_hankaku(raw or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_screen_name(raw: str) -> dict:
    """「本館スクリーン６ (100席)」→ 館・番号・席数。

    席数はページ側の値をそのまま使う。施設紹介の公称値より 1〜4 多いが、
    差は車椅子席の数と一致する。座席図に描かれるのは車椅子席込みなので、
    照合相手としてはこちらが正しい。
    """
    t = to_hankaku(raw)
    m = SCREEN_NAME_RE.search(t)
    if not m:
        return {"venue_name": None, "screen_no": "00", "seats": None, "raw": raw}
    return {
        "venue_name": m.group("venue"),
        "screen_no": m.group("no").zfill(2),
        "seats": int(m.group("seats")) if m.group("seats") else None,
        "raw": raw,
    }


def venue_for(cinema: dict, screen_no: str, venue_name: str | None) -> dict:
    """館は名前で引くのを優先し、無ければスクリーン番号から引く。

    ページが「本館 / 別館」と明示してくれているので、番号の対応表より
    そちらのほうが確か。改装で番号が振り直されても追随できる。
    """
    if venue_name:
        alias = {"アネックス": "別館", "annex": "別館"}.get(venue_name, venue_name)
        for v in cinema.get("venues", []):
            if v["name"] == alias:
                return v
    return venue_of(cinema, screen_no)


def classify_avail(status_text: str, status_code: str | None = None,
                   status_html: str = "") -> dict | None:
    """空席状況を決める。根拠は アイコン → 数字コード → 文字 の順。

    文字は「販売中」しか言わないので、それだけでは空き具合が分からない。
    どれも読めなかったときは捨てずに unknown として残す——捨てると
    対応表を育てられない。
    """
    text = (status_text or "").strip()
    icon = ICON_RE.search(status_html or "")
    icon_name = icon.group(1) if icon else None

    hit, by = None, None
    if icon_name and icon_name in AVAIL_ICON_MAP:
        hit, by = AVAIL_ICON_MAP[icon_name], "icon"
    elif status_code and status_code in AVAIL_STATUS_MAP:
        hit, by = AVAIL_STATUS_MAP[status_code], "class"
    if hit:
        code, mark, label, certainty = hit
        return {"code": code, "mark": mark, "label": label, "text": text,
                "status_code": status_code, "icon": icon_name,
                "certainty": certainty, "by": by}

    for code, keys, mark, label in AVAIL_TEXT_RULES:
        if text and any(k in text for k in keys):
            return {"code": code, "mark": mark, "label": label, "text": text,
                    "status_code": status_code, "icon": icon_name,
                    "certainty": "推定", "by": "text"}
    if not text and not status_code and not icon_name:
        return None
    return {"code": "unknown", "mark": "?",
            "label": text or icon_name or f"is-status-{status_code}",
            "text": text, "status_code": status_code, "icon": icon_name,
            "certainty": "未知", "by": "none"}


def rows_to_showings(cinema: dict, rows: list[dict], fallback_date: str | None) -> list[dict]:
    """JS が持ち帰った素の行を、記録の形に整える。"""
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        date = None
        if r.get("date"):
            d = str(r["date"])
            date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        date = date or fallback_date
        start = hhmm(r.get("start"))
        if not date or not start:
            continue

        # 版タグは作品名と回の付記（「字幕」「特別料金」等）の両方から拾う
        # 素の in だと全角で書かれた付記（ＩＭＡＸ 等）を取り落とす。
        # 題名側と同じ _canon を通して揃える。
        note = _canon((r.get("note") or "") + (r.get("info_raw") or ""))
        extra = [t for t in VERSION_TAGS if _canon(t) in note]
        info = parse_title(r.get("title_raw") or "", extra)
        if not info["base_title"]:
            continue
        fkey = film_key(info["base_title"])

        scr = parse_screen_name(r.get("screen_raw") or "")
        screen = scr["screen_no"]
        sid = showing_id(cinema["id"], date, start, screen, fkey)
        if sid in seen:
            continue
        seen.add(sid)

        href = r.get("href")
        if href and href.startswith("/"):
            href = ORIGIN + href

        venue = venue_for(cinema, screen, scr["venue_name"])
        m = re.search(r"is-status-(\d+)", r.get("status_class") or "")
        status_code = m.group(1) if m else None

        runtime = None
        rm = RUNTIME_RE.search(to_hankaku(r.get("info_raw") or ""))
        if rm:
            runtime = int(rm.group(1))

        sec = SECTION_ID_RE.match(r.get("section_id") or "")
        out.append({
            "id": sid,
            "cinema": cinema["id"], "cinema_name": cinema["name"],
            "venue": venue["id"], "venue_name": venue["name"],
            "date": date, "start": start, "end": hhmm(r.get("end")),
            "start_at": f"{date}T{start}:00+09:00",
            "film_key": fkey, "film_title": info["base_title"], "tags": info["tags"],
            "runtime_min": runtime,
            "sakuhin_cd": sec.group("sakuhin") if sec else None,
            "theater_cd": sec.group("theater") if sec else None,
            "screen": f"スクリーン{int(screen)}" if screen != "00" else "",
            "screen_no": screen,
            # ページが教えてくれる席数を優先。無いときだけ設定表を使う。
            "screen_seats": scr["seats"] or cinema.get("screen_seats", {}).get(screen),
            "screen_raw": scr["raw"],
            "note": r.get("note") or None,
            "availability": classify_avail(r.get("status_text"), status_code,
                                           r.get("status_html", "")),
            "status_code": status_code,
            "reserve_url": href,
            # 購入リンクが無いときは、一覧から辿り直すための座標を持たせる
            "click_ref": {"date": (r.get("date") or "").replace("-", ""),
                          "section_id": r.get("section_id"),
                          "item_index": r.get("item_index")},
            "status": "pending", "attempts": 0, "captures": 0,
            "slots": [],                 # 消化済みの計画点（due_slot 参照）
            "last_attempt_at": None,
        })
    return out


def open_schedule(page, cinema: dict) -> dict:
    """一覧ページを開いて、描画が終わるまで待つ。"""
    url = cinema["schedule_url"]
    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    check_response(resp, url)
    dismiss_overlays(page)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    for _ in range(20):                 # 中身は JS が後から差し込む
        res = page.evaluate(EXTRACT_JS, js_config())
        if res["debug"]["rows"]:
            return res
        page.wait_for_timeout(500)
    return res


def select_day(page, date8: str) -> dict | None:
    """日付タブを押して、その日の内容に差し替わるまで待つ。

    「押したら 2 秒待つ」では足りないことがあるので、抽出結果の日付が
    目的の日になるまで確認する。差し替わらなければ None を返して
    その日は諦める（既存データを壊さないため、空で上書きしない）。
    """
    try:
        tab = page.locator(f'.schedule-tab-item[id="{date8}"]')
        if tab.count() == 0:
            return None
        tab.first.scroll_into_view_if_needed(timeout=5000)
        tab.first.click(timeout=8000)
    except Exception:
        return None
    for _ in range(24):
        page.wait_for_timeout(500)
        res = page.evaluate(EXTRACT_JS, js_config())
        if res["debug"]["date"] == date8 and res["debug"]["rows"]:
            return res
    return None


def scrape_schedule(page, cinema: dict, max_days: int | None) -> dict:
    """1 劇場の上映スケジュールを取る。

    1 日ずつタブを押して回る。TOHO は 1 日分しか描画しないので、
    日数分だけページ内遷移が要る。
    """
    res = open_schedule(page, cinema)
    dbg = res["debug"]
    EV.add("SYNC", "probe",
           f"{cinema['id']} date={dbg['date']} tabs={dbg['tabs']} "
           f"blocks={dbg['blocks']} rows={dbg['rows']} href={dbg['with_href']}")
    if dbg.get("statuses"):
        EV.add("SYNC", "status", json.dumps(dbg["statuses"], ensure_ascii=False)[:110])

    today8 = now_jst().strftime("%Y%m%d")
    wanted = [t["date"] for t in res.get("tabs", []) if t["date"] >= today8]
    if max_days is not None:
        wanted = wanted[:max_days]

    days: dict[str, dict] = {}

    def take(result: dict) -> None:
        for s in rows_to_showings(cinema, result.get("rows", []), None):
            days.setdefault(s["date"], {"showings": []})["showings"].append(s)

    if dbg["date"] in wanted:
        take(res)

    for date8 in wanted:
        if date8 == dbg["date"]:
            continue
        polite_sleep()
        got = select_day(page, date8)
        if got is None:
            EV.add("SYNC", "skip", f"{date8} タブが開かない（既存データを守る）")
            continue
        take(got)

    for date in list(days):
        days[date]["showings"].sort(key=lambda s: (s["start"], s["screen_no"]))
    return days


def merge_schedule(old: dict, cinema_days: dict[str, dict]) -> tuple[dict, list[str]]:
    """今回実際に見た（劇場, 日付）だけを差し替える。

    見ていない分まで作り直すと、軽量同期のたびに先の日程が消える。
    """
    old_days = old.get("days", {})
    merged: dict[str, dict] = {k: dict(v) for k, v in old_days.items()}
    changes: list[str] = []

    for date, payload in cinema_days.items():
        prev_all = old_days.get(date, {}).get("showings", [])
        touched = {s["cinema"] for s in payload["showings"]}
        prev = {s["id"]: s for s in prev_all}

        rows = [s for s in prev_all if s.get("cinema") not in touched]
        for s in payload["showings"]:
            if s["id"] in prev:
                keep = prev[s["id"]]
                for field in ("status", "attempts", "captures", "slots",
                              "last_attempt_at"):
                    s[field] = keep.get(field, s[field])
            rows.append(s)

        if date in old_days:
            before = {s["id"] for s in prev_all if s.get("cinema") in touched}
            after = {s["id"] for s in payload["showings"]}
            changes += [f"{date} +{sid}" for sid in sorted(after - before)]
            changes += [f"{date} -{sid}" for sid in sorted(before - after)
                        if prev[sid].get("status") in ("pending", "retry", "provisional")]

        rows.sort(key=lambda s: (s["start"], s.get("cinema", ""), s["screen_no"]))
        merged[date] = {"showings": rows}

    cutoff = (now_jst() - timedelta(days=KEEP_PAST_DAYS)).date().isoformat()
    merged = {d: p for d, p in merged.items() if d >= cutoff}
    return ({"cinemas": [{k: v for k, v in c.items() if k != "screen_seats"}
                         for c in CINEMAS],
             "fetched_at": now_jst().isoformat(),
             "days": dict(sorted(merged.items()))}, changes)


# ═══════════════════════════════════════════════════════════════════
#  座席取得
# ═══════════════════════════════════════════════════════════════════

SEAT_JS = r"""
(cfg) => {
  const bagOf = el => {
    let s = (el.className && el.className.baseVal !== undefined
              ? el.className.baseVal : (el.className || '')) + ' ' + (el.id || '');
    for (const k of ['src','alt','title','aria-label','data-status','data-seat',
                     'data-state','data-type','value'])
      s += ' ' + ((el.getAttribute && el.getAttribute(k)) || '');
    try {
      const bg = getComputedStyle(el).backgroundImage;
      if (bg && bg !== 'none') s += ' ' + bg;
    } catch (e) {}
    return s.toLowerCase();
  };
  const deepBag = el => {
    let s = bagOf(el);
    for (const c of el.querySelectorAll('*')) s += ' ' + bagOf(c);
    return s;
  };
  const hasWord = (bag, kw) => {
    if (/[^\x00-\x7f]/.test(kw)) return bag.includes(kw);
    if (kw.length > 4) return bag.includes(kw);
    // ok / ng / on / off は前後が英字でないときだけ一致させる
    return new RegExp('(^|[^a-z])' + kw + '([^a-z]|$)').test(bag);
  };
  const classify = bag => {
    // class も id も src も alt も無い <td></td> は、ただの排版用マス。
    // 「判定できなかった座席」に数えると数百件の偽の警告になる。
    if (!bag.trim()) return 'spacer';
    for (const [state, keys] of cfg.rules)
      for (const kw of keys) if (hasWord(bag, kw)) return state;
    return 'other';
  };

  // ── 1. 座席らしい要素を集める ────────────────────────────────
  // 「クラス名や画像に seat と入っている」か「表のセル・イメージマップ」。
  const seatish = /seat|\u5ea7\u5e2d|\u30b7\u30fc\u30c8/;
  const cand = [];
  for (const el of document.querySelectorAll('a,img,td,area,li,span,div,button,input')) {
    if (el.querySelectorAll('*').length > 3) continue;      // 座席は末端に近い
    if (!seatish.test(bagOf(el)) && !/^(td|area)$/i.test(el.tagName)) continue;
    cand.push(el);
  }
  // 入れ子（<a><img></a>）は外側だけ残し、状態は子孫の属性も含めて見る
  const outer = cand.filter(el => !cand.some(o => o !== el && o.contains(el)));

  // ── 2. 座席表の器を決める ────────────────────────────────────
  // 座席を最も多く含む「いちばん深い」祖先＝座席表。凡例や説明モーダルの
  // 座席見本は器の外にあるので、これで自然に落ちる。
  const counts = new Map(), depth = new Map();
  for (const el of outer) {
    let n = el.parentElement, d = 0;
    while (n) { counts.set(n, (counts.get(n) || 0) + 1);
                if (!depth.has(n)) { let k = 0, m = n; while (m) { k++; m = m.parentElement; }
                                     depth.set(n, k); }
                n = n.parentElement; d++; }
  }
  let box = null;
  const need = Math.max(20, outer.length * 0.7);
  for (const [el, n] of counts) {
    if (n < need) continue;
    if (!box || depth.get(el) > depth.get(box)) box = el;
  }
  const seats = box ? outer.filter(el => box.contains(el)) : outer;
  // 撮影対象を「実際に数えた器」と同じにする。別のセレクタで探し直すと、
  // 非表示の要素を掴んでスクリーンショットが失敗する。
  document.querySelectorAll('[data-toho-seatbox]')
    .forEach(e => e.removeAttribute('data-toho-seatbox'));
  if (box) box.setAttribute('data-toho-seatbox', '1');

  // ── 3. 状態を数える ──────────────────────────────────────────
  const tally = {vacant: 0, sold: 0, wheelchair: 0, spacer: 0, blocked: 0, other: 0};
  const otherBags = {};
  // 種別ごとに実物を 1 つ残す。「sold が 448 件」が本物か
  // キーワードの誤爆かは、これを見ないと分からない。
  const samples = {};
  for (const el of seats) {
    const bag = deepBag(el);
    const c = classify(bag);
    tally[c] = (tally[c] || 0) + 1;
    if (!samples[c]) samples[c] = {
      bag: bag.replace(/\s+/g, ' ').slice(0, 130),
      html: el.outerHTML.replace(/\s+/g, ' ').slice(0, 220),
    };
    if (c === 'other') {
      const k = bag.replace(/\s+/g, ' ').slice(0, 80);
      otherBags[k] = (otherBags[k] || 0) + 1;
    }
  }

  // ── 4. 診断用：署名の一覧 ────────────────────────────────────
  const sig = el => el.tagName + '.' + [...(el.classList || [])].sort().join('.');
  const groups = new Map();
  for (const el of outer) {
    const k = sig(el);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(el);
  }
  const signatures = [...groups.entries()].map(([k, v]) => {
    const r = v[0].getBoundingClientRect();
    return {sig: k, count: v.length, w: Math.round(r.width), h: Math.round(r.height),
            state: classify(deepBag(v[0])),
            sample: v.slice(0, 2).map(x => deepBag(x).replace(/\s+/g, ' ').slice(0, 110))};
  }).sort((a, b) => b.count - a.count).slice(0, 14);

  return {
    used_signature: box ? sig(box) + ' #' + (box.id || '') : null,
    counts: tally,
    samples,
    candidates: outer.length,
    unknown_samples: Object.entries(otherBags).sort((a, b) => b[1] - a[1]).slice(0, 6),
    signatures,
    title: document.title,
    url: location.href,
  };
}
"""


def seat_js_config() -> dict:
    return {"rules": [[state, keys] for state, keys in SEAT_STATE_RULES]}


def due_slot(showing: dict, lead: float, sweep: bool = False) -> int | None:
    """今この瞬間に撮るべき計画点の番号。無ければ None。

    slot 0     大範囲走査のベースライン（開映がまだ遠い回も 1 枚撮っておく）
    slot 1..N  CAPTURE_PLAN の各点（開映 30 分前・5 分前 …）

    起こされるのが遅れて 30 分前と 5 分前をまたいでしまったときは、**深いほう**
    （開映に近いほう）だけを撮る。跨いだ点をあとから撮り直しても実態には
    近づかないので、まとめて済みにする。
    """
    done = set(showing.get("slots") or [])
    crossed = [i + 1 for i, p in enumerate(CAPTURE_PLAN)
               if lead <= p + PLAN_TOLERANCE_MIN]
    for i in reversed(crossed):
        if i not in done:
            return i
    if sweep and not crossed and 0 not in done:
        return 0
    return None


def slots_covered(slot: int, lead: float) -> list[int]:
    """その 1 枚で消化したことにする計画点の一覧。"""
    if slot == 0:
        return [0]
    return [0] + [i + 1 for i, p in enumerate(CAPTURE_PLAN)
                  if lead <= p + PLAN_TOLERANCE_MIN and i + 1 <= slot]


def due_showings(schedule: dict, now: datetime, lead_min: int,
                 sweep: bool = False) -> list[tuple[dict, int]]:
    """今撮るべき回と、その計画点の番号を並べて返す。"""
    active_ids = {c["id"] for c in active_cinemas()}
    out: list[tuple[dict, int]] = []
    for payload in schedule.get("days", {}).values():
        for s in payload.get("showings", []):
            if s.get("cinema") not in active_ids:
                continue
            # 休館中の館は撮りにいかない。館は記録側に入っているので
            # そちらを信じる（スクリーン番号から引き直さない）。
            cinema = cinema_by_id(s["cinema"])
            if cinema:
                v = next((v for v in cinema.get("venues", [])
                          if v["id"] == s.get("venue")), None)
                if v and not v.get("active", True):
                    continue
            if s.get("status") not in ("pending", "retry", "provisional"):
                continue
            if s.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            # 購入リンクは販売中の回にしか出ない。リンクが無くても
            # 一覧から辿り直せるので、明らかに売っていない回だけ外す。
            code = (s.get("availability") or {}).get("code")
            if code in ("outside", "closed", "counter"):
                continue
            if not s.get("reserve_url") and not (s.get("click_ref") or {}).get("section_id"):
                continue
            try:
                start = datetime.fromisoformat(s["start_at"])
            except (ValueError, KeyError):
                continue
            lead = (start - now).total_seconds() / 60
            if lead < -LATE_GRACE_MIN:
                continue
            horizon = (sweep_days(now) + 1) * 1440 if sweep else lead_min
            if lead > horizon:
                continue
            slot = due_slot(s, lead, sweep=sweep)
            if slot is not None:
                out.append((s, slot))
    out.sort(key=lambda t: (t[0]["start_at"], t[0].get("cinema", ""), t[0]["screen_no"]))

    # 絞り込みは並べ替えたあとに掛ける。max_per_round が「開映の早い順に N 件」
    # を意味するようにするため。
    screens = CAPTURE_ONLY.get("screens")
    if screens:
        out = [t for t in out if t[0].get("screen_no") in screens]
    keys = CAPTURE_ONLY.get("film_keys")
    if keys:
        out = [t for t in out if t[0].get("film_key") in keys]
    min_seats = CAPTURE_ONLY.get("min_seats")
    if isinstance(min_seats, int):
        out = [t for t in out if (t[0].get("screen_seats") or 0) >= min_seats]
    cap = CAPTURE_ONLY.get("max_per_round")
    if isinstance(cap, int):
        out = out[:cap]
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


def seat_ready(page) -> dict | None:
    """座席表が描画されているか調べ、できていれば計測結果を返す。"""
    try:
        rec = page.evaluate(SEAT_JS, seat_js_config())
    except Exception:
        return None
    total = sum(rec["counts"].values())
    return rec if total >= 20 else None


def open_seat_page(page, showing: dict, cinema: dict):
    """座席選択画面へ移動する。

    TOHO の一覧では、販売中でない回の購入ボタンは `<span href="#">` の
    ダミーで、実 URL はどこにも書かれていない（販売中の回だけ実リンクになる）。
    そこで 2 経路を用意する。

      1. reserve_url があるなら goto（速い）
      2. 無ければ一覧を開き直し、日付タブを押し、作品ブロックの n 番目の
         回をクリックする（click_ref に座標を持たせてある）

    2 は 1 回あたりページ遷移が増えるが、URL の組み立て方を推測しなくて
    済むぶん壊れにくい。別窓で開く場合にも備える。
    """
    collect: dict = {}
    url = showing.get("reserve_url")
    if url:
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             referer=cinema["schedule_url"], timeout=NAV_TIMEOUT_MS)
            check_response(resp, url)
            rec = wait_seat(page, collect)
            if rec is not None:
                rec["form_params"] = collect.get("params")
                return page, rec
        except Blocked:
            raise
        except Exception:
            pass

    ref = showing.get("click_ref") or {}
    if not ref.get("section_id"):
        return None, None

    try:
        open_schedule(page, cinema)
        if ref.get("date"):
            select_day(page, ref["date"])
        # 目印を付けてから掴む。作品ブロック内の .schedule-item は
        # section が分かれていることがあり、CSS の nth-of-type では
        # 数え方がずれる。JS で通し番号を数えて印を付けるほうが確実。
        # （id が数字で始まる 0371-029244 なので #id 記法も使えない）
        marked = page.evaluate(
            r"""(ref) => {
              document.querySelectorAll('[data-toho-target]')
                .forEach(e => e.removeAttribute('data-toho-target'));
              const blk = document.querySelector(`[id="${ref.section_id}"]`);
              if (!blk) return false;
              // 作品ブロックは折り畳まれていることがある。閉じたままだと
              // 中の回は非表示で、クリックできない。
              const panel = blk.querySelector('.schedule-body-panel');
              if (panel && getComputedStyle(panel).display === 'none') {
                const head = blk.querySelector('.schedule-section-header, .toggle-button');
                if (head) head.click();
              }
              const it = blk.querySelectorAll('.schedule-item')[ref.item_index];
              if (!it) return false;
              // クリックのハンドラは中の span.wrapper に付いている。
              // 外側の div を押しても何も起きない（イベントは下へ伝わらない）。
              const hit = it.querySelector('.wrapper') || it;
              hit.setAttribute('data-toho-target', '1');
              return true;
            }""", ref)
        if not marked:
            return None, None
        item = page.locator('[data-toho-target="1"]')
        if item.count() == 0:
            return None, None
        item.first.scroll_into_view_if_needed(timeout=5000)

        target = page
        try:
            with page.context.expect_page(timeout=6000) as popup:
                item.first.click(timeout=8000)
            target = popup.value
            target.wait_for_load_state("domcontentloaded")
        except Exception:
            page.wait_for_timeout(2500)
        rec = wait_seat(target, collect)
        if rec is not None:
            rec["form_params"] = collect.get("params")
            rec["seat_url"] = target.url
            return target, rec
        if target is not page:
            target.close()
    except Blocked:
        raise
    except Exception as exc:
        collect.setdefault("trace", []).append(
            f"click 経路で例外 {type(exc).__name__}: {str(exc)[:120]}")
    showing.setdefault("_debug", {}).update(collect)
    # 失敗したときページは購入フローの途中に残る。次の回はどのみち
    # 一覧を開き直すので害は無いが、セッションを掴んだままにせず戻す。
    try:
        page.goto(cinema["schedule_url"], wait_until="domcontentloaded",
                  timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass
    return None, None


def advance_interstitial(page, trace: list | None = None) -> str | None:
    """座席表の手前に挟まる中間ページを 1 枚だけ通す。

    TOHO は上映回をクリックすると、まず TNPI2040J04（TOHO-ONE の入会・
    ログイン勧誘）に飛ぶ。そこの「ログインせずに購入する」を押すと
    座席選択へ進む。

    許可した文言のボタンしか押さない。「次へ」を機械的に押し進める
    実装にすると、その先の購入確定まで踏み抜きかねない。

    失敗は握り潰さず trace に積む。ここを黙って continue にしていたため、
    「ボタンは見えているのに押されない」の原因が分からなくなった。
    """
    for label in ADVANCE_BUTTON_TEXTS:
        if any(bad in label for bad in FORBIDDEN_BUTTON_TEXTS):
            continue                      # 許可リストの取り違え防止

        # 掴み方を変えて順に試す。role 名は前後の空白や <br> の入り方で
        # 揺れるので、text-is → has-text と緩めていく。
        strategies = [
            ("role", lambda: page.get_by_role("button", name=label, exact=True)),
            ("text-is", lambda: page.locator(
                f'button:text-is("{label}"), input[type=submit][value="{label}"]')),
            ("has-text", lambda: page.locator("button", has_text=label)),
            ("form-submit", lambda: page.locator(
                'form[name="selectSeatIntForm"] button[type=submit]')),
        ]
        for how, make in strategies:
            try:
                btn = make()
                n = btn.count()
            except Exception as exc:
                if trace is not None:
                    trace.append(f"{label}/{how}: locator 失敗 {str(exc)[:70]}")
                continue
            if n == 0:
                if trace is not None:
                    trace.append(f"{label}/{how}: 見つからない")
                continue

            # form-submit は文言を確認してから押す（別のボタンを踏まないため）
            if how == "form-submit":
                try:
                    got = (btn.first.inner_text(timeout=2000) or "").strip()
                except Exception:
                    got = ""
                if got not in ADVANCE_BUTTON_TEXTS:
                    if trace is not None:
                        trace.append(f"{label}/{how}: 文言が違う（{got[:20]}）ので押さない")
                    continue

            try:
                before = page.url
                btn.first.click(timeout=8000)
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(1200)
                if trace is not None:
                    trace.append(f"{label}/{how}: クリック成功 {before} → {page.url}")
                return label
            except Exception as exc:
                if trace is not None:
                    trace.append(f"{label}/{how}: クリック失敗 {type(exc).__name__} "
                                 f"{str(exc)[:90]}")
    return None


def form_params(page) -> dict:
    """中間ページの hidden から上映回の識別子を拾う。

    jyoei_date / gekijyo_cd / screen_cd / sakuhin_cd / pf_no が揃う。
    いまは記録に添えるだけだが、将来 URL を直接組み立てたくなったときの
    材料になる（gekijyo_cd の下 1 桁が館を表しているらしい：0371＝本館）。
    """
    try:
        return page.evaluate(
            r"""() => Object.fromEntries([...document.querySelectorAll(
                  'input[type=hidden][name]')].map(i => [i.name, i.value]))""")
    except Exception:
        return {}


def wait_seat(page, collect: dict | None = None):
    """座席表が描画されるまで待つ。中間ページは通す。"""
    dismiss_overlays(page)
    trace = collect.setdefault("trace", []) if collect is not None else []
    advanced = 0
    for _ in range(30):
        rec = seat_ready(page)
        if rec is not None:
            return rec
        if advanced < MAX_ADVANCE_STEPS:
            if collect is not None and not collect.get("params"):
                params = form_params(page)
                if params:
                    collect["params"] = params
            label = advance_interstitial(page, trace)
            if label:
                advanced += 1
                EV.add("CAP", "advance", f"{label} → {page.url.rsplit('/', 1)[-1]}")
                dismiss_overlays(page)
                continue
        page.wait_for_timeout(500)
    if collect is not None:
        collect["landed_url"] = page.url
    return None


def read_seat_page(page, probe: dict, shot_path: Path | None,
                   expected_seats: int | None) -> dict:
    record: dict = {}
    counts = probe.get("counts", {})
    vacant = counts.get("vacant", 0)
    sold = counts.get("sold", 0)
    # spacer（通路・空きマス）は正常な構成要素なので unclassified に数えない
    other = counts.get("other", 0) + counts.get("blocked", 0)
    total = vacant + sold

    record["seat_counts"] = {"vacant": vacant, "sold": sold, "total": total,
                             "wheelchair": counts.get("wheelchair", 0),
                             "spacer": counts.get("spacer", 0),
                             "unclassified": other, "expected": expected_seats}
    record["occupancy"] = round(sold / total, 4) if total else None
    record["seat_signature"] = probe.get("used_signature")
    if probe.get("form_params"):
        record["form_params"] = probe["form_params"]
    if probe.get("seat_url"):
        record["seat_url"] = probe["seat_url"]

    # 見張り。座席図はテーブルなので、通路や空きマスの <td> が座席と同数近く
    # 混ざる。それを「判定できない座席」と数えると毎回警告が出て意味を失う。
    # 公称席数と一致しているなら、余りが何百あっても構造は読めている。
    if total == 0:
        record["seat_warning"] = "座席が 0 件"
    elif expected_seats and abs(total - expected_seats) > 2:
        record["seat_warning"] = (f"座席数 {total} ≠ 公称 {expected_seats}"
                                  f"（判定不能 {other} 件）")
    elif not expected_seats and other > total:
        record["seat_warning"] = f"判定できない要素が座席より多い（{other} > {total}）"

    try:
        record["poster_url"] = page.evaluate(
            r"""() => Array.from(document.images).map(i => i.src)
                   .find(s => /(poster|works|movie|sakuhin)/i.test(s)
                              && /\.(jpe?g|png)/i.test(s)) || null""")
    except Exception:
        pass

    if shot_path is not None:
        # 数えた器に印が付いているので、それを撮る。
        container = page.locator('[data-toho-seatbox="1"]')
        if container.count() == 0:
            container, _ = first_match(page, SELECTORS["seat_container"])
        shot_path.parent.mkdir(parents=True, exist_ok=True)
        opts = {"path": str(shot_path), "timeout": 15_000,
                "type": "jpeg", "quality": SEAT_SHOT_QUALITY}
        shot_ok = False
        if container is not None and container.count():
            try:
                container.first.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(400)
                container.first.screenshot(**opts)
                shot_ok = True
            except Exception as exc:
                LOG.warning("座席図の要素撮影に失敗（全画面に切り替える）: %s",
                            str(exc).splitlines()[0][:90])
        if not shot_ok:
            try:
                page.screenshot(full_page=True, **opts)
                shot_ok = True
            except Exception as exc:
                LOG.warning("座席図の撮影に失敗: %s", str(exc).splitlines()[0][:90])
        if shot_ok and shot_path.exists():
            record["seat_image"] = rel(shot_path)
            record["seat_image_bytes"] = shot_path.stat().st_size
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
#
# 記録には 2 種類ある。
#   source="schedule" … 一覧ページから作った予定だけの記録（画像なし）
#   source="seat"     … 座席選択画面まで入って撮った記録
# 同じ id で衝突したらこの順位で勝ち負けを決める。純粋な比較なので、
# ローカルとクラウドがどちらの順で合流しても同じ結果になる（冪等）。

STATUS_RANK = {"pending": 0, "retry": 1, "missed": 2, "failed": 2,
               "provisional": 3, "captured": 4}


def record_source(rec: dict) -> str:
    """記録の種別。`source` が無い記録も中身から判定する。

    中身（画像・座席数・撮影時刻）があるなら、それは撮影済みの記録。
    ここを source の有無だけで判定すると、あとから作った予定だけの記録に
    上書きされて実際の撮影結果が消える。
    """
    if rec.get("source"):
        return rec["source"]
    if (rec.get("seat_image") or rec.get("occupancy") is not None
            or rec.get("lead_minutes") is not None or rec.get("seat_counts")):
        return "seat"
    return "schedule"


def lead_of(rec: dict, default: float = 9999.0) -> float:
    """開映まで何分か。数値として使えないときは default を返す。

    lead_minutes は「予定だけの記録」には無く、古い記録では欠けていることも
    ある。並べ替えの鍵に直接入れると None が紛れて比較が壊れるので、
    ここで必ず float にしてから使う。
    """
    v = rec.get("lead_minutes") if "lead_minutes" in rec else rec.get("lead")
    return float(v) if isinstance(v, (int, float)) else default


def record_quality(rec: dict) -> int:
    """記録の質。2 = 信用できる座席の読み、1 = 怪しい、0 = 予定だけ。

    ここが記録を守る要になる。同じ回の記録は id が同じなので 1 件しか残らず、
    順位は「開映に近いほうが勝つ」で決めていた。ところが座席ページには
    入れたのに座席表が描かれない、途中までしか描かれない、という失敗の仕方が
    ある。そのまま順位を付けると、開映 5 分前に取れた **0 席の読み** が
    30 分前の正しい読みを追い出してしまう。

    質を順位の第 1 要素に置くと、それが起きない。壊れた読みは既存を
    押しのけられず、記録に残るのは最後に取れた **まともな** 読みになる。

    満席は壊れた読みではない。sold=376 / vacant=0 / total=376 は
    公称席数と一致するので質 2 と判定され、通常どおり更新される。
    """
    if record_source(rec) != "seat":
        return 0
    counts = rec.get("seat_counts") or {}
    total = counts.get("total") or 0
    if total <= 0 or rec.get("occupancy") is None:
        return 1                       # 座席が 1 つも読めていない
    expected = counts.get("expected")
    if expected and abs(total - expected) > 2:
        return 1                       # 公称席数と食い違う＝読み落としている
    return 2


def record_rank(rec: dict) -> tuple:
    """大きいほど優先。まず質、次に開映への近さ。"""
    q = record_quality(rec)
    if q == 0:
        return (0, 0, rec.get("updated_at") or "")
    return (q, -lead_of(rec), rec.get("captured_at") or "")


def sort_key(c: dict) -> tuple:
    return (c["date"], c["start"], c.get("cinema", ""), c.get("screen_no", ""))


def record_history(rec: dict | None) -> list[dict]:
    """記録が持っている「読みの並び」。撮影記録なら自分自身も 1 点として数える。

    同じ回を 3 回撮っても、id が同じなので勝ち残る記録は 1 件だけ（開映に一番
    近い読み）。それだけだと売れ方の推移が消えてしまうので、数値の点だけを
    history に畳んで持たせる。id が決定的なぶん、この配列も点の和集合として
    合流でき、順番が違っても二度流しても同じ結果になる。
    """
    out = list(rec.get("history") or []) if rec else []
    p = record_point(rec) if rec else None
    if p:
        out.append(p)
    return out


def record_point(rec: dict) -> dict | None:
    """その記録自身を推移の 1 点として表したもの。怪しい読みなら None。"""
    if record_quality(rec) < 2:
        return None
    counts = rec.get("seat_counts") or {}
    return {"at": rec.get("captured_at"), "lead": rec.get("lead_minutes"),
            "slot": rec.get("capture_slot"), "sold": counts.get("sold"),
            "vacant": counts.get("vacant"), "total": counts.get("total"),
            "occupancy": rec.get("occupancy")}


def _hist_rank(h: dict) -> tuple:
    return (-lead_of(h), h.get("at") or "")


def merge_history(*groups) -> list[dict]:
    """点の和集合。同じ計画点が二重に来たら開映に近い読みを残す。"""
    best: dict[Any, dict] = {}
    for group in groups:
        for h in group or []:
            if not h or h.get("sold") is None:
                continue
            key = h.get("slot")
            if key is None:
                key = round(h.get("lead") or 0)
            cur = best.get(key)
            if cur is None or _hist_rank(h) > _hist_rank(cur):
                best[key] = h
    # 開映から遠い点（＝早い時刻に撮ったもの）を先頭に置く。lead が欠けている
    # 点はいつの読みか分からないので末尾に回す。record_rank とは既定値が逆に
    # なるが、あちらは「不明なら既存を押しのけない」で、こちらは「不明なら
    # 推移の途中に割り込ませない」。どちらも安全側に倒している。
    return sorted(best.values(), key=lambda h: -lead_of(h, -1.0))


def looks_truncated(new: dict, cur: dict) -> bool:
    """新しい読みで席数が目減りしていないか。

    公称席数が分からないスクリーンでは record_quality が働かない。その場合は
    既存の記録そのものを物差しにする。同じ回で箱の大きさが 1 割も縮むのは
    座席表を読み切れていないときだけで、正常な変化ではない。
    """
    a = (new.get("seat_counts") or {}).get("total") or 0
    b = (cur.get("seat_counts") or {}).get("total") or 0
    return b > 0 and a < b * 0.9


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
            if cur is None:
                winner = rec
            elif record_quality(rec) != record_quality(cur):
                # まず質。0 席や公称と食い違う読みは、開映に近くても勝てない。
                winner = rec if record_quality(rec) > record_quality(cur) else cur
            elif looks_truncated(rec, cur):
                # 質が同点でも席数が縮んでいる＝読み切れていない。既存を守る。
                LOG.warning("席数の目減りを検知したので更新しない: %s（%s → %s 席）",
                            rec.get("id"), (cur.get("seat_counts") or {}).get("total"),
                            (rec.get("seat_counts") or {}).get("total"))
                winner = cur
            elif looks_truncated(cur, rec):
                # 逆向き。先に入っていたほうが読み切れていなかった場合。
                winner = rec
            else:
                winner = rec if record_rank(rec) > record_rank(cur) else cur

            # 推移の点。負けたほうの数値も残す（票の動きはここにしか無い）。
            hist = merge_history(record_history(cur), record_history(rec))
            # 読み切れていない点は落とす。折れ線が谷になるだけで「その時刻に
            # それだけしか売れていなかった」ではない。物差しは勝った記録の席数。
            # 一度取り込んでしまった点にも遡って効く。
            floor = ((winner.get("seat_counts") or {}).get("total") or 0) * 0.9
            if floor:
                hist = [h for h in hist if not h.get("total") or h["total"] >= floor]
            merged = {**winner, "history": hist} if hist else winner
            if merged != cur:
                index[rec["id"]] = merged
                changed = True
                written += 1
        if not changed:
            continue
        data["captures"] = sorted(index.values(), key=sort_key)
        data["updated_at"] = now_jst().isoformat()
        write_json(path, data)
    return written


def schedule_records(days: dict) -> list[dict]:
    """一覧ページの情報だけで記録を作る。

    座席表が撮れなくても「その回が存在した」ことと空席記号は残る。
    あとで実際に撮れたら source="seat" の記録が上書きする。
    """
    stamp = now_jst().isoformat()
    out = []
    for payload in days.values():
        for s in payload.get("showings", []):
            rec = {k: v for k, v in s.items()
                   if k not in ("status", "attempts", "captures", "last_attempt_at")}
            rec.update(source="schedule", updated_at=stamp,
                       seat_image=None, occupancy=None)
            out.append(rec)
    return out


def build_films(records: list[dict]) -> dict:
    """作品台帳を記録から**毎回作り直す**。

    増分カウンタは合流できない（両側で 1 ずつ増えたとき、和なのか max なのか
    判断できない）。派生物にしておけば、合流時は捨てて作り直すだけで済む。
    """
    films: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: (x["date"], x["start"])):
        key = r["film_key"]
        e = films.setdefault(key, {
            "key": key, "title": r["film_title"], "poster": None, "tags": [],
            "first_seen": r["date"], "last_seen": r["date"],
            "cinemas": [], "showing_count": 0, "capture_count": 0,
        })
        if r.get("poster"):
            e["poster"] = r["poster"]
        for t in r.get("tags", []):
            if t not in e["tags"]:
                e["tags"].append(t)
        if r.get("cinema") and r["cinema"] not in e["cinemas"]:
            e["cinemas"].append(r["cinema"])
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


SHARED_POSTERS_FILE = DATA / "posters_shared.json"


def apply_shared_posters(films: dict) -> int:
    """ポスターが取れなかった作品に、隣（T・ジョイ側）の画像を借りる。

    TOHO の座席ページには作品画像が無いことが多い。同じ作品を撮っている
    T・ジョイ側には画像があるので、hub.py が題名で突き合わせて作った対応表
    （data/posters_shared.json）を当てる。対応表が無ければ何もしない
    ＝ 今までどおり空欄のまま。借り物には poster_from を立てておく。
    """
    shared = read_json(SHARED_POSTERS_FILE, {})
    if not isinstance(shared, dict) or not shared:
        return 0
    n = 0
    for key, entry in films.items():
        if entry.get("poster") or not shared.get(key):
            continue
        entry["poster"] = shared[key]
        entry["poster_from"] = "tjoy"
        n += 1
    if n:
        EV.add("INDEX", "poster", f"T・ジョイ側から {n} 作品ぶん借用")
    return n


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
    apply_shared_posters(films)
    write_json(FILMS_FILE, films)

    months: list[dict] = []
    dates: dict[str, dict] = {}
    for path in sorted(CAPTURES.glob("*.json")):
        caps = read_json(path, {}).get("captures", [])
        if caps:
            months.append({"month": path.stem, "file": rel(path), "count": len(caps)})

    venue_stats: dict[str, dict] = {}
    for c in records:
        d = dates.setdefault(c["date"], {"date": c["date"], "count": 0,
                                         "captured": 0, "films": []})
        d["count"] += 1
        if record_source(c) == "seat":
            d["captured"] += 1
        if c["film_key"] not in d["films"]:
            d["films"].append(c["film_key"])

        vkey = f"{c.get('cinema', '?')}/{c.get('venue', '?')}"
        v = venue_stats.setdefault(vkey, {
            "key": vkey, "cinema": c.get("cinema"), "cinema_name": c.get("cinema_name"),
            "venue": c.get("venue"), "venue_name": c.get("venue_name"),
            "count": 0, "captured": 0, "screens": []})
        v["count"] += 1
        if record_source(c) == "seat":
            v["captured"] += 1
        if c.get("screen_no") and c["screen_no"] not in v["screens"]:
            v["screens"].append(c["screen_no"])

    warnings = []
    for v in venue_stats.values():
        if v["venue"] == "unknown":
            warnings.append(f"館を判定できない記録が {v['count']} 件"
                            f"（スクリーン {sorted(v['screens'])}）")
    for w in warnings:
        EV.add("WARN", "venue", w)

    schedule = read_json(SCHEDULE_FILE, {})
    pending = sum(1 for p in schedule.get("days", {}).values()
                  for s in p.get("showings", []) if s.get("status") in ("pending", "retry"))

    write_json(INDEX_FILE, {
        "cinemas": [{k: v for k, v in c.items() if k != "screen_seats"} for c in CINEMAS],
        "generated_at": now_jst().isoformat(),
        "total_records": total,        # 予定＋撮影済み
        "total_captures": seat_total,  # 実際に座席表を撮れた数
        "pending_showings": pending,
        "warnings": warnings,
        "venues": sorted(venue_stats.values(), key=lambda v: v["key"]),
        "months": months,
        "dates": sorted(dates.values(), key=lambda d: d["date"], reverse=True),
        "films": sorted(films.values(),
                        key=lambda f: (f.get("last_seen") or "", f["title"]), reverse=True),
    })


# ═══════════════════════════════════════════════════════════════════
#  リポジトリ同期（ローカルとクラウドのどちらから走らせても合流する）
# ═══════════════════════════════════════════════════════════════════
#
# 合流できるのは、記録の id が決定的（劇場-日付-時刻-スクリーン-作品）だから。
# 両側が独立に走っても同じ id を作るので、突き合わせは純粋な集合演算になる。
#
#   captures/*.json  id で和集合。衝突は record_rank で決める
#   media/           勝った記録の側のファイルを採用
#   schedule.json    id で和集合。状態は進んだほう、回数は max
#   state.json       時刻は新しいほう、変更履歴は連結
#   logs/            行の和集合を時刻順に
#   films/index.json 合流しない。捨てて作り直す
#
# どの規則も可換かつ冪等なので、順番が違っても、二度流しても同じ結果になる。

DATA_BRANCH = "toho-data"
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
            local["captures"] = sorted(index.values(), key=sort_key)
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
            mine["slots"] = sorted(set(mine.get("slots") or []) | set(s.get("slots") or []))
            for field in ("reserve_url", "end", "availability"):
                if not mine.get(field) and s.get(field):
                    mine[field] = s[field]
        days[date] = {"showings": sorted(
            cur.values(), key=lambda x: (x["start"], x.get("cinema", ""), x["screen_no"]))}

    cutoff = (now_jst() - timedelta(days=KEEP_PAST_DAYS)).date().isoformat()
    days = {d: p for d, p in days.items() if d >= cutoff}
    fetched = max(filter(None, [local.get("fetched_at"), remote.get("fetched_at")]),
                  default=now_jst().isoformat())
    write_json(SCHEDULE_FILE, {
        "cinemas": [{k: v for k, v in c.items() if k != "screen_seats"} for c in CINEMAS],
        "fetched_at": fetched, "days": dict(sorted(days.items()))})


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
    out = git("show", f"{remote_ref()}:logs/toho.log", check=False)
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
        work = Path(tempfile.mkdtemp(prefix="toho-push-"))
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
            git("commit", "-m", f"toho {stamp} ({total} 件)", cwd=work)
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

    all_days: dict[str, dict] = {}
    pw, browser, ctx = browser_session()
    try:
        ctx.image_flags["images"] = False        # 一覧は本文と属性しか要らない
        page = ctx.new_page()
        for cinema in active_cinemas():
            if not robots_allows(cinema, cinema["schedule_url"]):
                EV.add("SYNC", "robots", f"{cinema['id']} robots.txt により中止")
                continue
            days = scrape_schedule(page, cinema,
                                   None if full else sweep_days(now_jst()) + 1)
            for date, payload in days.items():
                all_days.setdefault(date, {"showings": []})["showings"] += payload["showings"]
            polite_sleep()
    finally:
        ctx.close(); browser.close(); pw.stop()

    if not all_days:
        EV.add("SYNC", "empty", "1 件も取れなかった（既存データを維持）")
        return

    merged, changes = merge_schedule(schedule, all_days)
    write_json(SCHEDULE_FILE, merged)
    shows = sum(len(d["showings"]) for d in all_days.values())

    # 一覧の情報だけで先に記録を作っておく。座席表が撮れなくても
    # 「その回があった」ことと空席記号は残る。
    seeded = archive_many(schedule_records(all_days))
    rebuild_index()

    EV.add("SYNC", "full" if full else "light",
           f"{len(all_days)}日 {shows}件  予定記録 {seeded} 件"
           + (f"   変更 {len(changes)} 件" if changes else ""))

    if changes:
        for c in changes[:8]:
            EV.add("SYNC", "change", c)
        state = load_state()
        log = state.setdefault("sync_changes", [])
        log.append({"at": now_jst().isoformat(), "changes": changes})
        state["sync_changes"] = log[-40:]
        save_state(state)


def full_house_image(cinema_id: str | None, screen_no: str | None) -> str | None:
    """満席のときに使う既製の座席図。

    満席の回は座席選択へ入れないので撮りようがない。ただし「全席が売れて
    いる図」はスクリーンごとに 1 枚あれば足りるので、あらかじめ
    media/seats/tohoumeda_theater_6_manseki.jpg のように置いておく。
    無ければ None を返し、画像なしの満席として記録する。
    """
    try:
        n = int(screen_no or "")
    except (TypeError, ValueError):
        return None
    path = SEATS / f"{cinema_id or 'toho'}_theater_{n}_manseki.jpg"
    return rel(path) if path.exists() else None


def full_house_record(s: dict, slot: int, lead: float, final: bool) -> dict:
    """満席の回を「撮り逃し」ではなく「満席」として残す。

    座席表は撮れないが、状態としては座席表より確定的である（空席 0）。
    席数は screen_seats の公称値を使う。公称値が分からないスクリーンでは
    total が 0 になり、record_quality が 1 と判定するので、本物の読みを
    押しのけることはない。満席という事実だけが残る。
    """
    seats = s.get("screen_seats") or 0
    return {
        **s,
        "source": "seat",
        "full_house": True,
        "seat_counts": {"vacant": 0, "sold": seats, "total": seats,
                        "expected": seats},
        "occupancy": 1.0 if seats else None,
        "seat_image": full_house_image(s.get("cinema"), s.get("screen_no")),
        "poster": None,
        "lead_minutes": lead,
        "captured_at": now_jst().isoformat(),
        "capture_round": s.get("captures", 0),
        "capture_slot": slot,
        "status": "captured" if final else "provisional",
    }


def cmd_capture(lead_min: int = CAPTURE_LEAD_MIN, limit: int | None = None,
                dry_run: bool = False, sweep: bool = False) -> None:
    schedule = read_json(SCHEDULE_FILE, {"days": {}})
    now = now_jst()
    due = due_showings(schedule, now, lead_min, sweep=sweep)
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
        # 先に一覧を開いて cookie / session を作る（劇場ごとに 1 回）
        opened: set[str] = set()
        for s, _ in due:
            cinema = cinema_by_id(s["cinema"])
            if cinema is None or cinema["id"] in opened:
                continue
            resp = page.goto(cinema["schedule_url"], wait_until="domcontentloaded",
                             timeout=NAV_TIMEOUT_MS)
            check_response(resp, cinema["schedule_url"])
            dismiss_overlays(page)
            page.wait_for_timeout(1500)
            opened.add(cinema["id"])
        ctx.image_flags["images"] = True     # 座席図は撮影対象

        for s, slot in due:
            cinema = cinema_by_id(s["cinema"])
            if cinema is None:
                continue
            s["last_attempt_at"] = now_jst().isoformat()
            lead = round(
                (datetime.fromisoformat(s["start_at"]) - now_jst()).total_seconds() / 60, 1)

            # 満席の回は座席選択へ入れない。開きにいくだけ無駄なので先に畳む。
            if (s.get("availability") or {}).get("code") == "full":
                s["captures"] = s.get("captures", 0) + 1
                s["slots"] = sorted(set(s.get("slots") or [])
                                    | set(slots_covered(slot, lead)))
                final = slot >= len(CAPTURE_PLAN)
                record = full_house_record(s, slot, lead, final)
                if not dry_run:
                    archive_many([record])
                s["status"] = "captured" if final else "provisional"
                if final:
                    done.add(s["id"])
                ok += 1
                EV.add("CAP", "満席",
                       f"{s['date'][5:]} {s['start']} {s['film_title'][:14]}"
                       + ("" if record["seat_image"] else "  （既製図なし）"))
                continue

            try:
                seat_page, probe = open_seat_page(page, s, cinema)
                if seat_page is None:
                    # 大範囲走査には販売前の回も混ざる。ここで失敗回数を
                    # 数えると、本番（30 分前・5 分前）までに上限を使い切る。
                    if not sweep:
                        s["attempts"] += 1
                        s["status"] = ("retry" if s["attempts"] < MAX_ATTEMPTS
                                       else "failed")
                    fail += 1
                    EV.add("CAP", "no-page",
                           f"{s['date'][5:]} {s['start']} {s['film_title'][:14]}")
                    continue

                shot = None
                if SAVE_SEAT_SHOT and not dry_run:
                    suffix = f"_p{slot}" if KEEP_SLOT_SHOTS else ""
                    name = (f"{s['film_key']}_{s['date'].replace('-', '')}"
                            f"_{s['start'].replace(':', '')}"
                            f"_{s['cinema']}_s{s['screen_no']}{suffix}.jpg")
                    shot = SEATS / s["date"][:7] / name

                rec = read_seat_page(seat_page, probe, shot, s.get("screen_seats"))

                poster = None
                if not dry_run and rec.get("poster_url"):
                    p = POSTERS / f"{s['film_key']}.jpg"
                    poster = rel(p) if p.exists() else download(seat_page, rec["poster_url"], p)

                s["captures"] = s.get("captures", 0) + 1
                s["slots"] = sorted(set(s.get("slots") or [])
                                    | set(slots_covered(slot, lead)))
                final = slot >= len(CAPTURE_PLAN)
                record = {**s, **rec, "poster": poster, "source": "seat",
                          "lead_minutes": lead, "captured_at": now_jst().isoformat(),
                          "capture_round": s["captures"], "capture_slot": slot,
                          "status": "captured" if final else "provisional"}

                if not dry_run:
                    archive_many([record])
                s["status"] = "captured" if final else "provisional"
                if final:
                    done.add(s["id"])
                ok += 1

                occ = rec.get("occupancy")
                EV.add("CAP", "final" if final else f"slot{slot}",
                       f"{s['date'][5:]} {s['start']} {s['cinema']}/s{s['screen_no']} "
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

    EV.add("CAP", "sweep" if sweep else "done",
           f"対象 {len(due)} / 成功 {ok} / 失敗 {fail} / {time.time() - t0:.0f}s"
           + ("   [dry-run]" if dry_run else ""))


def cmd_repair() -> int:
    """media/seats/ に残っている画像を記録に繋ぎ直す。

    画像はどこでも削除されないので、JSON 側の関連が失われてもディスクには
    残っている。ファイル名から劇場・日付・時刻・作品を復元する。

    撮影時刻は画像ファイルの更新時刻から取る。ただし pull で持ってきた画像は
    書き直された時点の時刻になるため、開映前 24 時間〜開映直後という常識的な
    範囲に収まらなければ採用せず、時刻不明として残す。
    """
    pattern = re.compile(
        r"^(?P<film>.+)_(?P<date>\d{8})_(?P<time>\d{4})_(?P<cin>[A-Za-z0-9]+)"
        r"_s(?P<screen>\d{2})\.jpe?g$", re.IGNORECASE)
    images: dict[tuple[str, str, str, str], Path] = {}
    for img in SEATS.rglob("*.jp*g"):
        m = pattern.match(img.name)
        if not m:
            EV.add("WARN", "repair", f"名前を解釈できない: {img.name}")
            continue
        d = m.group("date")
        key = (m.group("cin"), f"{d[:4]}-{d[4:6]}-{d[6:]}",
               f"{m.group('time')[:2]}:{m.group('time')[2:]}", m.group("film"))
        images[key] = img

    if not images:
        print("media/seats/ に画像がありません")
        return 0

    fixed = timed = 0
    for path in sorted(CAPTURES.glob("*.json")):
        data = read_json(path, {"captures": []})
        changed = False
        for rec in data.get("captures", []):
            key = (rec.get("cinema", ""), rec["date"], rec["start"], rec["film_key"])
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
        print(f"  記録なし: {key}")
    return 0


def cmd_doctor(deep: bool = False) -> int:
    """環境と取得経路を一通り確認する。データは一切書かない。

    ここの出力がそのまま「サイト構造が変わっていないか」の検査になる。
    値が合わないときは inspect のダンプを見て SELECTORS / SEAT_STATE_RULES を直す。
    """
    print("\n=== toho.py doctor ===\n")
    ng = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal ng
        print(f"  {'OK ' if ok else 'NG '} {label:<26} {detail}")
        if not ok:
            ng += 1

    try:
        import playwright                                            # noqa: F401
        check("Playwright", True)
    except ImportError:
        check("Playwright", False, "pip install playwright / playwright install chromium")
        return 1

    cd = load_state().get("cooldown_until")
    check("クールダウン", not cd or datetime.fromisoformat(cd) < now_jst(), cd or "なし")

    pw, browser, ctx = browser_session()
    try:
        page = ctx.new_page()
        for cinema in active_cinemas():
            print(f"\n  ── {cinema['name']}（コード {cinema['code']}）")
            check("robots.txt", robots_allows(cinema, cinema["schedule_url"]),
                  cinema["robots_url"])

            res = open_schedule(page, cinema)
            dbg = res.get("debug", {})
            check("一覧ページ", bool(dbg.get("rows")),
                  f"date={dbg.get('date')} tabs={dbg.get('tabs')} "
                  f"blocks={dbg.get('blocks')} rows={dbg.get('rows')}")
            for line in dbg.get("sample", [])[:3]:
                print(f"      回の例: {line}")
            check("日付タブ", (dbg.get("tabs") or 0) >= 2, f"{dbg.get('tabs')} 日分")

            # 今日はもう全部終わっていることがある。明日のほうが実情が分かる。
            today8 = now_jst().strftime("%Y%m%d")
            nxt = [t["date"] for t in res.get("tabs", []) if t["date"] > today8]
            if nxt:
                got = select_day(page, nxt[0])
                check("タブ切り替え", got is not None, f"{nxt[0]} を開く")
                if got is not None:
                    res, dbg = got, got["debug"]
                    print(f"      {nxt[0]} rows={dbg['rows']} href={dbg['with_href']}")

            rows = rows_to_showings(cinema, res.get("rows", []), None)
            check("行の解釈", len(rows) > 10, f"{len(rows)} 件")
            if not rows:
                continue

            titles = list(dict.fromkeys(r["film_title"] for r in rows))
            with_url = sum(1 for r in rows if r["reserve_url"])
            screens = sorted({r["screen_no"] for r in rows})
            venues = sorted({str(r["venue_name"]) for r in rows})
            seatnums = {r["screen_no"]: r["screen_seats"] for r in rows}
            dates = sorted({r["date"] for r in rows})
            print(f"      日付 {' '.join(dates)}")
            print(f"      作品 {len(titles)} 本  例: {'、'.join(t[:18] for t in titles[:3])}")
            print(f"      スクリーン {' '.join(f'{k}:{v}席' for k, v in sorted(seatnums.items()))}")
            print(f"      状態 {json.dumps(dbg.get('statuses', {}), ensure_ascii=False)}")
            # is-status-NN の対応表を育てるための材料。
            # 文字は「販売中」しか出ないので、アイコンの class まで見る。
            codes: dict[str, dict] = {}
            for raw, r in zip(res.get("rows", []), rows):
                cc = r.get("status_code")
                if not cc:
                    continue
                e = codes.setdefault(cc, {"n": 0, "text": set(), "html": ""})
                e["n"] += 1
                e["text"].add((r["availability"] or {}).get("text", ""))
                if not e["html"]:
                    e["html"] = raw.get("status_html", "")
            print("      is-status の分布:")
            for cc, e in sorted(codes.items()):
                guess = AVAIL_STATUS_MAP.get(cc)
                print(f"        {cc} x{e['n']:<3} 文字={'/'.join(sorted(e['text']))}"
                      f"  → 現在の解釈 {guess[1] + ' ' + guess[2] + '(' + guess[3] + ')' if guess else '未登録'}")
                print(f"           {e['html']}")
            unmapped = [c for c in codes if c not in AVAIL_STATUS_MAP]
            check("状態コード", not unmapped,
                  f"未登録 {unmapped}" if unmapped else f"{len(codes)} 種すべて対応表にある")

            check("スクリーン番号", "00" not in screens, " ".join(screens))
            check("館の判定", "館不明" not in venues and "None" not in venues,
                  " / ".join(venues))
            check("席数", all(seatnums.values()), "全回でページから席数が取れた")
            unknown = sum(1 for r in rows
                          if (r["availability"] or {}).get("code") == "unknown")
            guessed = sum(1 for r in rows
                          if (r["availability"] or {}).get("certainty") == "推定")
            check("空席状況", unknown == 0,
                  f"未知 {unknown} 件 / 推定 {guessed} 件 / 全 {len(rows)} 件"
                  "（推定は座席表と突き合わせて確定させる）")
            print(f"      購入リンク {with_url}/{len(rows)} 件"
                  f"（0 でもクリック経路で座席表に入れる）")

            if not deep:
                continue

            sellable = [r for r in rows
                        if (r["availability"] or {}).get("code")
                        not in ("outside", "closed", "counter")]
            target = next((r for r in sellable if r["reserve_url"]), None) or \
                (sellable[0] if sellable else None)
            if not target:
                check("座席ページ", False,
                      "販売中の回が無い（深夜や休館日だとこうなる。翌日に試す）")
                continue
            print(f"      対象: {target['date']} {target['start']} "
                  f"{target['screen']} {target['film_title'][:18]}")
            ctx.image_flags["images"] = True
            seat_page, probe = open_seat_page(page, target, cinema)
            check("座席ページ遷移", seat_page is not None, page.url)
            if seat_page is None:
                continue

            c = probe["counts"]
            total = c.get("vacant", 0) + c.get("sold", 0)
            expected = target.get("screen_seats")
            check("座席カウント", total > 0 and (not expected or abs(total - expected) <= 2),
                  f"空 {c.get('vacant',0)} + 売 {c.get('sold',0)} = {total}"
                  f"（公称 {expected}）  判定不能 {c.get('other',0)}")
            print(f"      採用シグネチャ: {probe.get('used_signature')}")
            for st, sm in (probe.get("samples") or {}).items():
                print(f"      [{st}] {sm['html'][:150]}")
            for bag, n in probe.get("unknown_samples", []):
                print(f"      判定できなかった要素 x{n}: {bag}")
            if total == 0 or (expected and abs(total - expected) > 2):
                print("      → 上の署名一覧を見て SEAT_STATE_RULES を直すこと:")
                for s in probe.get("signatures", [])[:6]:
                    print(f"        {s['count']:>4} x {s['sig']}  {s['w']}x{s['h']}"
                          f"  {s['sample'][0][:70] if s['sample'] else ''}")

            tmp = LOGS / "doctor_seat.jpg"
            rec = read_seat_page(seat_page, probe, tmp, expected)
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


def cmd_inspect(url: str | None, live: bool) -> None:
    """ページ構造を dump する。抽出がおかしくなったときの調査用。

    --live を付けると、**販売中の回がある日**まで進んでから購入導線を
    たどり、座席ページの骨格まで出す。今日はもう全部終わっていることが
    多いので、既定で翌日以降を優先して探す。
    """
    dump = ROOT / "inspect"
    dump.mkdir(exist_ok=True)
    cinema = active_cinemas()[0]
    pw, browser, ctx = browser_session()
    pages = []
    try:
        page = ctx.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            res = page.evaluate(EXTRACT_JS, js_config())
        else:
            res = open_schedule(page, cinema)
        pages.append(_dump(page, "schedule", dump))
        write_json(dump / "schedule_rows.json", res)
        print(f"  抽出 {len(res.get('rows', []))} 行  debug={res.get('debug')}")

        if not live:
            write_json(dump / "report.json", {"pages": pages})
            print(f"\n{dump}/ に出力した")
            return

        # ── 販売中の回がある日を探す ────────────────────────────
        today8 = now_jst().strftime("%Y%m%d")
        order = [t["date"] for t in res.get("tabs", []) if t["date"] >= today8]
        # 今日は売り止めのことが多いので、翌日から見る
        order = order[1:] + order[:1]

        target, best = None, None
        for date8 in order[:4]:
            got = res if res["debug"]["date"] == date8 else select_day(page, date8)
            if got is None:
                print(f"  {date8}: タブが開かない")
                continue
            rows = rows_to_showings(cinema, got.get("rows", []), None)
            sellable = [r for r in rows
                        if (r["availability"] or {}).get("code")
                        not in ("outside", "closed", "counter")]
            print(f"  {date8}: {len(rows)} 回 / 販売中 {len(sellable)} 回 / "
                  f"リンク {sum(1 for r in rows if r['reserve_url'])} 件  "
                  f"{json.dumps(got['debug'].get('statuses', {}), ensure_ascii=False)}")
            if sellable and target is None:
                target, best = sellable[0], date8
        if target is None:
            print("  販売中の回がどの日にも無い。営業時間内にもう一度試すこと")
            write_json(dump / "report.json", {"pages": pages})
            return

        print(f"\n  対象: {best} {target['start']} {target['screen']} "
              f"{target['film_title'][:20]}  reserve_url={target['reserve_url']}")
        # 日を探して回ったので、いまページは最後に見た日を表示している。
        # 対象日に戻さないと作品ブロックが見つからない（ここで null になっていた）。
        if select_day(page, best) is None:
            print(f"  {best} に戻れなかった")
        # 販売中の回の HTML をそのまま残す（購入導線の実体を見るため）
        ref = target["click_ref"]
        write_json(dump / "sellable_item.json", page.evaluate(
            r"""(ref) => {
              const blk = document.querySelector(`[id="${ref.section_id}"]`);
              if (!blk) return null;
              const it = blk.querySelectorAll('.schedule-item')[ref.item_index];
              return {block_id: blk.id,
                      item: it ? it.outerHTML.slice(0, 2000) : null,
                      block_head: blk.outerHTML.slice(0, 1200)};
            }""", ref))

        ctx.image_flags["images"] = True
        before = page.url
        seat_page, probe = open_seat_page(page, target, cinema)
        if seat_page is None:
            # クリック自体は効いていて、遷移した先で座席表が見つからない、
            # という失敗が多い（券種選択が先に挟まる等）。落ちた先を調べる。
            landed = ctx.pages[-1]
            print("  座席表を認識できなかった。落ちた先を調べる。")
            print(f"    クリック前 {before}")
            print(f"    落ちた先   {landed.url}")
            print(f"    title      {landed.title()}")
            pages.append(_dump(landed, "seat_candidate", dump))
            probe = landed.evaluate(SEAT_JS, seat_js_config())
            write_json(dump / "seat_probe.json", probe)
            print(f"    座席プローブ: 候補 {probe.get('candidates')} 件  "
                  f"{probe.get('counts')}")
            for line in (target.get("_debug", {}) or {}).get("trace", []):
                print(f"    trace: {line}")
            for sg in probe.get("signatures", [])[:10]:
                print(f"      {sg['count']:>4} x {sg['sig']}  {sg['w']}x{sg['h']}"
                      f"  →{sg['state']}  {(sg['sample'] or [''])[0][:60]}")
            skel = landed.evaluate(SKELETON_JS, [None, 6])
            (dump / "seat_skeleton.txt").write_text(skel, encoding="utf-8")
            # 次へ進むボタンがあるなら、座席表はその先にある
            print("    ボタンらしきもの:")
            for b in landed.evaluate(r"""
              () => [...document.querySelectorAll('a,button,input[type=submit],input[type=button]')]
                .map(e => ({t: (e.innerText || e.value || '').replace(/\s+/g,' ').trim().slice(0,24),
                            tag: e.tagName, cls: (e.className||'').slice(0,60),
                            href: (e.getAttribute('href')||'').slice(0,60)}))
                .filter(x => x.t).slice(0, 25)"""):
                print(f"      {b['t']:<24} {b['tag']}.{b['cls']}  {b['href']}")
            write_json(dump / "report.json", {"pages": pages})
            print("\n  → inspect/seat_skeleton.txt と inspect/seat_probe.json を見せてください")
            return

        print(f"  座席ページ: {seat_page.url}")
        pages.append(_dump(seat_page, "seat", dump))
        write_json(dump / "seat_probe.json", probe)
        print(f"  座席プローブ: {probe['counts']}  候補 {probe.get('candidates')} 件  "
              f"器={probe.get('used_signature')}")
        print(f"  期待席数: {target.get('screen_seats')}  "
              f"実測: {probe['counts'].get('vacant', 0) + probe['counts'].get('sold', 0)}")
        for st, sm in (probe.get("samples") or {}).items():
            print(f"    [{st}] {sm['html'][:160]}")
        for sg in probe.get("signatures", [])[:8]:
            print(f"    {sg['count']:>4} x {sg['sig']}  {sg['w']}x{sg['h']}  "
                  f"→{sg['state']}  {(sg['sample'] or [''])[0][:60]}")
        write_json(dump / "report.json", {"pages": pages})
        print(f"\n{dump}/ に report.json / schedule_rows.json / sellable_item.json / "
              f"seat_probe.json / html / png を出力した")
    finally:
        ctx.close(); browser.close(); pw.stop()


SKELETON_JS = r"""
(arg) => {
  const [rootSel, maxDepth] = arg;
  const root = rootSel ? document.querySelector(rootSel) : document.body;
  if (!root) return '(' + rootSel + ' が無い)';
  const sig = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    for (const c of el.classList) s += '.' + c;
    for (const a of ['src', 'alt', 'value', 'name', 'type'])
      if (el.hasAttribute(a)) s += ` [${a}="${el.getAttribute(a).slice(0, 30)}"]`;
    return s;
  };
  const lines = [];
  const walk = (el, d) => {
    const kids = [...el.children];
    let i = 0;
    while (i < kids.length) {
      const k = sig(kids[i]);
      let n = 1;
      while (i + n < kids.length && sig(kids[i + n]) === k) n++;
      lines.push('  '.repeat(d) + k + (n > 1 ? `  «×${n}»` : ''));
      if (d < maxDepth) walk(kids[i], d + 1);
      i += n;
    }
  };
  walk(root, 0);
  return lines.slice(0, 400).join('\n');
}
"""


_META_RE = re.compile(r"""<meta[^>]+charset=["']?[\w-]+["']?[^>]*>""", re.I)


def _dump(page, name: str, dump: Path) -> dict:
    # TOHO のページは Windows-31J 宣言。中身は UTF-8 で保存するので、
    # 宣言を書き換えないとブラウザで開いたとき全部化ける
    # （エンコードのバグではなく、保存した HTML の meta が嘘になるだけ）。
    html = page.content()
    if _META_RE.search(html):
        html = _META_RE.sub('<meta charset="utf-8">', html, count=1)
    else:
        html = html.replace("<head>", '<head><meta charset="utf-8">', 1)
    (dump / f"{name}.html").write_text(html, encoding="utf-8")
    try:
        page.screenshot(path=str(dump / f"{name}.png"), full_page=True)
    except Exception:
        pass
    rep = {"name": name, "url": page.url, "selectors": {}}
    for key, cands in SELECTORS.items():
        rep["selectors"][key] = []
        for s in cands:
            try:
                rep["selectors"][key].append({"selector": s, "count": page.locator(s).count()})
            except Exception:
                rep["selectors"][key].append({"selector": s, "count": -1})
    rep["class_histogram"] = page.evaluate(r"""
      () => {
        const h = {};
        for (const el of document.querySelectorAll('[class]'))
          for (const c of el.classList) h[c] = (h[c] || 0) + 1;
        return Object.fromEntries(Object.entries(h).sort((a,b)=>b[1]-a[1]).slice(0,180));
      }""")
    rep["id_samples"] = page.evaluate(r"""
      () => [...document.querySelectorAll('[id]')].map(e => e.id).slice(0, 120)""")
    return rep


# ═══════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(
        description="TOHOシネマズ梅田 座席表アーカイバ",
        epilog="まず `python toho.py doctor --deep` で取得経路を確認してください。")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "sweep", "sync", "capture", "index", "doctor",
                            "log", "inspect", "pull", "push", "repair"])
    p.add_argument("--force", action="store_true", help="sync で TTL を無視する")
    p.add_argument("--full", action="store_true",
                   help="sync で全日程を見る（既定は今日から SWEEP_DAYS 日先まで）")
    p.add_argument("--deep", action="store_true", help="doctor で座席ページまで入る")
    p.add_argument("--lead", type=int, metavar="MIN",
                   help=f"capture の対象窓を一時的に変える（既定 {CAPTURE_LEAD_MIN} 分）")
    p.add_argument("--limit", type=int, metavar="N", help="capture で最初の N 件だけ処理する")
    p.add_argument("--dry-run", action="store_true", help="取得はするがファイルを書かない")
    p.add_argument("--sweep", action="store_true",
                   help="capture で窓を無視し、先の回もベースラインとして撮る")
    p.add_argument("-n", type=int, default=40, help="log で表示する行数")
    p.add_argument("--url", help="inspect の対象 URL")
    p.add_argument("--live", action="store_true", help="inspect で座席ページまで入る")
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
        return cmd_doctor(deep=args.deep)
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

    mark_run_start()
    sync_repo = args.command == "run" and not args.no_sync and not args.dry_run
    try:
        if sync_repo:
            cmd_pull()            # 走る前にリモートの成果を取り込む
        sweep = args.sweep or args.command == "sweep"
        if args.command in ("run", "sweep", "sync"):
            cmd_sync(force=args.force or sweep, full=args.full)
        if args.command in ("run", "sweep", "capture"):
            cmd_capture(lead_min=args.lead if args.lead is not None else CAPTURE_LEAD_MIN,
                        limit=args.limit, dry_run=args.dry_run, sweep=sweep)
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