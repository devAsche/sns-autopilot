"""夜間の収集をまとめて実行し、Claudeが朝に読む小さな要約(digest.json)を作る。

使い方:  python run_night.py            … 全部
         python run_night.py mix_x      … 指定した収集だけ(動作確認用)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collectors import (doce_competitors, ds_signals, ds_store, engage,  # noqa: E402
                        mix_inbox, mix_jobs, mix_x)
from lib.common import (DATA_DIR, OPS, keep_awake, load_config, log, now,  # noqa: E402
                        open_browser, read_json, safe_run, today, trim, write_json)

# メールとXのDMは朝のClaudeがGmailコネクタで確認する(本人の選択)。mix_inbox は予備として残すが実行しない
# ストアの見回りは朝のClaudeがShopifyコネクタで行う(本人の選択)。ds_store は予備として残すが実行しない
NO_BROWSER = []
BROWSER = [
    ("mix_x", mix_x.run, "mix"),
    ("mix_jobs", mix_jobs.run, "mix"),
    ("doce_competitors", doce_competitors.run, "doce"),
    ("ds_signals", ds_signals.run, "dropship"),
]
# 反応(いいね・フォロー・RT)の候補集め。検索用ブラウザではなく、Museのアカウント別Chromeプロファイルに自分でつなぐ。
# 他の収集が終わってから最後に行う(結果は data/<日付>/engage.json。digest には件数だけ載る)
AFTER = [
    ("engage", engage.run, "engage"),
]


def pending_mix_leads(new_items: list[dict]) -> int:
    return sum(1 for i in new_items if i.get("is_new"))


def top_per_site(items: list[dict], n: int) -> list[dict]:
    """案件は1つのサイトに偏らないよう、サイトごとに上位n件ずつ取る(並びは各サイト内の順位のまま)。"""
    out, count = [], {}
    for it in items:
        site = it.get("site", "")
        if count.get(site, 0) < n:
            out.append(it)
            count[site] = count.get(site, 0) + 1
    return out


def build_digest(results: dict) -> dict:
    """Claudeのトークンを節約するため、判断に必要な分だけに絞る。"""
    g = lambda k: results.get(k, {})
    ds = g("ds_signals").get("sources", {})
    doce = g("doce_competitors")
    digest = {
        "date": today(),
        "built_at": now().isoformat(),
        "status": {k: {"status": v.get("status"), "items": len(v.get("items", [])), "notes": v.get("notes", [])[:6]}
                   for k, v in results.items()},
        "mix": {
            "x_leads_new": [i for i in g("mix_x").get("items", []) if i.get("is_new")][:15],
            "x_leads_seen_count": sum(1 for i in g("mix_x").get("items", []) if not i.get("is_new")),
            "jobs": [dict(j, summary=trim(j["summary"], 180)) for j in top_per_site(g("mix_jobs").get("items", []), 5)],
            "inbox_unreplied": g("mix_inbox").get("items", [])[:12],
        },
        "dropship": {
            "store_issues": g("ds_store").get("notes", []),
            "amazon_movers_top": [a for a in ds.get("amazon", [])][:25],
            "tiktok_top": ds.get("tiktok", [])[:15],
            "meta_ads": ds.get("meta", []),
            "kalodata_rows": [r for p in ds.get("kalodata", []) for r in p.get("rows", [])][:15],
            "fastmoss_rows": [r for p in ds.get("fastmoss", []) for r in p.get("rows", [])][:15],
            "instagram_rows": [r for p in ds.get("instagram", []) for r in p.get("rows", [])][:10],
            "instagram_links": [l for p in ds.get("instagram", []) for l in p.get("links", [])][:20],
        },
        "doce": {
            "competitor_outliers": doce.get("items", [])[:12],
            "genre_trends_this_week": doce.get("trends", [])[:15],
        },
        "mailbox_to_claude": {c: sorted(p.name for p in (OPS / "mailbox" / c / "to_claude").glob("*.json"))
                              for c in ("post", "bgm", "sns", "system")},
    }
    return digest


def main(only: list[str]) -> None:
    keep_awake(True)
    log(f"=== night run {today()} start ===")
    results = {}
    for name, fn, cfgname in NO_BROWSER:
        if not only or name in only:
            results[name] = safe_run(name, fn, None, load_config(cfgname))
    todo = [b for b in BROWSER if not only or b[0] in only]
    if todo:
        try:
            pw, ctx = open_browser()
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            hint = ("検索用のChrome(browser/research)が開いたままの可能性。そのウィンドウを閉じてから再実行する"
                    if ("既存のブラウザ" in msg or "existing browser" in msg or "Target page, context or browser has been closed" in msg)
                    else "ブラウザを起動できなかった")
            log(f"browser launch failed: {hint} / {msg}")
            for name, _, _ in todo:
                results[name] = {"status": "error", "items": [], "notes": [hint, msg], "collector": name}
                write_json(DATA_DIR / f"{name}.json", results[name])
            todo = []
        else:
            try:
                for name, fn, cfgname in todo:
                    results[name] = safe_run(name, fn, ctx, load_config(cfgname))
            finally:
                ctx.close()
                pw.stop()
    for name, fn, cfgname in AFTER:
        if not only or name in only:
            results[name] = safe_run(name, fn, None, load_config(cfgname))
    if not only:
        # 部分実行の日に既存の結果を消さないよう、保存済みの結果も読み込んでから要約する
        for p in DATA_DIR.glob("*.json"):
            if p.stem not in results and p.stem not in ("digest", "status"):
                results[p.stem] = read_json(p, {})
        digest = build_digest(results)
        write_json(DATA_DIR / "digest.json", digest)
        write_json(DATA_DIR / "status.json", digest["status"])
        write_json(OPS / "state" / "last_night_run.json", {"date": today(), "finished_at": now().isoformat()})
    log(f"=== night run end ===")
    if not only:
        sch = load_config("schedule")
        until = (sch.get("stay_awake_overrides") or {}).get(today(), sch.get("stay_awake_until", "09:30"))
        hold_awake_until(until)


def hold_awake_until(hhmm: str) -> None:
    """朝のClaudeの確認が終わるまでPCを起こしておく(この間もスリープ防止を維持)。"""
    import time
    h, m = map(int, hhmm.split(":"))
    end = now().replace(hour=h, minute=m, second=0, microsecond=0)
    if now() >= end:
        return
    log(f"{hhmm} までスリープ防止を維持します")
    while now() < end:
        keep_awake(True)
        time.sleep(300)
    keep_awake(False)


if __name__ == "__main__":
    main(sys.argv[1:])
