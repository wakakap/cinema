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
from datetime import datetime, timedelta, timezone, tzinfo
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
SWEEP_AT = "09:30"

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


_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_DROP_RE = re.compile(r"[\s\u3000・･:：!！?？\-–—〜~、,。.'\"“”‘’()（）\[\]【】/／]+")
_JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff々〆ヶ]")
ROMAJI_TAIL_MIN = 3


def norm_title(title: str | None) -> str:
    """劇場をまたいで作品を突き合わせるための鍵。

    film_key は使えない。TOHO は「Ｍｉｃｈａｅｌ／マイケル」と全角で書き、
    T・ジョイは半角で書くので、同じ作品でも別の鍵になる。

    落とす記号を並べる方式（黒名単）はやめた。〈〉『』「」《》☆ のように
    題名に現れる括弧や記号は際限が無く、1 つ漏らすとその作品だけ静かに
    2 本へ割れる。実際「〈ワルプルギスの廻天〉」と「『賢者の石』」がそれで
    外れていた。**文字と数字だけを残す**白名単に変えれば、漏れは起きない。

    正規化は NFKC に任せる。全角英数、半角カナ（ﾊﾘｰ→ハリー）、㈱ や Ⅲ まで
    まとめて畳んでくれるので、自前の変換表より取りこぼしが少ない。

    そのうえで、T・ジョイが「国宝 Kokuhou」のように邦題のうしろへ足す
    ローマ字を削る。TOHO は足さないため、これが残ると合流しない。
    削るのは次をすべて満たすときだけ。

      ・題に日本語（漢字・かな）が含まれている
      ・最後の日本語文字より後ろが ASCII だけで出来ている
      ・その長さが ROMAJI_TAIL_MIN 文字以上
      ・そこに 1 文字でもアルファベットがある

    最後の条件は数字だけの尾を守るためのもの。これが無いと
    「機動戦士ガンダム 0079」と「0083」が同じ鍵になってしまう。

    表示に使う題名はどちらの館も元のまま。ここで作るのは照合用の鍵だけ。
    """
    s = unicodedata.normalize("NFKC", title or "").casefold()
    s = "".join(ch for ch in s if ch.isalnum())     # 記号・空白・括弧を捨てる
    last = None
    for last in _JA_RE.finditer(s):
        pass
    if last:
        tail = s[last.end():]
        if (len(tail) >= ROMAJI_TAIL_MIN and tail.isascii()
                and any(c.isalpha() for c in tail)):
            s = s[:last.end()]
    return s


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
#  ポスターの貸し借り
# ═══════════════════════════════════════════════════════════════════

def build_posters() -> int:
    """TOHO 側でポスターが取れなかった作品に、T・ジョイ側の画像を割り当てる。

    突き合わせは題名の正規化だけで行う。film_key は劇場ごとに別物なので
    使えない。結果は toho/data/posters_shared.json に置き、toho.py の
    rebuild_index が索引を作るときに拾う。見つからなければ空欄のまま。
    """
    tjoy = next(c for c in CINEMAS if c["id"] == "tjoy")
    toho = next(c for c in CINEMAS if c["id"] == "toho")

    lend: dict[str, str] = {}
    for key, f in read_json(ROOT / tjoy["dir"] / "data" / "films.json", {}).items():
        poster = f.get("poster")
        if poster:
            # 保存されている値は tjoy/ から見た相対パス。toho/index.html
            # から辿れるように 1 段上へ戻してやる。
            lend.setdefault(norm_title(f.get("title")), f"../{tjoy['dir']}/{poster}")

    borrow: dict[str, str] = {}
    for key, f in read_json(ROOT / toho["dir"] / "data" / "films.json", {}).items():
        if f.get("poster") and not f.get("poster_from"):
            continue
        hit = lend.get(norm_title(f.get("title")))
        if hit:
            borrow[key] = hit

    write_json(ROOT / toho["dir"] / "data" / "posters_shared.json", borrow)
    say(f"ポスター貸出 {len(borrow)} 作品（T・ジョイ側の在庫 {len(lend)}）")
    return len(borrow)


# ═══════════════════════════════════════════════════════════════════
#  票の推移（data/stats.json）
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


def build_stats() -> dict:
    """作品ごとに「日付 → 売れた席数」を劇場別に積む。

    注意：これは興行収入ではない。開映 5 分前の座席表を数えているだけなので、
    直前・当日券の一部は入らないし、券種（学生・レイト）も区別しない。
    「どちらの劇場でどれだけ埋まったか」の目安として読むこと。
    """
    films: dict[str, dict] = {}
    all_dates: set[str] = set()

    for cin in CINEMAS:
        for r in load_records(cin):
            sold = best_sold(r)
            if sold is None:
                continue                       # 予定だけの記録は数えない
            nkey = norm_title(r.get("film_title"))
            if not nkey:
                continue
            e = films.setdefault(nkey, {
                "key": nkey, "title": r.get("film_title"), "poster": None,
                "first": r["date"], "last": r["date"],
                "totals": {}, "series": {},
            })
            # 表示名は短いほうを採る。TOHO 側は版タグが混ざりやすい。
            if len(r.get("film_title") or "") < len(e["title"] or ""):
                e["title"] = r["film_title"]
            e["first"] = min(e["first"], r["date"])
            e["last"] = max(e["last"], r["date"])

            if not e["poster"] and r.get("poster"):
                e["poster"] = f"{cin['dir']}/{r['poster']}"

            tot = e["totals"].setdefault(cin["id"], {"sold": 0, "shows": 0, "seats": 0})
            tot["sold"] += sold
            tot["shows"] += 1
            tot["seats"] += (r.get("seat_counts") or {}).get("total") or 0

            day = e["series"].setdefault(cin["id"], {})
            day[r["date"]] = day.get(r["date"], 0) + sold
            all_dates.add(r["date"])

    # ポスターは T・ジョイ側の在庫を優先で埋め直す（TOHO 側は空が多い）
    lend: dict[str, str] = {}
    for cin in CINEMAS:
        for f in read_json(ROOT / cin["dir"] / "data" / "films.json", {}).values():
            if f.get("poster") and not f.get("poster_from"):
                lend.setdefault(norm_title(f.get("title")), f"{cin['dir']}/{f['poster']}")
    for nkey, e in films.items():
        if not e["poster"]:
            e["poster"] = lend.get(nkey)

    payload = {
        "generated_at": now_jst().isoformat(),
        "note": "開映直前の座席表から数えた販売席数。興行収入ではない。",
        "cinemas": [{k: c[k] for k in ("id", "dir", "name", "short", "color")}
                    for c in CINEMAS],
        "dates": sorted(all_dates),
        "films": sorted(films.values(),
                        key=lambda f: (f["last"], sum(t["sold"] for t in f["totals"].values())),
                        reverse=True),
    }
    write_json(STATS_FILE, payload)
    say(f"統計 {len(payload['films'])} 作品 / {len(payload['dates'])} 日ぶん")
    return payload


def cmd_build(reindex: bool = True) -> None:
    """索引 → ポスター貸出 → 索引（TOHO のみ）→ 統計、の順で作り直す。

    TOHO の索引を 2 度作るのは、貸出表が出来てからでないとポスターを
    当てられないため。索引作りは captures/ を読み直すだけなので安い。
    """
    if reindex:
        for cin in CINEMAS:
            run_script(cin, "index")
    build_posters()
    if reindex:
        run_script(next(c for c in CINEMAS if c["id"] == "toho"), "index")
    build_stats()


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
                            "posters", "status", "serve"])
    p.add_argument("--port", type=int, default=8000, help="serve の待受ポート")
    p.add_argument("--no-index", action="store_true",
                   help="build で索引の作り直しを省く")
    args = p.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)

    if args.command == "status":
        cmd_status()
    elif args.command == "serve":
        cmd_serve(args.port)
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