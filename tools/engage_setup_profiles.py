"""Muse が書いた「どのアカウントがどの Chrome プロファイルか」(state/engage_profile_map.json)をもとに、
各アカウントの Chrome を決まったポートで起動できるようにし、ログイン中のアカウントを確かめる。

使い方(Museが実行):
  python tools/engage_setup_profiles.py --job "<作業指示ファイル>"
  python tools/engage_setup_profiles.py --check     … 登録済みの各アカウントを起動・確認だけする(変更しない)

やること(アカウントごと):
  1. すでに --remote-debugging-port 付きで起動するタスクがあれば(map の port と launched_by)、それをそのまま使う
  2. 無ければ Windows タスク「Ops-Chrome-<id>」を登録する(ポート固定。プロファイルの場所はMuseのものをそのまま使う)
     - 1つのユーザーデータフォルダに複数アカウントのプロファイルが入っている場合だけ、同時に動かせないので
       C:\\Users\\<ユーザー>\\Chrome-Engage\\<id> に複製して使う(複製元のChromeはいったん閉じる)
  3. 起動してつなぎ、X/Instagram でログイン中のアカウント名を読んで、map の handle と一致するか確かめる
  4. 結果を state/engage_accounts_auto.json に書く(収集・実行スクリプトはここから接続先を読む)

いいね・フォロー・投稿は一切しない。このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.common import OPS, now, read_json, write_json  # noqa: E402
from lib.engage_common import (ACCOUNTS_AUTO, PROFILE_MAP, _kill_profile_chrome, cdp_alive,  # noqa: E402
                               current_handle, load_config, new_tab)

CHROME = next((p for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                           os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")] if Path(p).exists()),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FLAGS = ("--no-first-run --disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows "
         "--disable-renderer-backgrounding --disable-background-timer-throttling")
BASE_PORT = 9231
CLONE_ROOT = Path(os.path.expandvars(r"%USERPROFILE%")) / "Chrome-Engage"
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache", "ShaderCache", "Crashpad",
             "CacheStorage", "ScriptCache", "blob_storage"}


def ps(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script],
                       capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    return (r.stdout + r.stderr).strip()


def register_task(name: str, port: int, udd: str, profile_dir: str, start_url: str) -> str:
    args = f'--remote-debugging-port={port} --user-data-dir="{udd}" --profile-directory="{profile_dir}" {FLAGS} {start_url}'
    a = args.replace("'", "''")
    return ps(
        "$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        "$p = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew; "
        f"$a = New-ScheduledTaskAction -Execute '{CHROME}' -Argument '{a}'; "
        f"Register-ScheduledTask -TaskName '{name}' -Action $a -Settings $s -Principal $p "
        f"-Description 'ops: 反応の仕組み用 Chrome ({name})' -Force | Out-Null; 'registered {name}'")


def clone_profile(src_udd: str, profile_dir: str, dst: Path) -> str:
    if dst.exists():
        return f"複製先 {dst} はすでにあるので、そのまま使う"
    if "\\google\\chrome\\user data" in src_udd.lower():
        # 本人がふだん使う Chrome のデータ。勝手に閉じない(使用中だと複製に失敗するので、そのときは本人に閉じてもらう)
        n = "0(本人のChromeなので閉じていない)"
    else:
        n = _kill_profile_chrome(src_udd)
        time.sleep(4)
    dst.mkdir(parents=True)
    try:
        shutil.copy2(Path(src_udd) / "Local State", dst / "Local State")
        shutil.copytree(Path(src_udd) / profile_dir, dst / "Default",
                        ignore=lambda d, names: [x for x in names if x in SKIP_DIRS])
    except Exception as ex:
        shutil.rmtree(dst, ignore_errors=True)  # 作りかけの複製先(このスクリプトが今作ったもの)だけを片付ける
        raise RuntimeError(f"複製に失敗({type(ex).__name__}: {str(ex)[:200]})。複製元のChromeが開いていればすべて閉じてから再実行する")
    return f"{src_udd}\\{profile_dir} を {dst} に複製した(複製元のChromeを {n} プロセス終了)"


def assign(cfg: dict, entries: list[dict], auto: dict) -> list[tuple[dict, dict]]:
    """map の各行を config のアカウントに割り当てる。Xはハンドルで、Instagramはハンドル一致か空いている枠に順に。"""
    out, used = [], set()
    accts = cfg.get("accounts", [])
    for e in entries:
        h = (e.get("handle") or "").lstrip("@")
        e["handle"] = h
        hit = next((a for a in accts if a["platform"] == e.get("platform") and a["id"] not in used and
                    (a.get("handle") or auto.get(a["id"], {}).get("handle") or "").lower() == h.lower()), None)
        if not hit and e.get("platform") == "instagram":
            hit = next((a for a in accts if a["platform"] == "instagram" and a["id"] not in used and
                        not a.get("handle") and not auto.get(a["id"], {}).get("handle")), None)
        if hit:
            used.add(hit["id"])
            out.append((hit, e))
    return out


def verify(pw, acct_id: str, platform: str, port: int, task: str | None, want: str, notes: list) -> str | None:
    if not cdp_alive(port):
        if not task:
            notes.append(f"{acct_id}: ポート {port} が応答せず、起動タスクも無い")
            return None
        subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True, timeout=60)
        for _ in range(45):
            if cdp_alive(port, 3):
                break
            time.sleep(2)
        else:
            notes.append(f"{acct_id}: {task} を起動したが 90 秒たってもポート {port} が応答しない")
            return None
        time.sleep(8)
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30_000)
    page = new_tab(browser)
    try:
        return current_handle(page, platform)
    finally:
        page.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    cfg = load_config("engage")
    auto = read_json(ACCOUNTS_AUTO, {})
    notes: list[str] = []
    did: list[str] = []
    if a.check:
        pairs = [(next(x for x in cfg["accounts"] if x["id"] == k), {"platform": v["platform"], "handle": v["handle"],
                  "port": v["cdp_port"], "launched_by": v["launch_task"], "user_data_dir": v.get("user_data_dir")})
                 for k, v in auto.items() if any(x["id"] == k for x in cfg["accounts"])]
    else:
        m = read_json(PROFILE_MAP, {})
        entries = m.get("accounts", [])
        if not entries:
            raise SystemExit(f"{PROFILE_MAP} が無いか空。Muse が先に書く")
        pairs = assign(cfg, entries, auto)
        for e in entries:
            if not any(e is p[1] for p in pairs):
                notes.append(f"設定に枠が無いので使わない: {e.get('platform')} @{e.get('handle')}")
        dirs: dict[str, int] = {}
        for _, e in pairs:
            k = (e.get("user_data_dir") or "").lower()
            dirs[k] = dirs.get(k, 0) + 1
        order = [x["id"] for x in cfg["accounts"]]
        for acct, e in pairs:
            if e.get("port") and e.get("launched_by"):
                continue  # 既存のポート付き起動をそのまま使う
            udd, pdir = e.get("user_data_dir"), e.get("profile_directory") or "Default"
            if not udd or not Path(udd).exists():
                notes.append(f"{acct['id']}: プロファイルの場所 {udd} が見つからない")
                e["skip"] = True
                continue
            if dirs[udd.lower()] > 1 or pdir != "Default" or "\\google\\chrome\\user data" in udd.lower():
                dst = CLONE_ROOT / acct["id"]
                try:
                    did.append(clone_profile(udd, pdir, dst))
                except Exception as ex:
                    notes.append(f"{acct['id']}: {ex}")
                    e["skip"] = True
                    continue
                udd, pdir = str(dst), "Default"
            else:
                if not cdp_alive(BASE_PORT + order.index(acct["id"])):
                    n = _kill_profile_chrome(udd)  # ポート無しで開いていると、新しい起動がそちらに吸われるため
                    if n not in ("0", ""):
                        did.append(f"{acct['id']}: ポート無しで開いていた {udd} のChromeを終了({n})")
                        time.sleep(3)
            port = BASE_PORT + order.index(acct["id"])
            task = f"Ops-Chrome-{acct['id']}"
            start = "https://x.com/home" if acct["platform"] == "x" else "https://www.instagram.com/"
            did.append(register_task(task, port, udd, pdir, start))
            e.update(port=port, launched_by=task, user_data_dir=udd)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for acct, e in pairs:
            if e.get("skip"):
                continue
            try:
                got = verify(pw, acct["id"], acct["platform"], int(e["port"]), e.get("launched_by"), e["handle"], notes)
            except Exception as ex:
                got = None
                notes.append(f"{acct['id']}: 接続エラー {type(ex).__name__}: {str(ex)[:200]}")
            ok = bool(got) and got.lower() == e["handle"].lower()
            auto[acct["id"]] = {"platform": acct["platform"], "handle": e["handle"], "cdp_port": int(e["port"]),
                                "launch_task": e.get("launched_by"), "user_data_dir": e.get("user_data_dir"),
                                "verified": ok, "seen_handle": got, "checked_at": now().isoformat(timespec="minutes")}
            if not ok:
                notes.append(f"{acct['id']}: ログイン中のアカウントが {got}(期待は @{e['handle']})。ログインし直しが必要かもしれない")
    write_json(ACCOUNTS_AUTO, auto)
    summary = {k: f"@{v['handle']} port {v['cdp_port']} {'OK' if v['verified'] else 'NG(' + str(v.get('seen_handle')) + ')'}" for k, v in auto.items()}
    print(json.dumps({"accounts": summary, "did": did, "notes": notes}, ensure_ascii=False, indent=1))
    if a.job:
        job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        allok = all(v["verified"] for v in auto.values()) and not notes
        write_json(OPS / "mailbox" / "to_claude" / f"{job['id']}.result.json", {
            "id": job["id"], "status": "done" if allok else ("failed" if not any(v["verified"] for v in auto.values()) else "done"),
            "finished_at": now().isoformat(timespec="seconds"), "what_i_did": did or ["確認だけ行った"],
            "evidence": {"accounts": summary, "file": str(ACCOUNTS_AUTO)},
            "error_text": "" if allok else "; ".join(notes)[:1500], "not_done": [k for k, v in auto.items() if not v["verified"]],
            "notes": "",
        })


if __name__ == "__main__":
    main()
