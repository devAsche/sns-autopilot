"""反応(いいね・フォロー・RT)の仕組みの共通部品。

- Muse が用意したアカウントごとの Chrome プロファイルに CDP でつなぐ(つながらなければ起動タスクで起こして1回だけやり直す)
- ログイン中のアカウントが設定どおりか確かめる(違えばそのアカウントは何もしない)
- 操作制限・ログイン切れ・確認画面を見つけたら、そのアカウントを止める
- 1日の回数・実行済みの記録・一時停止の記録

このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from lib.common import OPS, STATE_DIR, load_config, now, read_json, today, write_json

PROFILES_STATE = STATE_DIR / "engage_profiles.json"   # tools/engage_probe.py が書く
PROFILE_MAP = STATE_DIR / "engage_profile_map.json"    # Muse が書く(どのアカウントがどのChromeプロファイルか)
ACCOUNTS_AUTO = STATE_DIR / "engage_accounts_auto.json"  # tools/engage_setup_profiles.py が書く(ポート・起動タスク・ハンドル)
COUNTS_STATE = STATE_DIR / "engage_counts.json"       # 1日の実行回数
PAUSED_STATE = STATE_DIR / "engage_paused.json"       # 制限などで止めたアカウント
HISTORY_STATE = STATE_DIR / "engage_history.json"     # 実行済み(同じ相手に二度しない)
QUEUE_STATE = STATE_DIR / "engage_queue.json"         # 承認済みで未実行の操作
STATUS_STATE = STATE_DIR / "engage_status.json"       # 朝のClaudeが読む要約
LOG_DIR = OPS / "logs" / "engage"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ACTIONS = {"x": ("like", "retweet", "follow"), "instagram": ("like", "follow")}
HANDLE_X = re.compile(r"^[A-Za-z0-9_]{1,15}$")
HANDLE_IG = re.compile(r"^[A-Za-z0-9._]{1,30}$")


# ---------- 設定 ----------

def load_engage_config() -> dict:
    cfg = load_config("engage")
    cfg.setdefault("accounts", [])
    cfg.setdefault("caps", {})
    cfg.setdefault("pacing", {})
    # tools/engage_setup_profiles.py が確かめた接続先とハンドルを足す(config に書いてある値が優先)
    auto = read_json(ACCOUNTS_AUTO, {})
    for a in cfg["accounts"]:
        got = auto.get(a.get("id"), {})
        if not got.get("verified"):
            continue
        if not a.get("handle"):
            a["handle"] = got.get("handle")
        if (a.get("handle") or "").lower() != (got.get("handle") or "").lower():
            continue
        for k in ("cdp_port", "launch_task", "user_data_dir"):
            if not a.get(k) and got.get(k):
                a[k] = got[k]
        if got.get("user_data_dir"):
            a.setdefault("profile_mark", got["user_data_dir"])  # 接続が詰まったとき、このChromeだけを再起動するため
    return cfg


def enabled_accounts(cfg: dict) -> list[dict]:
    return [a for a in cfg["accounts"] if a.get("enabled") and a.get("handle")]


def account_by_id(cfg: dict, acct_id: str) -> dict | None:
    return next((a for a in cfg["accounts"] if a.get("id") == acct_id), None)


def cap_for(cfg: dict, acct: dict, action: str) -> int:
    caps = dict((cfg.get("caps") or {}).get(acct["platform"], {}))
    caps.update(acct.get("caps") or {})
    return int(caps.get(action, 0))


def resolve_profile(acct: dict) -> dict:
    """アカウントのCDPポートと起動タスクを決める。設定に書いてあればそれを、無ければ調査結果(engage_profiles.json)を使う。"""
    out = {"cdp_port": acct.get("cdp_port"), "launch_task": acct.get("launch_task"),
           "profile_mark": acct.get("profile_mark")}
    if out["cdp_port"]:
        return out
    key = "x_handle" if acct["platform"] == "x" else "ig_handle"
    for p in read_json(PROFILES_STATE, {}).get("profiles", []):
        if (p.get(key) or "").lower() == acct["handle"].lower():
            out["cdp_port"] = p.get("port")
            out["launch_task"] = out["launch_task"] or p.get("launch_task")
            out["profile_mark"] = out["profile_mark"] or p.get("user_data_dir")
            return out
    return out


# ---------- ブラウザ接続 ----------

def cdp_alive(port: int, timeout: float = 4) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _run_task(task: str) -> None:
    r = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /run /tn {task} が失敗: {(r.stdout + r.stderr).strip()}")


def _kill_profile_chrome(mark: str) -> str:
    safe = mark.replace("'", "''")
    ps = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{safe}*' }}; "
        "$n = @($p).Count; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "Write-Output $n"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    return (r.stdout or r.stderr).strip()


class _PersistentWrap:
    """launch_persistent_context の結果を、CDP接続と同じ形(.contexts)で扱うための包み。"""

    def __init__(self, ctx):
        self.contexts = [ctx]
        self.persistent = True

    def close(self):
        self.contexts[0].close()


def release(browser) -> None:
    """直接開いたプロファイルは閉じる。CDPでつないだChromeは閉じない(Museのものなので切断だけ)。"""
    if getattr(browser, "persistent", False):
        try:
            browser.close()
        except Exception:
            pass


def connect_account(pw, acct: dict, steps: list):
    """CDPでつなぐ。応答が無ければ起動タスクで起こし、詰まっていればそのプロファイルのChromeだけ再起動して1回だけやり直す。"""
    prof = resolve_profile(acct)
    port = prof["cdp_port"]
    if not port and acct.get("user_data_dir"):
        # ポートを使わず、プロファイルのフォルダを直接開く方式(そのプロファイルのChromeが閉じている必要がある)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=acct["user_data_dir"], channel="chrome", headless=False, locale="ja-JP",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"])
        steps.append(f"{acct['id']}: プロファイル {acct['user_data_dir']} を直接開いた")
        return _PersistentWrap(ctx)
    if not port:
        raise RuntimeError(f"{acct['id']}: CDPポートが分からない(tools/engage_probe.py の結果に無く、設定にも無い)")
    url = f"http://127.0.0.1:{int(port)}"
    if cdp_alive(port):
        try:
            return pw.chromium.connect_over_cdp(url, timeout=30_000)
        except Exception as e:
            steps.append(f"{acct['id']}: 1回目の接続に失敗: {type(e).__name__}: {str(e)[:200]}")
            if not prof.get("profile_mark"):
                raise RuntimeError(f"{acct['id']}: 接続が詰まったが、どのChromeか特定できないので再起動しない")
            n = _kill_profile_chrome(prof["profile_mark"])
            steps.append(f"{acct['id']}: そのプロファイルのChromeを終了(プロセス {n} 個)")
            time.sleep(5)
    else:
        steps.append(f"{acct['id']}: CDP {port} が応答しない")
    task = prof.get("launch_task")
    if not task:
        raise RuntimeError(f"{acct['id']}: CDP {port} が応答せず、起動タスクも分からない")
    _run_task(task)
    steps.append(f"{acct['id']}: タスク {task} で起動")
    for _ in range(45):
        if cdp_alive(port, 3):
            time.sleep(8)
            return pw.chromium.connect_over_cdp(url, timeout=30_000)
        time.sleep(2)
    raise RuntimeError(f"{acct['id']}: 起動したが 90 秒たっても CDP {port} が応答しない")


def new_tab(browser):
    """Museが開いているタブには触らず、作業用のタブを1枚だけ足す(終わったら閉じる)。"""
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.new_page()
    page.set_default_timeout(25_000)
    return page


# ---------- ログイン中のアカウント確認 ----------

def current_handle(page, platform: str) -> str | None:
    if platform == "x":
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[data-testid="AppTabBar_Profile_Link"]', timeout=20_000)
        except Exception:
            return None
        href = page.get_attribute('a[data-testid="AppTabBar_Profile_Link"]', "href") or ""
        h = href.strip("/").split("/")[-1]
        return h if HANDLE_X.match(h) else None
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    time.sleep(6)
    # サイドバーが折りたたまれている(ウィンドウが狭い)と「プロフィール」の文字が非表示になるので、
    # 表示されていない文字(textContent)・左端の位置・自分の写真の alt の3通りで探す
    h = None
    for _ in range(3):
        h = page.evaluate(IG_SELF_JS)
        if h:
            break
        time.sleep(3)
    return h if h and HANDLE_IG.match(h) else None


IG_SELF_JS = r"""() => {
  const isUser = h => /^\/[A-Za-z0-9._]+\/$/.test(h || '');
  const links = Array.from(document.querySelectorAll('a[href]')).filter(a => isUser(a.getAttribute('href')));
  const name = a => a.getAttribute('href').replaceAll('/', '');
  // 1. 「プロフィール」ラベル付きのリンク(非表示でも textContent には残る)
  let a = links.find(a => /^(プロフィール|Profile)$/.test((a.textContent || '').trim()));
  if (a) return name(a);
  // 2. 左端(サイドバー)にある、自分の写真つきのリンク
  a = links.find(a => { const r = a.getBoundingClientRect(); return r.width > 0 && r.left < 150 && a.querySelector('img'); });
  if (a) return name(a);
  // 3. 写真の alt が「<リンク先>のプロフィール写真」で、ナビゲーション内か、投稿(article)の外にあるもの
  a = links.find(a => { const img = a.querySelector('img[alt]'); return img && !a.closest('article') &&
        img.alt.replace(/のプロフィール写真|'s profile picture/, '').trim() === name(a); });
  return a ? name(a) : null;
}"""


# ---------- 制限・確認画面の検知 ----------

_BLOCK = {
    "x": [
        ("rate_limited", r"Rate limit exceeded|You are rate limited|over the (daily )?limit|reached your daily limit|"
                         r"unable to follow more people|しばらくしてから|上限に達し|制限を超え|これ以上フォローできません|"
                         r"Try again later|後でもう一度"),
        ("locked", r"Your account (is|has been) (locked|suspended)|アカウントはロック|アカウントが凍結|凍結されています"),
        ("challenge", r"Verify your identity|本人確認|Are you a robot|ロボットではありません|unusual activity|不審なアクティビティ"),
    ],
    "instagram": [
        # 「この投稿へのコメントは制限されています」等で誤検知しないよう、操作制限そのものの文言だけにする
        ("rate_limited", r"Try Again Later|後でもう一度実行してください|We restrict certain activity|特定のアクティビティを制限|"
                         r"Action Blocked|アクションがブロック|しばらくしてからもう一度"),
        ("locked", r"Your account has been (disabled|suspended)|アカウントは停止|アカウントが利用停止"),
        ("challenge", r"Confirm it's you|ご本人確認|本人確認|suspicious (login|activity)|不審なログイン|Help us confirm"),
    ],
}
_BLOCK_URL = re.compile(r"/account/access|/i/flow/login|/login|/accounts/login|/challenge|/accounts/suspended|/checkpoint", re.I)


def detect_block(page, platform: str) -> str | None:
    """制限・凍結・確認画面・ログアウトを見つけたら理由を返す。見つからなければ None。"""
    try:
        url = page.url
    except Exception:
        url = ""
    if _BLOCK_URL.search(url or ""):
        return "login_or_challenge_page"
    # 投稿本文に同じ言葉が出ても誤検知しないよう、通知・ダイアログだけを見る。
    # 本文全体は、短い(=制限や確認の専用画面らしい)ときだけ見る。
    try:
        if platform == "x":
            text = " ".join(page.locator('[data-testid="toast"], [role="alert"], [data-testid="sheetDialog"], [role="dialog"]').all_inner_texts())
        else:
            text = " ".join(page.locator('[role="dialog"], [role="alert"]').all_inner_texts())
        body = page.inner_text("body", timeout=3000)
        if len(body) < 2500:
            text += " " + body
    except Exception:
        return None
    for reason, pat in _BLOCK[platform]:
        if re.search(pat, text, re.I):
            return reason
    return None


# ---------- 記録 ----------

def counts_today() -> dict:
    d = read_json(COUNTS_STATE, {})
    return d.get(today(), {})


def add_count(acct_id: str, action: str) -> None:
    d = read_json(COUNTS_STATE, {})
    keep = (now() - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    d = {k: v for k, v in d.items() if k >= keep}
    day = d.setdefault(today(), {})
    a = day.setdefault(acct_id, {})
    a[action] = a.get(action, 0) + 1
    write_json(COUNTS_STATE, d)


def used(acct_id: str, action: str) -> int:
    return counts_today().get(acct_id, {}).get(action, 0)


def paused_until(acct_id: str) -> str | None:
    p = read_json(PAUSED_STATE, {}).get(acct_id)
    if p and p.get("until", "") > now().isoformat():
        return p["until"]
    return None


def pause(acct_id: str, reason: str, hours: int) -> str:
    d = read_json(PAUSED_STATE, {})
    until = (now() + dt.timedelta(hours=hours)).isoformat(timespec="minutes")
    d[acct_id] = {"until": until, "reason": reason, "at": now().isoformat(timespec="minutes")}
    write_json(PAUSED_STATE, d)
    return until


def history_key(acct_id: str, action: str, target: str) -> str:
    return f"{acct_id}|{action}|{target.lower()}"


def load_history() -> dict:
    d = read_json(HISTORY_STATE, {})
    keep = (now() - dt.timedelta(days=180)).strftime("%Y-%m-%d")
    return {k: v for k, v in d.items() if v.get("date", "") >= keep}


def record_history(key: str, result: str) -> None:
    d = load_history()
    d[key] = {"date": today(), "result": result}
    write_json(HISTORY_STATE, d)


def log_event(obj: dict) -> None:
    obj = {"at": now().isoformat(timespec="seconds"), **obj}
    with (LOG_DIR / f"{today()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_status(cfg: dict, extra: dict | None = None) -> dict:
    """朝のClaudeと承認反映タスクが読む要約。"""
    queue = read_json(QUEUE_STATE, [])
    paused = read_json(PAUSED_STATE, {})
    st = {
        "updated_at": now().isoformat(timespec="minutes"),
        "today": today(),
        "counts_today": counts_today(),
        "queue_pending": {},
        "paused": {k: v for k, v in paused.items() if v.get("until", "") > now().isoformat()},
    }
    for q in queue:
        st["queue_pending"].setdefault(q["account"], {}).setdefault(q["action"], 0)
        st["queue_pending"][q["account"]][q["action"]] += 1
    if extra:
        st.update(extra)
    write_json(STATUS_STATE, st)
    return st
