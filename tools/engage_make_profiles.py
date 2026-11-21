"""反応の仕組み用に、専用の Chrome プロファイルを作って起動タスクを登録し、ログイン画面を開く。

対象: config/engage.yaml のアカウントのうち、create_profile: true のもの(user_data_dir・cdp_port・launch_task を書いてあるもの)

使い方(Museが実行):
  python tools/engage_make_profiles.py --job "<作業指示ファイル>"
      … フォルダを作り、Windows タスク(launch_task)を登録し、各 Chrome をログイン画面で開く。
        ログインは本人が行う(このスクリプトもMuseも、ID・パスワードを入力しない)
  python tools/engage_make_profiles.py --check [--job "<作業指示ファイル>"]
      … 本人のログイン後に、各 Chrome を起動してログイン中のアカウントが設定どおりか確かめる

いいね・フォロー・投稿は一切しない。このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.common import load_config, now, result_dir_for, write_json  # noqa: E402
from lib.engage_common import cdp_alive, current_handle, new_tab  # noqa: E402
from tools.engage_setup_profiles import register_task  # noqa: E402

LOGIN_URL = {"x": "https://x.com/login", "instagram": "https://www.instagram.com/accounts/login/"}


def targets() -> list[dict]:
    return [a for a in load_config("engage").get("accounts", []) if a.get("create_profile")]


def make(notes: list, did: list) -> None:
    for a in targets():
        udd, port, task = a.get("user_data_dir"), a.get("cdp_port"), a.get("launch_task")
        if not (udd and port and task):
            notes.append(f"{a['id']}: user_data_dir / cdp_port / launch_task のどれかが設定に無い")
            continue
        Path(udd).mkdir(parents=True, exist_ok=True)
        did.append(register_task(task, int(port), udd, "Default", LOGIN_URL[a["platform"]]))
        if not cdp_alive(int(port)):
            r = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True, timeout=60)
            did.append(f"{a['id']} (@{a['handle']}): ログイン画面で起動 → {(r.stdout + r.stderr).strip()[:120]}")
            time.sleep(3)
        else:
            did.append(f"{a['id']} (@{a['handle']}): すでに起動している(ポート {port})")


def check(notes: list, did: list) -> dict:
    out = {}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for a in targets():
            port, task = int(a["cdp_port"]), a["launch_task"]
            if not cdp_alive(port):
                subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True, timeout=60)
                for _ in range(45):
                    if cdp_alive(port, 3):
                        break
                    time.sleep(2)
                time.sleep(6)
            try:
                b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30_000)
                page = new_tab(b)
                try:
                    got = current_handle(page, a["platform"])
                finally:
                    page.close()
            except Exception as ex:
                got = None
                notes.append(f"{a['id']}: {type(ex).__name__}: {str(ex)[:150]}")
            ok = bool(got) and got.lower() == a["handle"].lower()
            out[a["id"]] = f"@{a['handle']} → {'OK' if ok else 'NG(' + str(got) + ')'}"
            if not ok:
                notes.append(f"{a['id']}: ログイン中が {got}(期待は @{a['handle']})。本人がこの Chrome でログインする必要がある")
    did.append("各 Chrome のログイン中アカウントを確かめた")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    notes: list[str] = []
    did: list[str] = []
    evidence = check(notes, did) if a.check else (make(notes, did) or {})
    print(json.dumps({"did": did, "evidence": evidence, "notes": notes}, ensure_ascii=False, indent=1))
    if a.job:
        job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        write_json(result_dir_for(a.job, "sns") / f"{job['id']}.result.json", {
            "id": job["id"], "status": "failed" if notes else "done", "finished_at": now().isoformat(timespec="seconds"),
            "what_i_did": did, "evidence": evidence, "error_text": "; ".join(notes), "not_done": [],
            "notes": "" if a.check else "各 Chrome がログイン画面で開いている。ログインは本人が行う"})


if __name__ == "__main__":
    main()
