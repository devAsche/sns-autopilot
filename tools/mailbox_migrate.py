"""mailbox を用途別のフォルダに分ける(1回だけ実行する移行スクリプト)。

前:  mailbox/to_muse/  mailbox/to_claude/  mailbox/done/  mailbox/consult/
後:  mailbox/<用途>/to_muse/  mailbox/<用途>/to_claude/  mailbox/<用途>/done/   (consult/ はそのまま)
     用途: post(投稿・投稿の数字) / bgm(BGM集め) / sns(いいね・フォロー・RT) / system(それ以外・質問と回答)

使い方(Museが実行):
  python tools/mailbox_migrate.py --dry-run        … 何をどこへ移すかを表示するだけ
  python tools/mailbox_migrate.py --job "<この作業指示ファイル>"   … 実際に移す

- 移すだけで、ファイルを消したり中身を変えたりしない。同じ名前が移動先にあれば移さずに報告する。
- 古い to_muse/ to_claude/ done/ フォルダは空にして残し、中に MOVED.txt を置く。
このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.common import OPS, now, write_json  # noqa: E402

MB = OPS / "mailbox"
CATEGORIES = ("post", "bgm", "sns", "system")
BOXES = ("to_muse", "to_claude", "done")
TYPE_CAT = {"post_tiktok": "post", "post_instagram": "post", "collect_post_stats": "post",
            "collect_bgm": "bgm",
            "engage_enqueue": "sns", "engage_probe": "sns", "engage_profile_setup": "sns"}


def job_type(p: Path) -> str | None:
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("type")
    except Exception:
        return None


def by_name(name: str) -> str:
    n = name.lower()
    if n.startswith("engage") or "engage" in n:
        return "sns"
    if n.startswith("bgm"):
        return "bgm"
    if any(k in n for k in ("post", "tiktok", "instagram", "cap-")):
        return "post"
    return "system"


def classify(files: list[Path]) -> dict[Path, str]:
    # 作業指示(to_muse・done)は type で、結果(to_claude)は同じ id の作業指示の type で決める。無ければ名前で決める
    id_cat: dict[str, str] = {}
    for p in files:
        if p.parent.name in ("to_muse", "done"):
            t = job_type(p)
            if t in TYPE_CAT:
                id_cat[p.stem] = TYPE_CAT[t]
            elif t:
                id_cat[p.stem] = by_name(p.stem)
    out = {}
    for p in files:
        stem = p.name.split(".")[0]
        out[p] = id_cat.get(stem) or by_name(p.name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--job")
    a = ap.parse_args()
    self_job = Path(a.job).resolve() if a.job else None
    files = [p for box in BOXES for p in sorted((MB / box).glob("*")) if p.is_file()
             and p.name not in (".keep", "MOVED.txt") and p.resolve() != self_job]
    plan = classify(files)
    moved, skipped = [], []
    if not a.dry_run:
        for c in CATEGORIES:
            for b in BOXES:
                (MB / c / b).mkdir(parents=True, exist_ok=True)
    for p, cat in plan.items():
        dst = MB / cat / p.parent.name / p.name
        line = f"{p.parent.name}/{p.name} → {cat}/{p.parent.name}/"
        if a.dry_run:
            print(line)
            continue
        if dst.exists():
            skipped.append(line + "(移動先に同名あり)")
            continue
        shutil.move(str(p), str(dst))
        moved.append(line)
    if a.dry_run:
        return
    for b in BOXES:
        d = MB / b
        if d.exists():
            (d / "MOVED.txt").write_text(
                "2026-11-21 から mailbox は用途別(post / bgm / sns / system)に分けた。\n"
                f"このフォルダは使わない。作業指示は mailbox/<用途>/{b}/ を見る(手順は PROTOCOL.md)。\n", encoding="utf-8")
    counts = {c: {b: len([x for x in (MB / c / b).glob("*") if x.is_file()]) for b in BOXES} for c in CATEGORIES}
    print(json.dumps({"moved": len(moved), "skipped": skipped, "counts": counts}, ensure_ascii=False, indent=1))
    if self_job:
        job = json.loads(self_job.read_text(encoding="utf-8"))
        write_json(MB / "system" / "to_claude" / f"{job['id']}.result.json", {
            "id": job["id"], "status": "done" if not skipped else "failed", "finished_at": now().isoformat(timespec="seconds"),
            "what_i_did": [f"{len(moved)} 件を用途別フォルダへ移した"] + moved[:200],
            "evidence": {"counts": counts}, "error_text": "; ".join(skipped), "not_done": skipped, "notes": ""})
        shutil.move(str(self_job), str(MB / "system" / "done" / self_job.name))


if __name__ == "__main__":
    main()
