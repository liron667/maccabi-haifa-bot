"""Maccabi Haifa news bot: fetch -> keyword filter -> post to Telegram.

    python bot.py --dry-run   # print what it would post, change nothing
    python bot.py             # post new matching items and update seen.json
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DRY_RUN = "--dry-run" in sys.argv
UA = "Mozilla/5.0 (compatible; maccabi-haifa-bot)"


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    p = ROOT / "seen.json"
    return json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}


def save_seen(seen):
    (ROOT / "seen.json").write_text(
        json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def matches(text, keywords, exclusions):
    t = text.lower()
    if any(x.lower() in t for x in exclusions):
        return False
    return any(k.lower() in t for k in keywords)


def within_lookback(when, cutoff):
    return when is None or when >= cutoff


# ---- sources: each returns list of {id, title, summary, link, source} ----

def collect_rss(feeds, cutoff):
    items = []
    for feed in feeds or []:
        try:
            parsed = feedparser.parse(feed["url"])
            for e in parsed.entries:
                when = None
                if getattr(e, "published_parsed", None):
                    when = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                if not within_lookback(when, cutoff):
                    continue
                link = e.get("link", "")
                items.append({
                    "id": e.get("id") or link,
                    "title": e.get("title", ""),
                    "summary": re.sub("<[^>]+>", " ", e.get("summary", "")),
                    "link": link,
                    "source": feed["name"],
                })
        except Exception as ex:
            print(f"[warn] RSS {feed.get('name')} failed: {ex}")
    return items


def collect_html(sources, _cutoff):
    items = []
    for src in sources or []:
        try:
            page = requests.get(src["url"], headers={"User-Agent": UA}, timeout=20).text
            seen_ids = set()
            for a in re.finditer(
                r'<a[^>]+href="([^"]*' + src["link_regex"] + r'[^"]*)"[^>]*>(.*?)</a>',
                page, re.IGNORECASE | re.DOTALL,
            ):
                link = html.unescape(a.group(1))
                if not link.startswith("http"):
                    link = requests.compat.urljoin(src["url"], link)
                title = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", a.group(2))).strip()
                title = html.unescape(title)
                title = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{4}.*$", "", title).strip()
                if not title or link in seen_ids:
                    continue
                seen_ids.add(link)
                items.append({
                    "id": link, "title": title, "summary": "",
                    "link": link, "source": src["name"],
                    "always": src.get("always_relevant", False),
                })
        except Exception as ex:
            print(f"[warn] HTML {src.get('name')} failed: {ex}")
    return items


def collect_telegram(channels, cutoff):
    api_id = os.getenv("TELEGRAM_API_ID")
    session = os.getenv("TELETHON_SESSION")
    if not (channels and api_id and session):
        return []
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

    def has_link(msg):  # club promos/ads carry a link or button; real news is plain text
        if msg.buttons:
            return True
        if msg.entities and any(
            isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)) for e in msg.entities
        ):
            return True
        return "http" in msg.message.lower()

    items = []
    try:
        with TelegramClient(StringSession(session), int(api_id),
                            os.getenv("TELEGRAM_API_HASH")) as client:
            for ch in channels:
                try:
                    for msg in client.iter_messages(ch, limit=40):
                        if msg.date < cutoff:
                            break
                        if not msg.message:
                            continue
                        if has_link(msg):  # skip channel self-promo/ads
                            continue
                        items.append({
                            "id": f"{ch}:{msg.id}",
                            "title": msg.message.split("\n")[0][:200],
                            "summary": msg.message,
                            "link": f"https://t.me/{ch}/{msg.id}",
                            "source": f"Telegram @{ch}",
                        })
                except Exception as ex:
                    print(f"[warn] Telegram {ch} failed: {ex}")
    except Exception as ex:
        print(f"[warn] Telegram login failed: {ex}")
    return items


def fetch_article_text(url):
    """Best-effort article body: og meta + paragraph text. Empty string on failure."""
    try:
        doc = requests.get(url, headers={"User-Agent": UA}, timeout=15).text
    except Exception:
        return ""

    def meta(prop):
        m = re.search(
            r'<meta[^>]+(?:property|name)="' + prop + r'"[^>]+content="([^"]*)"', doc, re.I
        )
        return html.unescape(m.group(1)) if m else ""

    parts = [meta("og:title"), meta("og:description")]
    for p in re.findall(r"<p[^>]*>(.*?)</p>", doc, re.I | re.S):
        t = html.unescape(re.sub("<[^>]+>", " ", p)).strip()
        if len(t) > 40:
            parts.append(t)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:2000]


def ai_message(title, text, ai_cfg):
    """Ask Gemini for a catchy Hebrew headline + summary. None if disabled/failed."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = ai_cfg.get("model", "gemini-2.0-flash")
    team = ai_cfg.get("team", "")
    prompt = (
        f"אתה עורך תוכן לערוץ טלגרם בעברית בנושא {team} (כדורגל). בהינתן כותרת וטקסט של כתבה: "
        "אם הכתבה אינה עוסקת בכדורגל (למשל כדורסל, כדוריד או ענף ספורט אחר), החזר בדיוק את המילה SKIP וכלום מלבדה. "
        "אחרת, כתוב הודעה קצרה וקולעת בעברית: שורה ראשונה = כותרת קליטה שמתחילה באימוג'י אחד מתאים, "
        "אחריה שורת רווח, ואז סיכום של 1-2 משפטים. אל תמציא עובדות, ואל תוסיף קישורים או האשטגים.\n\n"
        f"כותרת: {title}\n\nטקסט: {text}"
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=90,
        )
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip() or None
    except Exception as ex:
        print(f"[warn] AI failed: {ex}")
        return None


def post(item, token, channel):
    if item["link"].startswith("https://t.me/"):
        # Telegram-sourced: post the text only, no t.me link / @mention.
        text = html.escape((item.get("summary") or item["title"])[:3800])
    elif item.get("ai_text"):
        head, _, body = item["ai_text"].partition("\n")
        text = f"<b>{html.escape(head.strip())}</b>\n\n{html.escape(body.strip())}\n\n{html.escape(item['link'])}"
        if item.get("hashtags"):
            text += "\n\n" + html.escape(" ".join(item["hashtags"]))
    else:
        text = (f"<b>{html.escape(item['title'])}</b>\n\n"
                f"{html.escape(item['link'])}\n\n"
                f"<i>{html.escape(item['source'])}</i>")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": channel, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


def prune(seen, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for k in list(seen):
        try:
            if datetime.fromisoformat(seen[k]) < cutoff:
                del seen[k]
        except ValueError:
            pass


def main():
    cfg = load_config()
    seen = load_seen()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.get("lookback_hours", 3))

    items = (collect_rss(cfg.get("rss_feeds"), cutoff)
             + collect_html(cfg.get("html_sources"), cutoff)
             + collect_telegram(cfg.get("telegram_channels"), cutoff))

    keywords, exclusions = cfg["keywords"], cfg.get("exclusions", [])
    ai_cfg = cfg.get("ai") or {}
    token = os.getenv("BOT_TOKEN")
    channel = os.getenv("TARGET_CHANNEL_ID") or cfg.get("target_channel_id")

    posted = 0
    for item in items:
        if item["id"] in seen:
            continue
        if not item.get("always") and not matches(
            f"{item['title']} {item['summary']}", keywords, exclusions
        ):
            continue
        if ai_cfg.get("enabled") and not item["link"].startswith("https://t.me/"):
            body = fetch_article_text(item["link"]) or item.get("summary", "")
            ai_text = ai_message(item["title"], body, ai_cfg)
            if ai_text and ai_text.strip().upper().startswith("SKIP"):
                # AI judged this off-topic (e.g. basketball) — remember and skip.
                if DRY_RUN:
                    print(f"[skip non-soccer] {item['source']}: {item['title']}")
                else:
                    seen[item["id"]] = datetime.now(timezone.utc).isoformat()
                continue
            item["ai_text"] = ai_text
            if ai_text:
                item["hashtags"] = ai_cfg.get("hashtags", [])
        if DRY_RUN:
            preview = item.get("ai_text") or f"{item['title']}"
            print(f"[would post] {item['source']}:\n{preview}\n  {item['link']}\n")
        else:
            post(item, token, channel)
            seen[item["id"]] = datetime.now(timezone.utc).isoformat()
        posted += 1

    if not DRY_RUN:
        prune(seen, cfg.get("seen_retention_days", 30))
        save_seen(seen)
    print(f"{'[dry-run] ' if DRY_RUN else ''}{posted} new matching item(s) from "
          f"{len(items)} fetched.")


if __name__ == "__main__":
    main()
