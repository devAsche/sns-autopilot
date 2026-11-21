"""ops 共通ライブラリ: パス、設定、ログ、状態保存、ブラウザ、スナップショット。

このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

OPS = Path(__file__).resolve().parents[1]
JST = dt.timezone(dt.timedelta(hours=9))


def now() -> dt.datetime:
    return dt.datetime.now(JST)


def today() -> str:
    return now().strftime("%Y-%m-%d")


DATA_DIR = OPS / "data" / today()
STATE_DIR = OPS / "state"
LOG_DIR = OPS / "logs"
for _d in (DATA_DIR, STATE_DIR, LOG_DIR, DATA_DIR / "snapshots"):
    _d.mkdir(parents=True, exist_ok=True)

_LOG_FILE = LOG_DIR / f"{today()}.log"


def log(msg: str) -> None:
    line = f"[{now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 設定・環境変数 ----------

def load_config(name: str) -> dict:
    import yaml  # pyyaml

    with (OPS / "config" / f"{name}.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_env() -> dict:
    env = {}
    p = OPS / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("OPS_")})
    return env


# ---------- 状態(既読ID・回数上限) ----------

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))  # PowerShell が付ける BOM があっても読む
    except Exception:
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


class Seen:
    """過去に見たIDを覚えておき、新規かどうかを判定する。"""

    def __init__(self, name: str, keep_days: int = 60):
        self.path = STATE_DIR / f"seen_{name}.json"
        self.data: dict = read_json(self.path, {})
        cutoff = (now() - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
        self.data = {k: v for k, v in self.data.items() if v >= cutoff}

    def is_new(self, key: str) -> bool:
        return key not in self.data

    def mark(self, key: str) -> None:
        self.data.setdefault(key, today())

    def save(self) -> None:
        write_json(self.path, self.data)


class Quota:
    """サイトごとの1日あたりの読み込み回数上限。超えたら False を返す。"""

    path = STATE_DIR / "quota.json"

    def __init__(self):
        self.data = read_json(self.path, {})
        if self.data.get("date") != today():
            self.data = {"date": today(), "used": {}}

    def take(self, site: str, limit: int) -> bool:
        used = self.data["used"].get(site, 0)
        if used >= limit:
            return False
        self.data["used"][site] = used + 1
        write_json(self.path, self.data)
        return True


# ---------- スリープ防止 ----------

def keep_awake(on: bool = True) -> None:
    """Windows: 実行中にPCがスリープへ戻らないようにする。"""
    if sys.platform != "win32":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


# ---------- ブラウザ ----------

BLOCK_PATTERNS = [
    r"captcha", r"verify you are human", r"are you a robot", r"unusual traffic",
    r"ロボットではありません", r"セキュリティチェック", r"security check",
]
LOGIN_PATTERNS = [r"^\s*(log in|sign in|ログイン)\s*$", r"ログインして", r"Log in to", r"Sign in to continue"]


def open_browser(headless: bool = False):
    """検索専用プロファイル(browser/research)でChromeを開く。ログインはこのプロファイルに本人が一度だけ行う。"""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(OPS / "browser" / "research"),
        channel="chrome",
        headless=headless,
        locale="ja-JP",
        viewport={"width": 1366, "height": 900},
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )
    ctx.set_default_timeout(30000)
    return pw, ctx


def human_pause(a: float = 2.0, b: float = 5.0) -> None:
    time.sleep(random.uniform(a, b))


def scroll(page, times: int = 3) -> None:
    for _ in range(times):
        page.mouse.wheel(0, random.randint(1800, 2600))
        human_pause(1.5, 3.5)


def page_state(page) -> str:
    """ok / blocked / login のどれか。CAPTCHA等には触れず止まる。"""
    try:
        text = page.inner_text("body", timeout=5000)[:4000]
    except Exception:
        return "ok"
    low = text.lower()
    if any(re.search(p, low) for p in BLOCK_PATTERNS):
        return "blocked"
    head = text[:600]
    if any(re.search(p, head, re.I | re.M) for p in LOGIN_PATTERNS) and len(text) < 2500:
        return "login"
    return "ok"


def snapshot(page, name: str, keep_html: bool = False) -> dict:
    """解析を後から直せるよう、画面と本文テキストを保存する。"""
    base = DATA_DIR / "snapshots" / re.sub(r"[^\w\-]+", "_", name)[:80]
    out = {}
    try:
        page.screenshot(path=str(base) + ".png", full_page=False)
        out["png"] = str(base.relative_to(OPS)) + ".png"
    except Exception:
        pass
    try:
        txt = page.inner_text("body", timeout=5000)
        Path(str(base) + ".txt").write_text(txt[:60000], encoding="utf-8")
        out["txt"] = str(base.relative_to(OPS)) + ".txt"
    except Exception:
        pass
    if keep_html:
        try:
            Path(str(base) + ".html").write_text(page.content()[:3_000_000], encoding="utf-8")
        except Exception:
            pass
    return out


# ---------- 数値の読み取り ----------

_MULT = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9, "千": 1e3, "万": 1e4, "億": 1e8}


def parse_count(s: str | None) -> float | None:
    """'1.2M' '3.4万' '12,345' '1.2K views' → 数値。読めなければ None。"""
    if not s:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([kKmMbB千万億])?", s)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * _MULT.get(m.group(2) or "", 1)


def parse_yen(text: str) -> list[int]:
    vals = []
    for m in re.finditer(r"(?:¥|￥)?\s*([\d,]{3,9})\s*円|(?:¥|￥)\s*([\d,]{3,9})", text):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        if raw.isdigit():
            vals.append(int(raw))
    return vals


def relative_age_days(s: str) -> float | None:
    """'3日前' '2 hours ago' '1週間前' など → 経過日数。"""
    if not s:
        return None
    table = [
        (r"(\d+)\s*(秒|second)", 1 / 86400), (r"(\d+)\s*(分|minute)", 1 / 1440),
        (r"(\d+)\s*(時間|hour)", 1 / 24), (r"(\d+)\s*(日|day)", 1),
        (r"(\d+)\s*(週間|week)", 7), (r"(\d+)\s*(か月|ヶ月|month)", 30), (r"(\d+)\s*(年|year)", 365),
    ]
    for pat, mul in table:
        m = re.search(pat, s, re.I)
        if m:
            return int(m.group(1)) * mul
    return None


def trim(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def result(status: str = "ok", items=None, notes=None, **extra) -> dict:
    d = {"status": status, "items": items or [], "notes": notes or []}
    d.update(extra)
    return d


def safe_run(name: str, fn, *args, **kwargs) -> dict:
    t0 = time.time()
    log(f"start {name}")
    try:
        r = fn(*args, **kwargs)
    except Exception as e:  # 失敗は隠さず、エラー文をそのまま残す
        r = result("error", notes=[f"{type(e).__name__}: {e}", traceback.format_exc()[-1500:]])
    r["collector"] = name
    r["seconds"] = round(time.time() - t0, 1)
    write_json(DATA_DIR / f"{name}.json", r)
    log(f"end {name}: {r['status']} items={len(r.get('items', []))} ({r['seconds']}s)")
    return r


def result_dir_for(job_path, category: str = "system"):
    """作業指示ファイルの場所から、結果を書くフォルダを決める(mailbox/<用途>/to_muse/x.json → mailbox/<用途>/to_claude/)。
    to_muse の外にある場合は mailbox/<category>/to_claude/。"""
    from pathlib import Path as _P
    p = _P(job_path).resolve() if job_path else None
    d = p.parent.parent / "to_claude" if p and p.parent.name in ("to_muse", "done") else OPS / "mailbox" / category / "to_claude"
    d.mkdir(parents=True, exist_ok=True)
    return d
