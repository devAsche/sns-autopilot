"""Muse が用意したアカウントごとの Chrome プロファイルを調べ、state/engage_profiles.json に書く。

調べること:
  1. 起動中の chrome.exe のうち --remote-debugging-port 付きのもの(ポートと --user-data-dir)
  2. Windows のタスクのうち Chrome を起動するもの(タスク名・ポート・プロファイルの場所)
  3. 応答するポートごとに作業用タブを1枚開き、X と Instagram でログイン中のアカウント名を読む(読んだらタブを閉じる)

使い方(Museが実行):
  python tools/engage_probe.py            … 調べて結果を表示し、state/engage_profiles.json に保存
  python tools/engage_probe.py --launch   … 応答しないプロファイルを、見つかった起動タスクで起こしてから調べる
  python tools/engage_probe.py --job <作業指示ファイル>  … 結果を作業指示と同じ用途の mailbox/<用途>/to_claude/<id>.result.json にも書く

このファイルは Claude が保守する。Muse は編集しない。何かを投稿・いいね・フォローすることはない(読むだけ)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.common import OPS, now, result_dir_for, write_json  # noqa: E402
from lib.engage_common import PROFILES_STATE, cdp_alive, current_handle, new_tab  # noqa: E402

PORT_RE = re.compile(r"--remote-debugging-port[= ](\d+)")
UDD_RE = re.compile(r"--user-data-dir[= ](\"[^\"]+\"|\S+)")


def ps_json(script: str):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script + " | ConvertTo-Json -Depth 4"],
                       capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def running_chromes() -> list[dict]:
    rows = ps_json("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                   "Where-Object { $_.CommandLine -like '*remote-debugging-port*' -and $_.CommandLine -notlike '*--type=*' } | "
                   "Select-Object ProcessId, CommandLine")
    out = []
    for r in rows:
        cl = r.get("CommandLine") or ""
        m, u = PORT_RE.search(cl), UDD_RE.search(cl)
        if m:
            out.append({"port": int(m.group(1)), "user_data_dir": u.group(1).strip('"') if u else None, "pid": r.get("ProcessId")})
    return out


def chrome_tasks() -> list[dict]:
    rows = ps_json("Get-ScheduledTask | ForEach-Object { $t = $_; $t.Actions | ForEach-Object { "
                   "[pscustomobject]@{ TaskName = $t.TaskName; TaskPath = $t.TaskPath; Execute = $_.Execute; Arguments = $_.Arguments } } } | "
                   "Where-Object { ($_.Execute + ' ' + $_.Arguments) -match 'chrome|remote-debugging' -or $_.TaskName -match 'Chrome' }")
    out = []
    for r in rows:
        args = f"{r.get('Execute') or ''} {r.get('Arguments') or ''}"
        m, u = PORT_RE.search(args), UDD_RE.search(args)
        out.append({"task": r.get("TaskName"), "path": r.get("TaskPath"), "command": args.strip()[:400],
                    "port": int(m.group(1)) if m else None, "user_data_dir": u.group(1).strip('"') if u else None})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--job")
    a = ap.parse_args()
    notes: list[str] = []
    tasks = chrome_tasks()
    if a.launch:
        for t in tasks:
            if t["port"] and not cdp_alive(t["port"]):
                r = subprocess.run(["schtasks", "/run", "/tn", t["task"]], capture_output=True, text=True, timeout=60)
                notes.append(f"起動: {t['task']} → {(r.stdout + r.stderr).strip()[:200]}")
        time.sleep(15)
    procs = running_chromes()
    ports: dict[int, dict] = {}
    for p in procs:
        ports.setdefault(p["port"], {"port": p["port"], "user_data_dir": p["user_data_dir"]})
    for t in tasks:
        if t["port"]:
            e = ports.setdefault(t["port"], {"port": t["port"], "user_data_dir": t["user_data_dir"]})
            e["launch_task"] = t["task"]
            e.setdefault("user_data_dir", t["user_data_dir"])

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for port, e in sorted(ports.items()):
            e["alive"] = cdp_alive(port)
            if not e["alive"]:
                continue
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30_000)
                page = new_tab(browser)
                try:
                    e["x_handle"] = current_handle(page, "x")
                    time.sleep(2)
                    e["ig_handle"] = current_handle(page, "instagram")
                finally:
                    page.close()
            except Exception as ex:
                e["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    result = {"probed_at": now().isoformat(timespec="minutes"), "profiles": list(ports.values()),
              "chrome_tasks": tasks, "notes": notes}
    write_json(PROFILES_STATE, result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if a.job:
        job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        found = [p for p in ports.values() if p.get("x_handle") or p.get("ig_handle")]
        write_json(result_dir_for(a.job, "sns") / f"{job['id']}.result.json", {
            "id": job["id"], "status": "done" if found else "failed",
            "finished_at": now().isoformat(timespec="seconds"),
            "what_i_did": ["tools/engage_probe.py を実行した"],
            "evidence": {"profiles_file": str(PROFILES_STATE)},
            "error_text": "" if found else "ログイン中のアカウントを1つも読めなかった",
            "not_done": [], "notes": "; ".join(f"{p['port']}: X={p.get('x_handle')} IG={p.get('ig_handle')}" for p in ports.values()),
        })


if __name__ == "__main__":
    main()
