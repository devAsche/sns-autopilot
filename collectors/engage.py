"""反応(いいね・フォロー・RT)の候補を集める。Museのアカウント別Chromeプロファイルで読むだけ(ボタンは押さない)。

集める場所(config/engage.yaml の sources):
  notifications  自分に反応してくれた人(お返しの候補)
  keywords       検索語・ハッシュタグの最近の投稿
  seeds          指定した仲間・競合アカウントの最近の投稿と、そのフォロワー
  home           ホームのタイムライン(おすすめ)

結果: data/<日付>/engage.json(朝のClaudeがここから選んで承認ページに出す)
このファイルは Claude が保守する。Muse は編集しない。
"""
from __future__ import annotations

import datetime as dt
import re
import time
from urllib.parse import quote

from lib.common import human_pause, log, now, parse_count, result, snapshot, trim
from lib.engage_common import (HANDLE_IG, HANDLE_X, connect_account, current_handle, detect_block,
                               enabled_accounts, load_engage_config, load_history, new_tab,
                               paused_until, release)

SOURCE_WEIGHT = {"notif": 3.0, "seed": 2.0, "keyword": 1.5, "home": 1.0}

# ---------- X ----------

X_TWEETS_JS = r"""
() => Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map(a => {
  const t = a.querySelector('time');
  const link = t ? t.closest('a') : null;
  const textEl = a.querySelector('div[data-testid="tweetText"]');
  const nameEl = a.querySelector('div[data-testid="User-Name"]');
  const lab = id => { const b = a.querySelector(`[data-testid="${id}"]`); return b ? (b.getAttribute('aria-label') || '') : null; };
  const ctx = a.querySelector('[data-testid="socialContext"]');
  return {
    url: link ? link.href : null,
    created_at: t ? t.getAttribute('datetime') : null,
    text: textEl ? textEl.innerText : '',
    user: nameEl ? nameEl.innerText : '',
    reply_label: lab('reply'),
    rt_label: lab('retweet') ?? lab('unretweet'),
    like_label: lab('like') ?? lab('unlike'),
    liked: !!a.querySelector('[data-testid="unlike"]'),
    retweeted: !!a.querySelector('[data-testid="unretweet"]'),
    social: ctx ? ctx.innerText : '',
    promoted: /プロモーション|Promoted|広告|^Ad$/m.test(a.innerText.slice(-200)),
  };
})
"""

X_USERCELL_JS = r"""
() => Array.from(document.querySelectorAll('[data-testid="UserCell"]')).map(c => {
  const spans = Array.from(c.querySelectorAll('span')).map(s => s.innerText.trim());
  const handle = (spans.find(s => /^@[A-Za-z0-9_]{1,15}$/.test(s)) || '').slice(1);
  return {
    handle,
    text: c.innerText.slice(0, 400),
    following: !!c.querySelector('button[data-testid$="-unfollow"]'),
    follows_you: !!c.querySelector('[data-testid="userFollowIndicator"]'),
  };
})
"""

X_NOTIF_JS = r"""
() => Array.from(document.querySelectorAll('[data-testid="cellInnerDiv"]')).map(c => {
  const tw = c.querySelector('article[data-testid="tweet"]');
  if (tw) {
    const t = tw.querySelector('time'); const link = t ? t.closest('a') : null;
    const textEl = tw.querySelector('div[data-testid="tweetText"]');
    return {kind: 'tweet', url: link ? link.href : null, created_at: t ? t.getAttribute('datetime') : null,
            text: textEl ? textEl.innerText : '', liked: !!tw.querySelector('[data-testid="unlike"]')};
  }
  const handles = new Set();
  c.querySelectorAll('[data-testid^="UserAvatar-Container-"]').forEach(e => handles.add(e.getAttribute('data-testid').replace('UserAvatar-Container-', '')));
  return {kind: 'notice', text: (c.innerText || '').slice(0, 300), handles: Array.from(handles)};
}).filter(x => x.url || (x.handles && x.handles.length))
"""


X_REACTION_RE = re.compile(r"フォローされました|いいねしました|リポストしました|あなたのポスト|あなたの返信|返信しました|"
                           r"followed you|liked your|reposted your|liked \d+ of your|reposted \d+ of your", re.I)
X_FOLLOWED_RE = re.compile(r"フォローされました|followed you", re.I)


def _num(label: str | None) -> float | None:
    return parse_count(label) if label else None


def _age_h(created: str | None) -> float | None:
    if not created:
        return None
    try:
        return round((now() - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
    except Exception:
        return None


def _x_post(t: dict, source: str, detail: str) -> dict | None:
    m = re.search(r"x\.com/([^/]+)/status/(\d+)", t.get("url") or "")
    if not m or t.get("promoted"):
        return None
    return {"cid": f"x-{m.group(2)}", "kind": "post", "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}",
            "handle": m.group(1), "name": trim((t.get("user") or "").split("\n")[0], 40),
            "text": trim(t.get("text"), 220), "age_h": _age_h(t.get("created_at")),
            "metrics": {"replies": _num(t.get("reply_label")), "reposts": _num(t.get("rt_label")), "likes": _num(t.get("like_label"))},
            "liked": t.get("liked"), "retweeted": t.get("retweeted"), "source": source, "detail": detail}


def _x_user(handle: str, source: str, detail: str, text: str = "", following=None, follows_you=None) -> dict | None:
    if not HANDLE_X.match(handle or ""):
        return None
    return {"cid": f"xu-{handle.lower()}", "kind": "user", "url": f"https://x.com/{handle}", "handle": handle,
            "text": trim(text, 220), "following": following, "follows_you": follows_you, "source": source, "detail": detail}


def _goto(page, url: str, platform: str, notes: list, tag: str) -> bool:
    page.goto(url, wait_until="domcontentloaded")
    human_pause(3, 6)
    b = detect_block(page, platform)
    if b:
        snapshot(page, f"engage_{tag}_{b}")
        notes.append(f"{tag}: {b} を検知したのでこのアカウントの収集を中止")
        raise _Blocked(b)
    return True


class _Blocked(Exception):
    pass


def _scroll(page, n: int) -> None:
    for _ in range(n):
        page.mouse.wheel(0, 2200)
        human_pause(1.5, 3.0)


def collect_x(page, acct: dict, cfg: dict, notes: list) -> list[dict]:
    lim = cfg.get("limits", {}).get("x", {})
    src = acct.get("sources", {})
    out: list[dict] = []
    tag = acct["id"]
    if src.get("notifications"):
        try:
            _goto(page, "https://x.com/notifications", "x", notes, f"{tag}_notif")
            page.wait_for_selector('[data-testid="cellInnerDiv"]', timeout=15000)
            _scroll(page, 1)
            for n in page.evaluate(X_NOTIF_JS):
                if n["kind"] == "tweet":
                    p = _x_post(n, "notif", "自分への返信・メンション・引用")
                    if p:
                        out.append(p)
                        u = _x_user(p["handle"], "notif", "自分に返信・メンションした人")
                        if u:
                            out.append(u)
                else:
                    # 「おすすめ」「トレンド」などの通知は自分への反応ではないので捨てる
                    if not X_REACTION_RE.search(n["text"]):
                        continue
                    followed = bool(X_FOLLOWED_RE.search(n["text"]))
                    for h in n["handles"]:
                        u = _x_user(h, "notif", trim(n["text"], 120), follows_you=True if followed else None)
                        if u:
                            out.append(u)
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 通知: {type(e).__name__}: {str(e)[:150]}")
            snapshot(page, f"engage_{tag}_notif_err")
    for q in (src.get("keywords") or [])[: lim.get("keywords", 4)]:
        try:
            _goto(page, f"https://x.com/search?q={quote(q + ' -filter:nativeretweets')}&src=typed_query&f=live", "x", notes, f"{tag}_kw")
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            _scroll(page, lim.get("scrolls", 2))
            out += [p for p in (_x_post(t, "keyword", q) for t in page.evaluate(X_TWEETS_JS)) if p]
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 検索「{q}」: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_kw_err")
    for seed in (src.get("seeds") or [])[: lim.get("seeds", 2)]:
        seed = seed.lstrip("@")
        try:
            _goto(page, f"https://x.com/{seed}", "x", notes, f"{tag}_seed")
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            out += [p for p in (_x_post(t, "seed", f"@{seed} の投稿") for t in page.evaluate(X_TWEETS_JS)) if p and p["handle"].lower() == seed.lower()]
            _goto(page, f"https://x.com/{seed}/followers", "x", notes, f"{tag}_seedf")
            page.wait_for_selector('[data-testid="UserCell"]', timeout=15000)
            _scroll(page, lim.get("scrolls", 2))
            for c in page.evaluate(X_USERCELL_JS):
                u = _x_user(c["handle"], "seed", f"@{seed} のフォロワー", c["text"], c["following"], c["follows_you"])
                if u:
                    out.append(u)
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 指定アカウント @{seed}: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_seed_err")
    if src.get("home"):
        try:
            _goto(page, "https://x.com/home", "x", notes, f"{tag}_home")
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            _scroll(page, lim.get("scrolls", 2))
            out += [p for p in (_x_post(t, "home", "ホームのおすすめ") for t in page.evaluate(X_TWEETS_JS)) if p]
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} ホーム: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_home_err")
    return out


# ---------- Instagram ----------

IG_RESERVED = {"explore", "reels", "reel", "direct", "accounts", "p", "stories", "about", "legal", "web", "developer",
               "privacy", "terms", "session", "challenge", "emails", "notifications", "your_activity", "tv", "ar", "lite"}

IG_FEED_JS = r"""
() => Array.from(document.querySelectorAll('article')).map(a => {
  const post = Array.from(a.querySelectorAll('a[href]')).map(x => x.getAttribute('href')).find(h => /\/(p|reel)\/[A-Za-z0-9_-]+/.test(h));
  const author = Array.from(a.querySelectorAll('a[href]')).map(x => x.getAttribute('href')).find(h => /^\/[A-Za-z0-9._]+\/$/.test(h));
  const t = a.querySelector('time');
  return {post, author: author ? author.replaceAll('/', '') : null, text: (a.innerText || '').slice(0, 400),
          created_at: t ? t.getAttribute('datetime') : null,
          liked: !!a.querySelector('svg[aria-label="「いいね！」を取り消す"], svg[aria-label="Unlike"]'),
          sponsored: /広告|Sponsored|プロモーション/.test((a.innerText || '').slice(0, 200))};
})
"""

IG_POSTLINKS_JS = r"""
() => Array.from(new Set(Array.from(document.querySelectorAll('a[href]')).map(x => x.getAttribute('href'))
       .filter(h => /\/(p|reel)\/[A-Za-z0-9_-]+\/?$/.test(h))))
"""

IG_POST_JS = r"""
() => {
  const meta = n => { const m = document.querySelector(`meta[property="${n}"], meta[name="${n}"]`); return m ? m.content : ''; };
  const scope = document.querySelector('main') || document.body;
  const hdr = scope.querySelector('header');
  const authorA = hdr ? Array.from(hdr.querySelectorAll('a[href]')).find(a => /^\/[A-Za-z0-9._]+\/$/.test(a.getAttribute('href'))) : null;
  const t = scope.querySelector('time');
  const h1 = scope.querySelector('h1');
  const big = Array.from(scope.querySelectorAll('svg[aria-label]')).filter(s => s.getBoundingClientRect().height >= 20);
  return {
    author: authorA ? authorA.getAttribute('href').replaceAll('/', '') : null,
    caption: h1 ? h1.innerText : '',
    og_desc: meta('og:description') || meta('description'),
    created_at: t ? t.getAttribute('datetime') : null,
    body: scope.innerText.slice(0, 1500),
    liked: big.some(s => /「いいね！」を取り消す|^Unlike$/.test(s.getAttribute('aria-label'))),
  };
}
"""

IG_DIALOG_USERS_JS = r"""
() => {
  const onlyUser = (el, u) => {
    const hs = new Set(Array.from(el.querySelectorAll('a[href]')).map(x => x.getAttribute('href'))
      .filter(h => /^\/[A-Za-z0-9._]+\/$/.test(h)).map(h => h.replaceAll('/', '')));
    return hs.size <= 1 && (hs.size === 0 || hs.has(u));
  };
  const d = document.querySelector('[role="dialog"]');
  if (!d) return [];
  const seen = new Set(); const out = [];
  d.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href');
    if (!/^\/[A-Za-z0-9._]+\/$/.test(h)) return;
    const u = h.replaceAll('/', '');
    if (seen.has(u)) return; seen.add(u);
    let row = a;
    while (row.parentElement && row.parentElement !== d && onlyUser(row.parentElement, u)) row = row.parentElement;
    const btn = Array.from(row.querySelectorAll('button, div[role="button"]')).map(b => b.innerText.trim()).find(Boolean) || '';
    out.push({handle: u, text: (row.innerText || '').slice(0, 200), button: btn});
  });
  return out;
}
"""

IG_SCROLL_DIALOG_JS = r"""
() => {
  const d = document.querySelector('[role="dialog"]'); if (!d) return false;
  const els = Array.from(d.querySelectorAll('div')).filter(e => e.scrollHeight > e.clientHeight + 20);
  const el = els.sort((a, b) => b.scrollHeight - a.scrollHeight)[0]; if (!el) return false;
  el.scrollTop = el.scrollHeight; return true;
}
"""

IG_NOTIF_JS = r"""
() => {
  const onlyUser = (el, u) => {
    const hs = new Set(Array.from(el.querySelectorAll('a[href]')).map(x => x.getAttribute('href'))
      .filter(h => /^\/[A-Za-z0-9._]+\/$/.test(h)).map(h => h.replaceAll('/', '')));
    return hs.size <= 1 && (hs.size === 0 || hs.has(u));
  };
  const scope = document.querySelector('[role="dialog"]') || document.querySelector('main') || document.body;
  const seen = new Set(); const out = [];
  scope.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href');
    if (!/^\/[A-Za-z0-9._]+\/$/.test(h)) return;
    if (a.closest('nav')) return;
    const u = h.replaceAll('/', '');
    let row = a;
    while (row.parentElement && row.parentElement !== scope && onlyUser(row.parentElement, u)) row = row.parentElement;
    const text = (row.innerText || '').slice(0, 200);
    const key = u + '|' + text.slice(0, 40); if (seen.has(key)) return; seen.add(key);
    out.push({handle: u, text});
  });
  return out;
}
"""

IG_REACTION_RE = re.compile(r"フォローしました|フォローを開始|いいね|コメントしました|メンション|タグ付け|"
                            r"started following|liked your|commented|mentioned you|tagged you", re.I)
LIKES_RE = re.compile(r"([\d,\.]+\s*[KkMm万千]?)\s*(?:likes|件の「?いいね|人が「?いいね)|いいね！?\s*([\d,\.]+\s*[万千]?)\s*件", re.I)


def _ig_handle_ok(h: str | None, own: str) -> bool:
    return bool(h) and HANDLE_IG.match(h) is not None and h.lower() not in IG_RESERVED and h.lower() != own.lower()


def _ig_code(href: str) -> str | None:
    m = re.search(r"/(p|reel)/([A-Za-z0-9_-]+)", href or "")
    return m.group(2) if m else None


def _ig_post(code: str, author: str | None, text: str, likes, liked, created, source: str, detail: str) -> dict:
    return {"cid": f"ig-{code}", "kind": "post", "url": f"https://www.instagram.com/p/{code}/", "handle": author,
            "text": trim(text, 220), "age_h": _age_h(created), "metrics": {"likes": likes}, "liked": liked,
            "source": source, "detail": detail}


def _ig_user(h: str, source: str, detail: str, text: str = "", following=None) -> dict:
    return {"cid": f"igu-{h.lower()}", "kind": "user", "url": f"https://www.instagram.com/{h}/", "handle": h,
            "text": trim(text, 220), "following": following, "source": source, "detail": detail}


def _ig_open_post(page, code: str, own: str, source: str, detail: str, notes: list, tag: str) -> dict | None:
    _goto(page, f"https://www.instagram.com/p/{code}/", "instagram", notes, tag)
    info = page.evaluate(IG_POST_JS)
    author = info.get("author")
    if not author:
        m = re.search(r"-\s*([A-Za-z0-9._]+)\s*(?:on|さん)", info.get("og_desc") or "")
        author = m.group(1) if m else None
    lm = LIKES_RE.search((info.get("og_desc") or "") + " " + (info.get("body") or ""))
    likes = parse_count(lm.group(1) or lm.group(2)) if lm else None
    text = info.get("caption") or info.get("og_desc") or ""
    if author and author.lower() == own.lower():
        return None
    return _ig_post(code, author, text, likes, info.get("liked"), info.get("created_at"), source, detail)


def collect_ig(page, acct: dict, cfg: dict, notes: list) -> list[dict]:
    lim = cfg.get("limits", {}).get("instagram", {})
    src = acct.get("sources", {})
    own = acct["handle"]
    out: list[dict] = []
    tag = acct["id"]
    if src.get("notifications"):
        try:
            _goto(page, "https://www.instagram.com/notifications/", "instagram", notes, f"{tag}_notif")
            rows = page.evaluate(IG_NOTIF_JS)
            if len(rows) < 2:  # PC版では通知がパネルで開くことがある
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
                human_pause(4, 6)
                btn = page.locator('svg[aria-label="通知"], svg[aria-label="Notifications"]').first
                btn.click()
                human_pause(3, 5)
                rows = page.evaluate(IG_NOTIF_JS)
            for r in rows:
                # 「おすすめ」のユーザーなど、自分への反応でない行は捨てる
                if _ig_handle_ok(r["handle"], own) and IG_REACTION_RE.search(r["text"]):
                    out.append(_ig_user(r["handle"], "notif", trim(r["text"], 120)))
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 通知: {type(e).__name__}: {str(e)[:150]}")
            snapshot(page, f"engage_{tag}_notif_err")
    for kw in (src.get("keywords") or [])[: lim.get("keywords", 3)]:
        tagname = kw.lstrip("#")
        try:
            _goto(page, f"https://www.instagram.com/explore/tags/{quote(tagname)}/", "instagram", notes, f"{tag}_kw")
            links = page.evaluate(IG_POSTLINKS_JS)
            if not links:
                _goto(page, f"https://www.instagram.com/explore/search/keyword/?q={quote('#' + tagname)}", "instagram", notes, f"{tag}_kw2")
                _scroll(page, 1)
                links = page.evaluate(IG_POSTLINKS_JS)
            codes = list(dict.fromkeys(c for c in (_ig_code(h) for h in links) if c))[: lim.get("posts_per_keyword", 6)]
            if not codes:
                snapshot(page, f"engage_{tag}_kw_empty")
                notes.append(f"{tag} 「{kw}」: 投稿のリンクが見つからない")
            for code in codes:
                p = _ig_open_post(page, code, own, "keyword", kw, notes, f"{tag}_kwpost")
                if p:
                    out.append(p)
                    if _ig_handle_ok(p["handle"], own):
                        out.append(_ig_user(p["handle"], "keyword", f"「{kw}」の投稿者", p["text"]))
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 「{kw}」: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_kw_err")
    for seed in (src.get("seeds") or [])[: lim.get("seeds", 2)]:
        seed = seed.lstrip("@")
        try:
            _goto(page, f"https://www.instagram.com/{seed}/", "instagram", notes, f"{tag}_seed")
            codes = list(dict.fromkeys(c for c in (_ig_code(h) for h in page.evaluate(IG_POSTLINKS_JS)) if c))[:3]
            fl = page.locator(f'a[href="/{seed}/followers/"]').first
            if fl.count():
                fl.click()
                page.wait_for_selector('[role="dialog"]', timeout=15000)
                human_pause(2, 4)
                for _ in range(lim.get("scrolls", 2)):
                    page.evaluate(IG_SCROLL_DIALOG_JS)
                    human_pause(2, 3.5)
                for r in page.evaluate(IG_DIALOG_USERS_JS):
                    if _ig_handle_ok(r["handle"], own) and r["handle"].lower() != seed.lower():
                        following = bool(re.search(r"^(フォロー中|Following|リクエスト済み|Requested)$", r["button"]))
                        out.append(_ig_user(r["handle"], "seed", f"@{seed} のフォロワー", r["text"], following))
                page.keyboard.press("Escape")
            for code in codes:
                p = _ig_open_post(page, code, own, "seed", f"@{seed} の投稿", notes, f"{tag}_seedpost")
                if p:
                    out.append(p)
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} 指定アカウント @{seed}: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_seed_err")
    if src.get("home"):
        try:
            _goto(page, "https://www.instagram.com/", "instagram", notes, f"{tag}_home")
            _scroll(page, lim.get("scrolls", 2))
            for a in page.evaluate(IG_FEED_JS):
                code = _ig_code(a.get("post") or "")
                if not code or a.get("sponsored") or not _ig_handle_ok(a.get("author"), own):
                    continue
                out.append(_ig_post(code, a["author"], a["text"], None, a["liked"], a.get("created_at"), "home", "ホームのフィード"))
        except _Blocked:
            raise
        except Exception as e:
            notes.append(f"{tag} ホーム: {type(e).__name__}: {str(e)[:120]}")
            snapshot(page, f"engage_{tag}_home_err")
    return out


# ---------- 自分のフォロー一覧 ----------

def _following_state(acct_id: str):
    from lib.common import STATE_DIR
    return STATE_DIR / f"engage_following_{acct_id}.json"


def fetch_following(page, acct: dict, cfg: dict, notes: list) -> set[str]:
    """自分のフォロー一覧を最後まで読み、フォロー中の相手を候補から外すのに使う。
    読めなかったときは前回の一覧(state/engage_following_<id>.json)を使う。"""
    from lib.common import read_json, write_json
    max_scrolls = int(cfg.get("collect", {}).get("following_max_scrolls", 60))
    h, got = acct["handle"], set()
    tag = acct["id"]
    try:
        if acct["platform"] == "x":
            _goto(page, f"https://x.com/{h}/following", "x", notes, f"{tag}_following")
            page.wait_for_selector('[data-testid="UserCell"]', timeout=15000)
            still = 0
            for _ in range(max_scrolls):
                before = len(got)
                got |= {c["handle"].lower() for c in page.evaluate(X_USERCELL_JS) if c["handle"]}
                still = still + 1 if len(got) == before else 0
                if still >= 3:
                    break
                page.mouse.wheel(0, 2500)
                time.sleep(1.5)
        else:
            _goto(page, f"https://www.instagram.com/{h}/", "instagram", notes, f"{tag}_following")
            link = page.locator(f'a[href="/{h}/following/"]').first
            link.click()
            page.wait_for_selector('[role="dialog"]', timeout=15000)
            time.sleep(2)
            still = 0
            for _ in range(max_scrolls):
                before = len(got)
                got |= {r["handle"].lower() for r in page.evaluate(IG_DIALOG_USERS_JS)}
                still = still + 1 if len(got) == before else 0
                if still >= 3:
                    break
                page.evaluate(IG_SCROLL_DIALOG_JS)
                time.sleep(1.8)
            page.keyboard.press("Escape")
            got.discard(h.lower())
    except _Blocked:
        raise
    except Exception as e:
        notes.append(f"{tag} フォロー一覧: {type(e).__name__}: {str(e)[:120]}")
        snapshot(page, f"engage_{tag}_following_err")
    path = _following_state(acct["id"])
    if got:
        write_json(path, {"fetched_at": now().isoformat(timespec="minutes"), "handles": sorted(got)})
        return got
    prev = read_json(path, {}).get("handles", [])
    if prev:
        notes.append(f"{tag} フォロー一覧が読めなかったので前回の一覧({len(prev)}人)を使う")
    return set(prev)


# ---------- まとめ ----------

def finalize(acct: dict, cands: list[dict], cfg: dict, history: dict, following: set[str] | None = None) -> list[dict]:
    """重複をまとめ、実行済み・自分・古すぎる・反応済みを除き、出所の重みで並べて上限件数に絞る。"""
    max_age_h = cfg.get("collect", {}).get("max_post_age_days", 3) * 24
    own = acct["handle"].lower()
    done_targets = {k.split("|", 2)[2] for k in history if k.startswith(acct["id"] + "|")}
    merged: dict[str, dict] = {}
    for c in cands:
        if not c or not c.get("cid"):
            continue
        if (c.get("handle") or "").lower() == own:
            continue
        target = c["url"].lower()
        if target in done_targets:
            continue
        if c["kind"] == "post":
            if c.get("age_h") is not None and c["age_h"] > max_age_h:
                continue
            if c.get("liked") and (acct["platform"] == "instagram" or c.get("retweeted")):
                continue
        if c["kind"] == "user" and (c.get("following") or (c.get("handle") or "").lower() in (following or set())):
            continue  # すでにフォローしている相手は候補にしない
        m = merged.get(c["cid"])
        if m:
            if SOURCE_WEIGHT.get(c["source"], 0) > SOURCE_WEIGHT.get(m["source"], 0):
                c["also"] = sorted(set(m.get("also", []) + [m["source"]]))
                merged[c["cid"]] = c
            else:
                m.setdefault("also", [])
                if c["source"] not in m["also"] and c["source"] != m["source"]:
                    m["also"].append(c["source"])
            continue
        merged[c["cid"]] = c

    def rank(c: dict) -> float:
        s = SOURCE_WEIGHT.get(c["source"], 0) + 0.5 * len(c.get("also", []))
        likes = (c.get("metrics") or {}).get("likes") or 0
        s += min(likes, 500) / 500  # 反応が多すぎる大手より、届きやすい規模を少しだけ優先する程度
        if c.get("follows_you"):
            s += 1
        return s

    items = sorted(merged.values(), key=rank, reverse=True)
    for c in items:
        c["account"] = acct["id"]
        c["platform"] = acct["platform"]
    return items[: cfg.get("collect", {}).get("max_candidates_per_account", 60)]


def run(_ctx, _cfg_unused=None) -> dict:
    from playwright.sync_api import sync_playwright

    cfg = load_engage_config()
    accounts = enabled_accounts(cfg)
    if not accounts:
        return result("skipped", notes=["config/engage.yaml に有効なアカウントが無い"])
    history = load_history()
    per_account: dict[str, dict] = {}
    items: list[dict] = []
    notes: list[str] = []
    with sync_playwright() as pw:
        for acct in accounts:
            info = {"platform": acct["platform"], "handle": acct["handle"], "status": "ok", "notes": [], "count": 0}
            per_account[acct["id"]] = info
            if paused_until(acct["id"]):
                info["status"] = "paused"
                info["notes"].append(f"一時停止中(~{paused_until(acct['id'])})")
                continue
            steps: list[str] = []
            browser = page = None
            try:
                browser = connect_account(pw, acct, steps)
                page = new_tab(browser)
                h = current_handle(page, acct["platform"])
                if not h or h.lower() != acct["handle"].lower():
                    info["status"] = "account_mismatch"
                    info["notes"].append(f"ログイン中のアカウントが {h} で、設定の {acct['handle']} と違う(または読めない)ので収集しない")
                    snapshot(page, f"engage_{acct['id']}_mismatch")
                    continue
                t0 = time.time()
                following = fetch_following(page, acct, cfg, info["notes"])
                info["following_count"] = len(following)
                raw = collect_x(page, acct, cfg, info["notes"]) if acct["platform"] == "x" else collect_ig(page, acct, cfg, info["notes"])
                got = finalize(acct, raw, cfg, history, following)
                info["count"] = len(got)
                info["raw_count"] = len(raw)
                info["seconds"] = round(time.time() - t0)
                items += got
                log(f"engage {acct['id']}: 候補 {len(got)} 件(読み取り {len(raw)} 件)")
            except _Blocked as b:
                info["status"] = f"blocked:{b}"
            except Exception as e:
                info["status"] = "error"
                info["notes"].append(f"{type(e).__name__}: {str(e)[:300]}")
            finally:
                info["notes"] = steps + info["notes"]
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                if browser:
                    release(browser)
            human_pause(3, 6)
    for k, v in per_account.items():
        if v["status"] != "ok":
            notes.append(f"{k}: {v['status']} {'; '.join(v['notes'][:2])}")
    status = "ok" if all(v["status"] in ("ok", "paused") for v in per_account.values()) else "partial"
    return result(status, items, notes, accounts=per_account)
