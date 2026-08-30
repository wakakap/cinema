#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_bad_shots.py — 座席図として保存された画像を点検する。

「座席表が出た」の判定が緩かったころ、一覧ページや規約ページに留まったまま
全画面写真を座席図として保存していた。判定は直したが、既に保存された画像は
残っている。それを見つけるための道具。

**既定では何も消さない。まず report を見ること。**

全画面写真は縦に異様に長い（一覧ページなら 1000×14000 のような形）。
座席図は横長で、縦横比はせいぜい 2 くらい。だから縦横比がいちばん素直な
手掛かりになる。バイト数は劇場や画質設定で桁が変わるので当てにならない。

    python cleanup_bad_shots.py                    全部の寸法を一覧する
    python cleanup_bad_shots.py --tall 3           縦横比 3 以上を候補として表示
    python cleanup_bad_shots.py --tall 3 --apply   それを消す

条件は重ねられる。指定したものだけが候補になる。

    --tall N        高さ÷幅 が N 以上
    --zero-seats    seat_counts.total が 0（座席を 1 席も読めていない）
    --bytes N       N バイト以上
    --cinema tjoy   片方だけ見る
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIRS = ["tjoy", "toho"]


def jpeg_size(path: Path) -> tuple[int, int] | None:
    """JPEG の幅と高さをヘッダから読む（外部ライブラリを使わない）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        try:
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        except struct.error:
            return None
        # SOF0..SOF15（DHT/JPG/DAC を除く）に寸法が入っている
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return None


def collect(dirname: str) -> list[dict]:
    """その館の座席図を、記録と突き合わせて一覧にする。"""
    out: list[dict] = []
    caps = ROOT / dirname / "data" / "captures"
    if not caps.exists():
        return out
    for path in sorted(caps.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  !! {path} が読めない: {exc}")
            continue
        for rec in data.get("captures", []):
            img = rec.get("seat_image")
            if not img:
                continue
            f = ROOT / dirname / img
            size = jpeg_size(f)
            out.append({
                "dir": dirname, "file": path, "rec": rec, "path": f,
                "w": size[0] if size else 0,
                "h": size[1] if size else 0,
                "ratio": (size[1] / size[0]) if size and size[0] else 0.0,
                "bytes": f.stat().st_size if f.exists() else 0,
                "total": (rec.get("seat_counts") or {}).get("total") or 0,
                "full_house": bool(rec.get("full_house")),
            })
    return out


def show(items: list[dict], title: str) -> None:
    print(f"\n■ {title}  {len(items)} 枚")
    if not items:
        return
    print(f"   {'日付':<11}{'時刻':<6}{'寸法':>12}{'縦横比':>7}{'KB':>7}{'座席':>6}  作品")
    for e in sorted(items, key=lambda x: -x["ratio"]):
        r = e["rec"]
        mark = " ←満席" if e["full_house"] else ""
        print(f"   {r.get('date','?'):<11}{r.get('start','?'):<6}"
              f"{e['w']:>5}×{e['h']:<6}{e['ratio']:>7.2f}"
              f"{e['bytes'] // 1024:>7}{e['total']:>6}  "
              f"{(r.get('film_title') or '')[:22]}{mark}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="座席図として保存された画像を点検する",
        epilog="条件を 1 つも指定しなければ、消さずに一覧するだけ。")
    ap.add_argument("--tall", type=float, metavar="N",
                    help="高さ÷幅 が N 以上のものを候補にする（全画面写真は 3 を超える）")
    ap.add_argument("--zero-seats", action="store_true",
                    help="座席を 1 席も読めていない記録を候補にする")
    ap.add_argument("--bytes", type=int, metavar="N",
                    help="N バイト以上のものを候補にする")
    ap.add_argument("--cinema", choices=DIRS, help="片方だけ見る")
    ap.add_argument("--apply", action="store_true", help="候補を実際に消す")
    args = ap.parse_args()

    dirs = [args.cinema] if args.cinema else DIRS
    everything: list[dict] = []
    for d in dirs:
        everything += collect(d)

    if not (args.tall or args.zero_seats or args.bytes):
        for d in dirs:
            show([e for e in everything if e["dir"] == d], d)
        print("\n縦横比が大きいものが全画面写真の疑い。中身を見て確かめてから、")
        print("  python cleanup_bad_shots.py --tall 3        候補を絞る")
        print("  python cleanup_bad_shots.py --tall 3 --apply  消す")
        return 0

    def suspect(e: dict) -> bool:
        if e["full_house"]:
            return False                      # 満席の既製図は対象外
        if args.tall and e["ratio"] >= args.tall:
            return True
        if args.zero_seats and e["total"] == 0:
            return True
        if args.bytes and e["bytes"] >= args.bytes:
            return True
        return False

    hits = [e for e in everything if suspect(e)]
    for d in dirs:
        show([e for e in hits if e["dir"] == d], f"{d}（候補）")

    if not hits:
        print("\n条件に当てはまる画像は無かった。")
        return 0
    if not args.apply:
        print(f"\n合計 {len(hits)} 枚。消すなら同じ条件に --apply を付けて実行。")
        return 0

    gone: dict[Path, set[str]] = {}
    for e in hits:
        if e["path"].exists():
            e["path"].unlink()
        gone.setdefault(e["file"], set()).add(e["rec"].get("id"))

    for path, ids in gone.items():
        data = json.loads(path.read_text(encoding="utf-8"))
        for rec in data.get("captures", []):
            if rec.get("id") in ids:
                rec.pop("seat_image", None)
                rec.pop("seat_image_bytes", None)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)

    print(f"\n{len(hits)} 枚を消して記録から外した。索引を作り直すこと:")
    print("   python tjoy/cinema.py index && python toho/toho.py index")
    return 0


if __name__ == "__main__":
    sys.exit(main())