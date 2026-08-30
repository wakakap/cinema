#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hub.py — 2 つの劇場アーカイバを 1 つのディレクトリでローカル運転する。

GitHub Actions の schedule は指定どおりに来ない（実測 14〜69 分間隔、
数時間空くこともある）。開映 30 分前・5 分前という細かい時刻を当てにいく
運転とは相性が悪いので、時計を握る役をこちらへ持ってくる。

  1 日の流れ
    09:30       大範囲走査（sweep）。今日から 2 日先までの全回について
                上映一覧を取り直し、その時点の座席をベースラインとして 1 枚
    各回 30 分前  capture
    各回  5 分前  capture   ← これが実態に一番近い読み
    走査・撮影のあと  posters_shared.json と data/stats.json を作り直す

  使い方（どちらか一方でよい）
    python hub.py daemon            常駐させる。自分で時計を見て動く
    python hub.py tick              1 回ぶんの判断だけして終わる
                                    → タスクスケジューラ／cron から毎分呼ぶ

  そのほか
    python hub.py sweep             大範囲走査を今すぐ
    python hub.py build             ポスター貸し借りと統計だけ作り直す
    python hub.py status            次に何がいつ起きるかを表示
    python hub.py serve             http://localhost:8000 で index.html を開く

  注意：ブラウザから fetch() で JSON を読むので、file:// では動かない。
  閲覧は `python hub.py serve` か、任意の静的サーバ越しに。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path


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
STATE_FILE = DATA / "hub_state.json"
STATS_FILE = DATA / "stats.json"

# 大範囲走査を始める時刻（JST）。TOHO の販売開始が朝なので、それより後に
# 置く。ここを早くしすぎると「まだ売っていない」で空振りする。
SWEEP_AT = "23:00"

# tick の刻み。daemon はこの間隔で自分を叩く。
TICK_SEC = 30

# 走査・撮影に掛けてよい上限（秒）。超えたら諦めて次の tick に回す。
RUN_TIMEOUT_SEC = 3600

CINEMAS = [
    {"id": "tjoy", "dir": "tjoy", "script": "cinema.py",
     "name": "T・ジョイ梅田", "short": "T・ジョイ", "color": "#D4000F"},
    {"id": "toho", "dir": "toho", "script": "toho.py",
     "name": "TOHOシネマズ梅田", "short": "TOHO", "color": "#1B62C8"},
]

# 計画点はスクリプト側の CAPTURE_PLAN をそのまま読む（二重管理を避ける）。
# 読めなかったときだけこの値を使う。
FALLBACK_PLAN = [30, 5]
FALLBACK_TOLERANCE = 2
LATE_GRACE_MIN = 5


# ═══════════════════════════════════════════════════════════════════
#  小道具
# ═══════════════════════════════════════════════════════════════════

def now_jst() -> datetime:
    return datetime.now(JST)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    tmp.replace(path)


def say(*parts) -> None:
    print(f"[{now_jst():%m-%d %H:%M:%S}]", *parts, flush=True)


MATCH_WINDOW_DAYS = 31      # 初回上映日がこれ以上離れていたら別作品とみなす

# 突き合わせを人手で直す表。自動判定が外れた作品だけをここに書く。
#
# 題名は画面に出ているとおりに写せばよい。全角半角・記号・空白・括弧の
# 違いは title_key が吸収するので、神経質にならなくてよい。逆に、題名の
# **一部だけ** を書いても効かない（照合は完全一致）。今どう対応が付いて
# いるかは `python hub.py match` で一覧できる。
#
# 書き方はどちらも ("TOHO 側の題名", "T・ジョイ側の題名")。

# 別作品なのに合流してしまうもの。包含判定は「国宝」が
# 「国宝の秘密」に呑まれる形の誤りを完全には防げないので、その逃げ道。
NEVER_MATCH: list[tuple[str, str]] = [
    # ("国宝", "国宝の秘密 Kokuhou no Himitsu"),
]

# 同じ作品なのに合流しないもの。邦題と英題で字面が全く違う場合や、
# 片方の上映が半年遅れて MATCH_WINDOW_DAYS を超えた場合に使う。
ALWAYS_MATCH: list[tuple[str, str]] = [
    # ("８番出口", "Exit 8"),
]


def title_key(title: str | None) -> str:
    """照合用の文字列。

    NFKC で全角と半角を畳み（５→5、ﾊﾘｰ→ハリー、㈱→(株)）、そのあと
    **文字と数字だけを残す**。記号を並べて落とす黒名単は際限が無く、
    〈〉『』「」《》☆ のどれか 1 つを漏らすとその作品だけ静かに 2 本へ
    割れるので、白名単にしてある。
    """
    s = unicodedata.normalize("NFKC", title or "").casefold()
    return "".join(ch for ch in s if ch.isalnum())


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return 10 ** 6


def match_films(base: list[dict], other: list[dict]) -> dict[int, int]:
    """2 館の作品一覧を突き合わせ、base の添字 → other の添字を返す。

    ローマ字の付き方を規則で当てにいくのはやめた。「トイ・ストーリー５」と
    「トイ・ストーリー５ Toy Story 5」のように、続篇番号と併記ローマ字が
    隣り合うと、どこまでが題名でどこからが併記か文字面からは決められない。

    代わりに **片方が丸ごともう片方の中に入っているか** だけを見る。
    T・ジョイは邦題のうしろに足すだけなので、TOHO 側の鍵はほぼ必ず
    T・ジョイ側の鍵の部分文字列になる。規則を増やさずに済む。

    部分文字列だけだと「国宝」が「国宝の秘密」に呑まれる。そこで 2 つ縛る。

      ・初回上映日が MATCH_WINDOW_DAYS 日以内であること
      ・1 対 1 であること。候補が複数あるときは長さの差が最も小さいものを
        取り、取られた側は他の候補から外す

    先に鍵が完全一致するものを固めてから、残りを長い鍵の順に処理する。
    こうすると「国宝の秘密」が自分の相手を先に確保するので、「国宝」が
    横取りしにくい。
    """
    free = list(range(len(other)))
    pair: dict[int, int] = {}
    never = {(title_key(a), title_key(b)) for a, b in NEVER_MATCH}
    forced = {(title_key(a), title_key(b)): (a, b) for a, b in ALWAYS_MATCH}

    def within(a: dict, b: dict) -> bool:
        return _days_apart(a.get("first", ""), b.get("first", "")) <= MATCH_WINDOW_DAYS

    def blocked(i: int, j: int) -> bool:
        return (base[i]["key"], other[j]["key"]) in never

    seen: set[tuple[str, str]] = set()
    for i, b in enumerate(base):                      # ① 人手で指定したもの
        for j in list(free):
            if (b["key"], other[j]["key"]) in forced:
                pair[i] = j
                free.remove(j)
                seen.add((b["key"], other[j]["key"]))
                break

    for i, b in enumerate(base):                      # ② 鍵が完全に一致
        if i in pair:
            continue
        for j in free:
            if other[j]["key"] == b["key"] and within(b, other[j]) and not blocked(i, j):
                pair[i] = j
                free.remove(j)
                break

    rest = [i for i in range(len(base)) if i not in pair]
    for i in sorted(rest, key=lambda i: -len(base[i]["key"])):   # ③ 包含
        bk = base[i]["key"]
        best: tuple[int, int] | None = None
        for j in free:
            ok = other[j]["key"]
            if not bk or not ok or not (bk in ok or ok in bk):
                continue
            if not within(base[i], other[j]) or blocked(i, j):
                continue
            d = abs(len(ok) - len(bk))
            if best is None or d < best[0]:
                best = (d, j)
        if best is not None:
            pair[i] = best[1]
            free.remove(best[1])

    # 書いたのに一度も当たらなかった行は、たいてい題名の写し間違い。
    # 黙って無視すると「直したつもりで直っていない」に気付けない。
    for keys, (t_toho, t_tjoy) in forced.items():
        if keys not in seen and base and other:
            say(f"※ ALWAYS_MATCH が当たりません: 「{t_toho}」×「{t_tjoy}」"
                "（題名の写し間違いか、まだ記録が無いか）")
    return pair


def script_of(cin: dict) -> Path:
    return ROOT / cin["dir"] / cin["script"]


def plan_of(cin: dict) -> tuple[list[int], float]:
    """スクリプト側の CAPTURE_PLAN と許容幅を読む。

    劇場スクリプトの定数をそのまま読むことで、計画点を 2 か所で管理せずに
    済ませている。読めなかったときは既定値に落ちるだけで、走査は止めない。
    """
    plan = list(FALLBACK_PLAN)
    tol = float(FALLBACK_TOLERANCE)
    try:
        src = script_of(cin).read_text(encoding="utf-8")
    except OSError:
        return plan, tol

    m = re.search(r"^CAPTURE_PLAN\s*=\s*\[([^\]]*)\]", src, re.M)
    if m:
        found = [int(x) for x in m.group(1).split(",") if x.strip().isdigit()]
        if found:
            plan = found
    m = re.search(r"^PLAN_TOLERANCE_MIN\s*=\s*(\d+)", src, re.M)
    if m:
        tol = float(m.group(1))
    return plan, tol


def run_script(cin: dict, *args: str) -> int:
    """劇場スクリプトを別プロセスで叩く。

    import して呼ばないのは、片方が落ちてももう片方と hub 自身を巻き込まない
    ため。ログもそれぞれの logs/ に残るので、切り分けが楽になる。
    """
    cmd = [sys.executable, cin["script"], *args]
    say(f"→ {cin['id']}: {' '.join(args)}")
    t0 = time.time()
    try:
        rc = subprocess.run(cmd, cwd=ROOT / cin["dir"],
                            timeout=RUN_TIMEOUT_SEC).returncode
    except subprocess.TimeoutExpired:
        say(f"  {cin['id']}: 時間切れ（{RUN_TIMEOUT_SEC}s）")
        return 124
    say(f"  {cin['id']}: 終了 rc={rc} / {time.time() - t0:.0f}s")
    return rc


# ═══════════════════════════════════════════════════════════════════
#  「今、撮る回があるか」
# ═══════════════════════════════════════════════════════════════════
#
# 判定はスクリプト側（due_slot）が最終決定権を持つ。ここでは
# schedule.json を読んで「そろそろ誰か居そうか」を大まかに見るだけ。
# 二重に厳密な判定を書くと、いずれ片方だけ直して食い違う。

def pending_slots(cin: dict, now: datetime) -> list[tuple[datetime, dict, int]]:
    """これから来る計画点を (時刻, 回, 点番号) で並べる。"""
    sched = read_json(ROOT / cin["dir"] / "data" / "schedule.json", {})
    plan, _ = plan_of(cin)
    out = []
    for payload in sched.get("days", {}).values():
        for s in payload.get("showings", []):
            if s.get("status") not in ("pending", "retry", "provisional"):
                continue
            try:
                start = datetime.fromisoformat(s["start_at"])
            except (ValueError, KeyError):
                continue
            done = set(s.get("slots") or [])
            for i, lead in enumerate(plan, start=1):
                if i in done:
                    continue
                at = start - timedelta(minutes=lead)
                if at >= now - timedelta(minutes=LATE_GRACE_MIN):
                    out.append((at, s, i))
    return sorted(out, key=lambda t: t[0])


def due_count(cin: dict, now: datetime) -> int:
    """今この瞬間に撮るべき回の数（おおよそ）。

    数えるのは「回」であって「計画点」ではない。起動が遅れて 30 分前と 5 分前を
    またいだ回でも、スクリプト側は深いほうを 1 枚撮るだけなので、ここで 2 と
    数えるとログが実際と食い違う。
    """
    sched = read_json(ROOT / cin["dir"] / "data" / "schedule.json", {})
    plan, tol = plan_of(cin)
    n = 0
    for payload in sched.get("days", {}).values():
        for s in payload.get("showings", []):
            if s.get("status") not in ("pending", "retry", "provisional"):
                continue
            try:
                lead = (datetime.fromisoformat(s["start_at"]) - now).total_seconds() / 60
            except (ValueError, KeyError):
                continue
            if lead < -LATE_GRACE_MIN:
                continue
            done = set(s.get("slots") or [])
            if any(i + 1 not in done for i, p in enumerate(plan) if lead <= p + tol):
                n += 1
    return n


# ═══════════════════════════════════════════════════════════════════
#  2 館の作品を突き合わせる
# ═══════════════════════════════════════════════════════════════════

def best_sold(rec: dict) -> int | None:
    """その回で分かっている最大の販売数。

    同じ回を 3 回撮っているので、開映に一番近い読みが実態に近い。記録本体は
    その読みで上書きされているが、合流の順によっては history のほうが
    深い点を持っていることがあるので、両方から最大を取る。
    """
    raw = [(rec.get("seat_counts") or {}).get("sold")]
    raw += [h.get("sold") for h in rec.get("history") or []]
    vals: list[int] = [v for v in raw if isinstance(v, int)]
    return max(vals) if vals else None


def load_records(cin: dict) -> list[dict]:
    out = []
    for path in sorted((ROOT / cin["dir"] / "data" / "captures").glob("*.json")):
        out += read_json(path, {}).get("captures", [])
    return out


def collect(cin: dict) -> list[dict]:
    """1 館ぶんの作品一覧。題名を正規化した鍵でまとめる。

    注意：ここで数えているのは開映直前の座席表であって、興行収入ではない。
    直前・当日券の一部は入らないし、券種も区別しない。
    """
    by_key: dict[str, dict] = {}
    for r in load_records(cin):
        sold = best_sold(r)
        if sold is None:
            continue                       # 予定だけの記録は数えない
        key = title_key(r.get("film_title"))
        if not key:
            continue
        e = by_key.setdefault(key, {
            "key": key, "title": r.get("film_title"), "cinema": cin["id"],
            "first": r["date"], "last": r["date"], "poster": None,
            "film_keys": set(), "sold": 0, "shows": 0, "seats": 0, "series": {},
        })
        # 表示名は短いほうを採る。版タグの付き方が回ごとに揺れるため。
        if len(r.get("film_title") or "") < len(e["title"] or ""):
            e["title"] = r["film_title"]
        e["first"] = min(e["first"], r["date"])
        e["last"] = max(e["last"], r["date"])
        if r.get("film_key"):
            e["film_keys"].add(r["film_key"])
        if not e["poster"] and r.get("poster"):
            e["poster"] = f"{cin['dir']}/{r['poster']}"
        e["sold"] += sold
        e["shows"] += 1
        e["seats"] += (r.get("seat_counts") or {}).get("total") or 0
        e["series"][r["date"]] = e["series"].get(r["date"], 0) + sold
    return sorted(by_key.values(), key=lambda e: e["key"])


def analyze() -> tuple[list[dict], list[dict], dict[int, int]]:
    """両館の作品一覧と、その対応表。ポスターと統計の両方がこれを使う。"""
    def one(cid: str) -> list[dict]:
        cin = next((c for c in CINEMAS if c["id"] == cid), None)
        return collect(cin) if cin else []

    toho, tjoy = one("toho"), one("tjoy")
    return toho, tjoy, match_films(toho, tjoy)


# ═══════════════════════════════════════════════════════════════════
#  ポスターの貸し借り
# ═══════════════════════════════════════════════════════════════════

def build_posters(pre=None) -> int:
    """TOHO 側でポスターが取れなかった作品に、T・ジョイ側の画像を割り当てる。

    突き合わせは統計と同じ対応表を使う。film_key は劇場ごとに別物なので
    鍵にはできないが、対応が付いたあとなら TOHO 側の film_key へ引き直せる。
    結果は toho/data/posters_shared.json に置き、toho.py の rebuild_index が
    索引を作るときに拾う。相手が見つからなければ空欄のまま。
    """
    toho, tjoy, pair = pre if pre else analyze()
    borrow: dict[str, str] = {}
    for i, j in pair.items():
        poster = tjoy[j].get("poster")
        if not poster or toho[i].get("poster"):
            continue
        # 保存されている値は tjoy/ から見た相対パス。toho/index.html から
        # 辿れるように 1 段上へ戻してやる。
        rel = "../" + poster
        for key in toho[i]["film_keys"]:
            borrow[key] = rel

    write_json(ROOT / "toho" / "data" / "posters_shared.json", borrow)
    say(f"ポスター貸出 {len(borrow)} 件（対応の付いた作品 {len(pair)}）")
    return len(borrow)


# ═══════════════════════════════════════════════════════════════════
#  票の推移（data/stats.json）
# ═══════════════════════════════════════════════════════════════════

def _entry(title: str, parts: dict[str, dict]) -> dict:
    """1 作品ぶんの出力。parts は劇場 id → collect() の要素。"""
    poster = None
    for cid in ("tjoy", "toho"):           # 画像は T・ジョイ側に在ることが多い
        if parts.get(cid) and parts[cid].get("poster"):
            poster = parts[cid]["poster"]
            break
    return {
        "key": "|".join(sorted(e["key"] for e in parts.values())),
        "title": title,
        "poster": poster,
        "first": min(e["first"] for e in parts.values()),
        "last": max(e["last"] for e in parts.values()),
        "titles": {cid: e["title"] for cid, e in parts.items()},
        "totals": {cid: {"sold": e["sold"], "shows": e["shows"], "seats": e["seats"]}
                   for cid, e in parts.items()},
        "series": {cid: e["series"] for cid, e in parts.items()},
    }


def build_stats(pre=None) -> dict:
    """作品ごとに「日付 → 売れた席数」を劇場別に積む。

    注意：これは興行収入ではない。開映 5 分前の座席表を数えているだけなので、
    直前・当日券の一部は入らないし、券種（学生・レイト）も区別しない。
    「どちらの劇場でどれだけ埋まったか」の目安として読むこと。
    """
    toho, tjoy, pair = pre if pre else analyze()

    films = []
    for i, e in enumerate(toho):
        parts = {"toho": e}
        j = pair.get(i)
        if j is not None:
            parts["tjoy"] = tjoy[j]
        # 合流したときの表示名は TOHO 側を使う。ローマ字併記が無く短い。
        films.append(_entry(e["title"], parts))
    for j, e in enumerate(tjoy):
        if j not in pair.values():
            films.append(_entry(e["title"], {"tjoy": e}))

    dates = sorted({d for f in films for s in f["series"].values() for d in s})
    payload = {
        "generated_at": now_jst().isoformat(),
        "note": "開映直前の座席表から数えた販売席数。興行収入ではない。",
        "cinemas": [{k: c[k] for k in ("id", "dir", "name", "short", "color")}
                    for c in CINEMAS],
        "dates": dates,
        "films": sorted(films, key=lambda f: (f["last"],
                                              sum(t["sold"] for t in f["totals"].values())),
                        reverse=True),
    }
    write_json(STATS_FILE, payload)
    both = sum(1 for f in films if len(f["totals"]) == 2)
    say(f"統計 {len(films)} 作品（うち両館 {both}）/ {len(dates)} 日ぶん")
    return payload


def cmd_match() -> None:
    """今どう対応が付いているかを一覧する。

    NEVER_MATCH / ALWAYS_MATCH に書く題名は、ここに出るものをそのまま
    写せばよい。合流していない作品は「—」で表示される。
    """
    toho, tjoy, pair = analyze()
    used = set(pair.values())
    print(f"{'TOHO':<34}  {'T・ジョイ':<34}  初回")
    print("─" * 88)
    for i, e in enumerate(toho):
        j = pair.get(i)
        mate = tjoy[j] if j is not None else None
        gap = f"{_days_apart(e['first'], mate['first'])}日差" if mate else e["first"]
        print(f"{e['title'][:32]:<34}  {(mate['title'][:32] if mate else '—'):<34}  {gap}")
    for j, e in enumerate(tjoy):
        if j not in used:
            print(f"{'—':<34}  {e['title'][:32]:<34}  {e['first']}")
    print(f"\n両館 {len(pair)} 作品 / TOHO のみ {len(toho) - len(pair)} / "
          f"T・ジョイのみ {len(tjoy) - len(pair)}")


def cmd_build(reindex: bool = True) -> None:
    """索引 → ポスター貸出 → 索引（TOHO のみ）→ 統計、の順で作り直す。

    TOHO の索引を 2 度作るのは、貸出表が出来てからでないとポスターを
    当てられないため。索引作りは captures/ を読み直すだけなので安い。
    突き合わせは 1 度だけ行い、ポスターと統計で使い回す。
    """
    if reindex:
        for cin in CINEMAS:
            run_script(cin, "index")
    pre = analyze()
    build_posters(pre)
    if reindex:
        run_script(next(c for c in CINEMAS if c["id"] == "toho"), "index")
    build_stats(pre)


# ═══════════════════════════════════════════════════════════════════
#  時計
# ═══════════════════════════════════════════════════════════════════

def sweep_due(state: dict, now: datetime) -> bool:
    if not any((ROOT / c["dir"] / "data" / "schedule.json").exists() for c in CINEMAS):
        return True                                  # 初回は今すぐ
    if state.get("last_sweep_date") == now.date().isoformat():
        return False
    hh, mm = (int(x) for x in SWEEP_AT.split(":"))
    return (now.hour, now.minute) >= (hh, mm)


def cmd_sweep(state: dict | None = None) -> None:
    say("── 大範囲走査 ──")
    for cin in CINEMAS:
        run_script(cin, "sweep")
    cmd_build()
    if state is not None:
        state["last_sweep_date"] = now_jst().date().isoformat()
        state["last_sweep_at"] = now_jst().isoformat()
        write_json(STATE_FILE, state)


def cmd_tick(quiet: bool = False) -> None:
    """1 回ぶんの判断。タスクスケジューラ／cron から毎分呼ぶ想定。"""
    state = read_json(STATE_FILE, {})
    now = now_jst()

    if sweep_due(state, now):
        cmd_sweep(state)
        return

    worked = False
    for cin in CINEMAS:
        n = due_count(cin, now)
        if n:
            say(f"{cin['id']}: {n} 件が計画点に入った")
            run_script(cin, "capture")
            worked = True
    if worked:
        cmd_build()
    elif not quiet:
        nxt = next_event(now)
        say("待機" + (f"（次 {nxt}）" if nxt else ""))


def next_event(now: datetime) -> str | None:
    cands = []
    hh, mm = (int(x) for x in SWEEP_AT.split(":"))
    sweep_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if sweep_at <= now:
        sweep_at += timedelta(days=1)
    cands.append((sweep_at, "sweep"))
    for cin in CINEMAS:
        pend = pending_slots(cin, now)
        if pend:
            at, s, i = pend[0]
            cands.append((at, f"{cin['id']} {s['start']} {s.get('film_title', '')[:12]}"))
    if not cands:
        return None
    at, what = min(cands, key=lambda t: t[0])
    return f"{at:%H:%M} {what}"


def cmd_daemon() -> None:
    say(f"常駐開始（走査 {SWEEP_AT} / 刻み {TICK_SEC}s）。Ctrl-C で終了")
    last_idle_msg = 0.0
    while True:
        try:
            quiet = time.time() - last_idle_msg < 900     # 待機の表示は 15 分に 1 回
            cmd_tick(quiet=quiet)
            if not quiet:
                last_idle_msg = time.time()
        except KeyboardInterrupt:
            say("終了")
            return
        except Exception as exc:                          # 1 回の失敗で止めない
            say(f"tick で例外: {exc}")
        try:
            time.sleep(TICK_SEC)
        except KeyboardInterrupt:
            say("終了")
            return


def cmd_status() -> None:
    now = now_jst()
    state = read_json(STATE_FILE, {})
    print(f"現在 {now:%Y-%m-%d %H:%M} JST")
    print(f"前回の走査 {state.get('last_sweep_at', '—')}  次の走査 {SWEEP_AT}")
    for cin in CINEMAS:
        idx = read_json(ROOT / cin["dir"] / "data" / "index.json", {})
        plan, _ = plan_of(cin)
        print(f"\n■ {cin['name']}  計画点 {plan} 分前")
        print(f"   記録 {idx.get('total_records', 0)} 件"
              f"（座席表 {idx.get('total_captures', 0)}）"
              f"  作品 {len(idx.get('films', []))}")
        pend = pending_slots(cin, now)
        if not pend:
            print("   予定なし（sweep がまだか、今日の分は終わっている）")
            continue
        print(f"   これから {len(pend)} 点")
        for at, s, i in pend[:8]:
            print(f"     {at:%m-%d %H:%M}  slot{i}  "
                  f"{s['start']} {s.get('film_title', '')[:20]}")
    stats = read_json(STATS_FILE, {})
    if stats:
        print(f"\n統計 {len(stats.get('films', []))} 作品 "
              f"（{stats.get('generated_at', '')[:16]} 更新）")


def cmd_serve(port: int) -> None:
    import http.server
    import socketserver
    import functools
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(ROOT))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as srv:
        say(f"http://localhost:{port}/ で配信中。Ctrl-C で終了")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            say("終了")


# ═══════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(
        description="2 劇場の座席表アーカイバをローカルで回す",
        epilog="まず `python hub.py sweep`、そのあと `python hub.py daemon`。")
    p.add_argument("command", nargs="?", default="tick",
                   choices=["tick", "daemon", "sweep", "build", "stats",
                            "posters", "match", "status", "serve"])
    p.add_argument("--port", type=int, default=8000, help="serve の待受ポート")
    p.add_argument("--no-index", action="store_true",
                   help="build で索引の作り直しを省く")
    args = p.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)

    if args.command == "status":
        cmd_status()
    elif args.command == "serve":
        cmd_serve(args.port)
    elif args.command == "match":
        cmd_match()
    elif args.command == "posters":
        build_posters()
    elif args.command == "stats":
        build_stats()
    elif args.command == "build":
        cmd_build(reindex=not args.no_index)
    elif args.command == "sweep":
        cmd_sweep(read_json(STATE_FILE, {}))
    elif args.command == "daemon":
        cmd_daemon()
    else:
        cmd_tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
