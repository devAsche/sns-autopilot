"""本人が承認したいいね・フォロー・RTを、Museのアカウント別Chromeプロファイルで実行する。

使い方(Museが実行):
  python actions/engage_run.py --enqueue "<mailbox/to_muse/engage-*.json>"
      … 作業指示の操作を待ち行列(state/engage_queue.json)に入れ、結果を to_claude に書く。ボタンは押さない
  python actions/engage_run.py --drain
      … 待ち行列から、1日の上限と間隔を守って実行する(普段は Windows タスク Ops-EngageRun が呼ぶ)
  python actions/engage_run.py --drain --dry-run
      … ボタンの手前まで全部確かめて止める(テスト用。回数も記録も増えない)

守っていること:
  - 承認済み(approved_by_user: true)の作業指示から入った操作しか実行しない
  - ログイン中のアカウントが設定と違えば、そのアカウントは何もしない
  - 1日の上限(config/engage.yaml の caps)と、同じアカウントでの間隔(pacing)を守る。上限を超えた分は翌日に回る
  - 制限・確認画面・ログアウトを見つけたら、そのアカウントを stop_hours_on_block 時間止め、mailbox/to_claude に知らせる
  - 結果のわからない操作は、念のため回数に数え、同じ相手には二度試さない

このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.common import OPS, keep_awake, now, read_json, today, write_json  # noqa: E402
from lib.engage_common import (ACTIONS, LOG_DIR, QUEUE_STATE, account_by_id, add_count, cap_for,  # noqa: E402
                               connect_account, current_handle, detect_block, history_key, load_engage_config,
                               load_history, log_event, new_tab, pause, paused_until, record_history, release,
                               used, write_status)

TO_CLAUDE = OPS / "mailbox" / "to_claude"
SHOTS = LOG_DIR / "shots"
NOT_FOUND = re.compile(r"このアカウントは存在しません|This account doesn.t exist|Account suspended|アカウントは凍結|"
                       r"このページはご利用いただけません|Sorry, this page isn.t available|このポストは削除|"
                       r"this post was deleted|Hmm\.\.\.this page doesn.t exist|ページが存在しません", re.I)


class Outcome(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


def shot(page, name: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    p = SHOTS / f"{today()}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:80]}.png"
    try:
        page.screenshot(path=str(p))
    except Exception:
        pass
    return str(p)


def _check(page, platform: str) -> None:
    b = detect_block(page, platform)
    if b:
        raise Outcome("blocked", b)
    try:
        body = page.inner_text("body", timeout=3000)[:3000]
    except Exception:
        body = ""
    if NOT_FOUND.search(body):
        raise Outcome("not_found", "ページが無い・削除済み・凍結")


# ---------- X ----------

X_FIND_CARD_JS = r"""
(tid) => Array.from(document.querySelectorAll('article[data-testid="tweet"]')).findIndex(a => {
  const t = a.querySelector('time'); const l = t ? t.closest('a') : null;
  return !!l && new RegExp('/status/' + tid + '(?:$|[/?#])').test(l.getAttribute('href') || '');
})
"""

X_PROFILE_BTN_JS = r"""
() => {
  const col = document.querySelector('[data-testid="primaryColumn"]'); if (!col) return null;
  const b = Array.from(col.querySelectorAll('button[data-testid]')).find(b =>
    /-(follow|unfollow|cancel)$/.test(b.getAttribute('data-testid')) && !b.closest('[data-testid="UserCell"]'));
  return b ? b.getAttribute('data-testid') : null;
}
"""


def x_card(page, url: str):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise Outcome("invalid", "ポストのURLではない")
    tid = m.group(1)
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        _check(page, "x")
        raise Outcome("not_found", "ポストが表示されない")
    time.sleep(random.uniform(2, 4))
    _check(page, "x")
    idx = page.evaluate(X_FIND_CARD_JS, tid)
    if idx < 0:
        raise Outcome("not_found", "URLのポストが画面に見つからない")
    return page.locator('article[data-testid="tweet"]').nth(idx)


def x_like(page, url: str, dry: bool) -> str:
    card = x_card(page, url)
    if card.locator('[data-testid="unlike"]').count():
        return "already"
    btn = card.locator('[data-testid="like"]').first
    if not btn.count():
        raise Outcome("unsupported", "いいねボタンが見つからない")
    if dry:
        return "dry_run"
    btn.click()
    try:
        card.locator('[data-testid="unlike"]').first.wait_for(state="visible", timeout=10000)
    except Exception:
        _check(page, "x")
        raise Outcome("unknown", "押したが、いいね済みの表示にならない")
    time.sleep(1.5)
    _check(page, "x")
    return "done"


def x_retweet(page, url: str, dry: bool) -> str:
    card = x_card(page, url)
    if card.locator('[data-testid="unretweet"]').count():
        return "already"
    btn = card.locator('[data-testid="retweet"]').first
    if not btn.count():
        raise Outcome("unsupported", "リポストボタンが見つからない(鍵垢などでリポストできない可能性)")
    if dry:
        return "dry_run"
    btn.click()
    confirm = page.locator('[data-testid="retweetConfirm"]').first
    try:
        confirm.wait_for(state="visible", timeout=8000)
    except Exception:
        page.keyboard.press("Escape")
        _check(page, "x")
        raise Outcome("unsupported", "リポストのメニューが出ない")
    confirm.click()
    try:
        card.locator('[data-testid="unretweet"]').first.wait_for(state="visible", timeout=10000)
    except Exception:
        _check(page, "x")
        raise Outcome("unknown", "押したが、リポスト済みの表示にならない")
    time.sleep(1.5)
    _check(page, "x")
    return "done"


def x_follow(page, url: str, dry: bool) -> str:
    m = re.search(r"x\.com/([A-Za-z0-9_]{1,15})/?$", url)
    if not m:
        raise Outcome("invalid", "プロフィールのURLではない")
    handle = m.group(1)
    page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
    try:
        page.wait_for_selector('[data-testid="UserName"]', timeout=20000)
    except Exception:
        _check(page, "x")
        raise Outcome("not_found", "プロフィールが表示されない")
    time.sleep(random.uniform(2, 4))
    _check(page, "x")
    name_text = page.inner_text('[data-testid="UserName"]')
    if f"@{handle}".lower() not in name_text.lower():
        raise Outcome("mismatch", f"開いたプロフィールが @{handle} ではない")
    tid = page.evaluate(X_PROFILE_BTN_JS)
    if not tid:
        raise Outcome("unsupported", "フォローボタンが見つからない")
    if tid.endswith("-unfollow") or tid.endswith("-cancel"):
        return "already"
    if dry:
        return "dry_run"
    page.locator(f'[data-testid="primaryColumn"] button[data-testid="{tid}"]').first.click()
    uid = tid[: -len("-follow")]
    try:
        page.locator(f'button[data-testid="{uid}-unfollow"], button[data-testid="{uid}-cancel"]').first.wait_for(state="visible", timeout=10000)
    except Exception:
        _check(page, "x")
        raise Outcome("unknown", "押したが、フォロー中の表示にならない")
    time.sleep(1.5)
    _check(page, "x")
    return "done"


# ---------- Instagram ----------

IG_LIKE_LABELS = ["いいね！", "Like"]
IG_UNLIKE_LABELS = ["「いいね！」を取り消す", "Unlike"]
IG_FOLLOW = re.compile(r"^(フォローする|Follow|フォローバックする|Follow Back)$")
IG_FOLLOWING = re.compile(r"^(フォロー中|Following|リクエスト済み|Requested)$")

IG_MARK_LIKE_JS = r"""
([likes, unlikes]) => {
  const scope = document.querySelector('main') || document.body;
  const big = Array.from(scope.querySelectorAll('svg[aria-label]')).filter(s => s.getBoundingClientRect().height >= 20);
  if (big.some(s => unlikes.includes(s.getAttribute('aria-label')))) return 'already';
  const s = big.find(s => likes.includes(s.getAttribute('aria-label')));
  if (!s) return 'missing';
  const b = s.closest('button, [role="button"]') || s.parentElement;
  document.querySelectorAll('[data-engage-target]').forEach(e => e.removeAttribute('data-engage-target'));
  b.setAttribute('data-engage-target', '1');
  return 'ok';
}
"""

IG_LIKED_JS = r"""
(unlikes) => Array.from((document.querySelector('main') || document.body).querySelectorAll('svg[aria-label]'))
  .some(s => s.getBoundingClientRect().height >= 20 && unlikes.includes(s.getAttribute('aria-label')))
"""


def ig_like(page, url: str, dry: bool) -> str:
    if not re.search(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(p|reel)/[A-Za-z0-9_-]+", url):
        raise Outcome("invalid", "投稿のURLではない")
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(random.uniform(4, 6))
    _check(page, "instagram")
    st = page.evaluate(IG_MARK_LIKE_JS, [IG_LIKE_LABELS, IG_UNLIKE_LABELS])
    if st == "already":
        return "already"
    if st != "ok":
        raise Outcome("unsupported", "いいねボタンが見つからない")
    if dry:
        return "dry_run"
    page.locator('[data-engage-target="1"]').first.click()
    try:
        page.wait_for_function(IG_LIKED_JS, arg=IG_UNLIKE_LABELS, timeout=10000)
    except Exception:
        _check(page, "instagram")
        raise Outcome("unknown", "押したが、いいね済みの表示にならない")
    time.sleep(1.5)
    _check(page, "instagram")
    return "done"


def ig_follow(page, url: str, dry: bool) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})/?$", url)
    if not m:
        raise Outcome("invalid", "プロフィールのURLではない")
    handle = m.group(1)
    page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded")
    time.sleep(random.uniform(4, 6))
    _check(page, "instagram")
    header = page.locator("header").first
    if not header.count() or handle.lower() not in header.inner_text().lower():
        raise Outcome("mismatch", f"開いたプロフィールが {handle} ではない(または表示されない)")
    buttons = header.locator('button, div[role="button"]')
    texts = [t.strip() for t in buttons.all_inner_texts()]
    if any(IG_FOLLOWING.match(t) for t in texts):
        return "already"
    idx = next((i for i, t in enumerate(texts) if IG_FOLLOW.match(t)), None)
    if idx is None:
        raise Outcome("unsupported", "フォローボタンが見つからない")
    if dry:
        return "dry_run"
    buttons.nth(idx).click()
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(1)
        if any(IG_FOLLOWING.match(t.strip()) for t in header.locator('button, div[role="button"]').all_inner_texts()):
            time.sleep(1)
            _check(page, "instagram")
            return "done"
    _check(page, "instagram")
    raise Outcome("unknown", "押したが、フォロー中の表示にならない")


HANDLERS = {("x", "like"): x_like, ("x", "retweet"): x_retweet, ("x", "follow"): x_follow,
            ("instagram", "like"): ig_like, ("instagram", "follow"): ig_follow}


# ---------- 待ち行列 ----------

def enqueue(job_path: str) -> None:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    res = {"id": job.get("id", Path(job_path).stem), "finished_at": None, "what_i_did": [], "evidence": {},
           "error_text": "", "not_done": [], "notes": ""}
    if job.get("approved_by_user") is not True:
        res.update(status="blocked", error_text="approved_by_user が true ではないので待ち行列に入れなかった")
    else:
        cfg = load_engage_config()
        queue = read_json(QUEUE_STATE, [])
        known = {q["id"] for q in queue}
        added, bad = 0, []
        for a in job.get("params", {}).get("actions", []):
            acct = account_by_id(cfg, a.get("account", ""))
            if not acct or a.get("action") not in ACTIONS.get(acct["platform"], ()) or not a.get("url") or not a.get("id"):
                bad.append(a.get("id") or str(a)[:80])
                continue
            if a["id"] in known:
                continue
            queue.append({"id": a["id"], "account": a["account"], "action": a["action"], "url": a["url"],
                          "job": res["id"], "approved_at": job.get("created_at"), "added": now().isoformat(timespec="minutes"),
                          "attempts": 0})
            known.add(a["id"])
            added += 1
        write_json(QUEUE_STATE, queue)
        write_status(cfg)
        res.update(status="done", what_i_did=[f"待ち行列に {added} 件入れた(合計 {len(queue)} 件)"],
                   evidence={"queue_file": str(QUEUE_STATE)}, not_done=bad,
                   notes="実行は Ops-EngageRun(engage_run.py --drain)が上限と間隔を守って行う")
    res["finished_at"] = now().isoformat(timespec="seconds")
    write_json(TO_CLAUDE / f"{res['id']}.result.json", res)
    print(json.dumps(res, ensure_ascii=False, indent=1))


def update_queue(remove: set[str] = frozenset(), bump: set[str] = frozenset()) -> None:
    """待ち行列をファイルから読み直してから直す(実行中に --enqueue で足された分を消さないため)。"""
    q = read_json(QUEUE_STATE, [])
    out = []
    for item in q:
        if item["id"] in remove:
            continue
        if item["id"] in bump:
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= 3:
                continue
        out.append(item)
    write_json(QUEUE_STATE, out)


def runs_left_today(cfg: dict) -> int:
    times = cfg.get("pacing", {}).get("run_times") or ["12:00", "15:00", "18:00", "21:00"]
    cur = now() - dt.timedelta(minutes=30)
    return max(1, sum(1 for t in times if t >= cur.strftime("%H:%M")))


def drain(dry: bool, max_minutes: int) -> dict:
    cfg = load_engage_config()
    pacing = cfg.get("pacing", {})
    gap = (pacing.get("min_gap_s", 45), pacing.get("max_gap_s", 120))
    stop_h = pacing.get("stop_hours_on_block", 48)
    queue = read_json(QUEUE_STATE, [])
    expire = (now() - dt.timedelta(days=7)).isoformat()
    expired = [q for q in queue if q.get("added", "") < expire]
    queue = [q for q in queue if q.get("added", "") >= expire]
    if expired and not dry:
        update_queue(remove={q["id"] for q in expired})
    history = load_history()
    summary = {"started_at": now().isoformat(timespec="minutes"), "dry_run": dry, "results": {}, "accounts": {}, "expired": len(expired)}
    left = runs_left_today(cfg)

    # アカウントごとに、今回の実行でやってよい件数を決める
    plan: dict[str, list[dict]] = {}
    for q in queue:
        plan.setdefault(q["account"], []).append(q)
    allowance: dict[str, dict[str, int]] = {}
    for acct_id, items in list(plan.items()):
        acct = account_by_id(cfg, acct_id)
        info = summary["accounts"].setdefault(acct_id, {"status": "ok", "notes": []})
        if not acct or not acct.get("enabled"):
            info["status"] = "disabled"
            plan.pop(acct_id)
            continue
        if paused_until(acct_id):
            info["status"] = f"paused_until {paused_until(acct_id)}"
            plan.pop(acct_id)
            continue
        allowance[acct_id] = {}
        for action in ACTIONS[acct["platform"]]:
            remain = max(0, cap_for(cfg, acct, action) - used(acct_id, action))
            allowance[acct_id][action] = remain if dry else math.ceil(remain / left)
        random.shuffle(items)

    deadline = time.time() + max_minutes * 60
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        sessions: dict[str, dict] = {}
        for acct_id in list(plan):
            acct = account_by_id(cfg, acct_id)
            if not any(allowance[acct_id].get(q["action"], 0) > 0 for q in plan[acct_id]):
                summary["accounts"][acct_id]["notes"].append("今日の上限に達しているので明日に回す")
                plan.pop(acct_id)
                continue
            steps: list[str] = []
            try:
                browser = connect_account(pw, acct, steps)
                page = new_tab(browser)
                h = current_handle(page, acct["platform"])
                if not h or h.lower() != acct["handle"].lower():
                    summary["accounts"][acct_id].update(status="account_mismatch")
                    summary["accounts"][acct_id]["notes"].append(f"ログイン中が {h}(設定は {acct['handle']})なので何もしない")
                    shot(page, f"{acct_id}_mismatch")
                    page.close()
                    release(browser)
                    plan.pop(acct_id)
                    continue
                sessions[acct_id] = {"browser": browser, "page": page, "acct": acct, "next": time.time()}
            except Exception as e:
                summary["accounts"][acct_id].update(status="connect_error")
                summary["accounts"][acct_id]["notes"] += steps + [f"{type(e).__name__}: {str(e)[:300]}"]
                plan.pop(acct_id)

        try:
            while sessions and time.time() < deadline:
                # 次に操作してよい時刻が一番早いアカウントを選ぶ(アカウントを交互に回して間隔を空ける)
                acct_id = min(sessions, key=lambda k: sessions[k]["next"])
                s = sessions[acct_id]
                item = next((q for q in plan[acct_id] if allowance[acct_id].get(q["action"], 0) > 0), None)
                if not item:
                    sessions.pop(acct_id)
                    continue
                wait = s["next"] - time.time()
                if wait > 0:
                    time.sleep(wait)
                plan[acct_id].remove(item)
                acct, page = s["acct"], s["page"]
                key = history_key(acct_id, item["action"], item["url"])
                if key in history and not dry:
                    kind, detail = "already", "実行済みの記録あり"
                else:
                    try:
                        kind = HANDLERS[(acct["platform"], item["action"])](page, item["url"], dry)
                        detail = ""
                    except Outcome as o:
                        kind, detail = o.kind, o.detail
                    except Exception as e:
                        kind, detail = "error", f"{type(e).__name__}: {str(e)[:200]}"
                evidence = shot(page, f"{acct_id}_{item['action']}_{item['id']}") if kind not in ("done", "already", "dry_run") else ""
                summary["results"][kind] = summary["results"].get(kind, 0) + 1
                log_event({"account": acct_id, "action": item["action"], "url": item["url"], "id": item["id"],
                           "result": kind, "detail": detail, "dry_run": dry, "shot": evidence})
                print(f"{acct_id} {item['action']} {item['url']} → {kind} {detail}", flush=True)
                if not dry:
                    if kind in ("done", "unknown"):
                        add_count(acct_id, item["action"])
                        allowance[acct_id][item["action"]] -= 1
                    if kind in ("done", "already", "unknown", "not_found", "invalid", "mismatch", "unsupported"):
                        record_history(key, kind)
                        history[key] = {"date": today(), "result": kind}
                        update_queue(remove={item["id"]})
                    elif kind == "error":
                        update_queue(bump={item["id"]})
                if kind == "blocked":
                    until = pause(acct_id, detail, stop_h) if not dry else "(dry-run のため止めない)"
                    summary["accounts"][acct_id].update(status=f"blocked:{detail}")
                    summary["accounts"][acct_id]["notes"].append(f"{detail} を検知。{until} まで止める")
                    if not dry:
                        write_json(TO_CLAUDE / f"engage-alert-{today()}-{acct_id}.json", {
                            "from": "engage_run", "account": acct_id, "reason": detail, "paused_until": until,
                            "screenshot": evidence, "at": now().isoformat(timespec="minutes")})
                    sessions.pop(acct_id)
                    continue
                s["next"] = time.time() + random.uniform(*gap)
            if sessions and time.time() >= deadline:
                summary["notes"] = [f"{max_minutes} 分たったので途中で終えた(残りは次回)"]
        finally:
            for s in list(sessions.values()):
                try:
                    s["page"].close()
                except Exception:
                    pass
                release(s["browser"])
    summary["finished_at"] = now().isoformat(timespec="minutes")
    write_status(cfg, {"last_run": summary})
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enqueue")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-minutes", type=int, default=150)
    a = ap.parse_args()
    if a.enqueue:
        enqueue(a.enqueue)
    if a.drain:
        keep_awake(True)
        try:
            s = drain(a.dry_run, a.max_minutes)
        except Exception as e:
            s = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
            write_status(load_engage_config(), {"last_run": s})
        finally:
            keep_awake(False)
        print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
