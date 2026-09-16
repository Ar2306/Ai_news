#!/usr/bin/env python3
"""Send the daily AR digest to configured channels.

Channels (each is enabled only when its environment variables are set):

  Email (Resend):   RESEND_API_KEY, NEWSLETTER_FROM, NEWSLETTER_TO
                    + optional NEWSLETTER_SUBSCRIBERS (comma-separated extra recipients)
  Telegram:         TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Usage:
  python3 scripts/send_digest.py            # send to every configured channel
  python3 scripts/send_digest.py --dry-run  # build the digests, print, don't send

Channels with missing credentials are skipped with a note, so the daily
workflow stays green while a channel is still being configured.
"""

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "newsletter.json"

EMAIL_TOTAL = 15     # stories in the email
TELEGRAM_TOTAL = 6   # stories in the Telegram message
SITE_NAME = "AR"


def load_digest():
    if not DATA_FILE.exists():
        raise SystemExit(f"❌ {DATA_FILE} not found — run the fetcher first")
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    articles = data.get("all_articles", [])
    if not articles:
        raise SystemExit("ℹ No articles in newsletter.json — nothing to send")
    return data


def build_stories(data, total=EMAIL_TOTAL):
    """Newest stories first from the flat all_articles list."""
    articles = sorted(data.get("all_articles", []), key=lambda a: a.get("published_at", ""), reverse=True)
    return articles[:total]


def summary_lines(a, limit=220):
    """[(label, text)] for the non-empty What / Why / Who lines."""
    s = a.get("summary") or {}
    if isinstance(s, str):
        s = {"what": s}
    lines = []
    for key, label in (("what", "What"), ("why", "Why"), ("who", "Who")):
        text = (s.get(key) or "").strip()
        if len(text) > limit:
            text = text[:limit - 1].rstrip() + "…"
        if text:
            lines.append((label, text))
    return lines


def fmt_tags(a):
    return " · ".join(a.get("tags") or [])


def fmt_date(iso, fmt):
    try:
        return datetime.fromisoformat(iso).strftime(fmt)
    except Exception:
        return (iso or "")[:10]


def build_html(stories, data):
    date_str = fmt_date(data.get("generated_at", ""), "%A, %B %d, %Y")

    cards = []
    for a in stories:
        title = html.escape(a.get("title", ""))
        url = html.escape(a.get("url", "#"))
        meta = html.escape(" · ".join(filter(None, [a.get("source", ""), fmt_date(a.get("published_at", ""), "%b %d")])))
        lines = "".join(
            f'<p style="margin:4px 0;font-size:14px;line-height:1.55;color:#1a1a1a;"><strong>{label}</strong> {html.escape(text)}</p>'
            for label, text in summary_lines(a)
        )
        cards.append(f"""
      <div style="padding:20px 0;border-bottom:1px solid #e6e6e6;">
        <div style="font-size:12px;color:#1f5fd6;margin-bottom:4px;">{html.escape(fmt_tags(a))}</div>
        <a href="{url}" style="font-size:16px;font-weight:600;color:#1a1a1a;text-decoration:none;line-height:1.4;">{title}</a>
        {lines}
        <p style="margin:8px 0 0;font-size:13px;color:#6b6b6b;">{meta}</p>
      </div>""")

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="font-size:26px;font-weight:700;color:#1a1a1a;">{SITE_NAME}</div>
    <div style="font-size:14px;color:#6b6b6b;margin-top:4px;">{date_str} · {data.get('article_count', 0)} items</div>
    {''.join(cards)}
    <p style="font-size:12px;color:#6b6b6b;margin-top:24px;">
      <a href="https://github.com/DavidGaso1/AI-News-Update" style="color:#6b6b6b;">View the full digest</a>
    </p>
  </div>
</body>
</html>"""


def build_text(stories, data):
    date_str = fmt_date(data.get("generated_at", ""), "%A, %B %d, %Y")

    lines = [f"{SITE_NAME} — {date_str}", f"{data.get('article_count', 0)} items", "=" * 40]
    for a in stories:
        lines.append("")
        lines.append(f"[{fmt_tags(a)}] {a.get('title', '')}")
        for label, text in summary_lines(a):
            lines.append(f"   {label}: {text}")
        lines.append(f"   {a.get('source', '')} — {a.get('url', '')}")
    lines.append("")
    lines.append("Full digest: https://github.com/DavidGaso1/AI-News-Update")
    return "\n".join(lines)


def build_telegram(data, total=TELEGRAM_TOTAL):
    """Compact Telegram message: newest N stories with tags and the What line."""
    date_str = fmt_date(data.get("generated_at", ""), "%b %d, %Y")

    lines = [f"<b>{SITE_NAME} — {date_str}</b>", ""]
    for a in build_stories(data, total):
        # Truncate BEFORE escaping so an HTML entity is never split mid-way
        # (Telegram's HTML parser rejects a truncated entity with HTTP 400).
        title = html.escape(a.get("title", "")[:170])
        url = html.escape(a.get("url", "#"))
        lines.append(f"• <a href=\"{url}\">{title}</a>")
        what = dict(summary_lines(a, limit=160)).get("What")
        if what:
            lines.append(html.escape(what))
        lines.append(f"<i>{html.escape(fmt_tags(a))} · {html.escape(a.get('source', ''))}</i>")
        lines.append("")
    lines.append("<a href=\"https://github.com/DavidGaso1/AI-News-Update\">Full digest →</a>")
    return "\n".join(lines)


def parse_recipients(raw):
    """Split a comma-separated recipient list, dropping empties/duplicates."""
    return list(dict.fromkeys(r.strip() for r in (raw or "").split(",") if r.strip()))


def send_resend(subject, html_body, text_body):
    api_key = os.environ.get("RESEND_API_KEY")
    frm = os.environ.get("NEWSLETTER_FROM")
    to = list(dict.fromkeys(
        parse_recipients(os.environ.get("NEWSLETTER_TO")) + parse_recipients(os.environ.get("NEWSLETTER_SUBSCRIBERS"))
    ))
    if not (api_key and frm and to):
        print("ℹ Email channel not configured (RESEND_API_KEY / NEWSLETTER_FROM / NEWSLETTER_TO) — skipping")
        return False
    payload = {
        "from": frm,
        "to": to,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        print(f"❌ Email failed (network): {e}")
        return False
    if resp.status_code in (200, 201):
        print(f"✅ Email sent to {', '.join(to)} (id {resp.json().get('id', '?')})")
        return True
    print(f"❌ Email failed ({resp.status_code}): {resp.text[:300]}")
    return False


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("ℹ Telegram channel not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — skipping")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        print(f"❌ Telegram failed (network): {e}")
        return False
    if resp.status_code == 200:
        print(f"✅ Telegram message sent to chat {chat_id}")
        return True
    print(f"❌ Telegram failed ({resp.status_code}): {resp.text[:300]}")
    return False


def main():
    parser = argparse.ArgumentParser(description="Send the daily AR digest")
    parser.add_argument("--dry-run", action="store_true", help="build and print the digests without sending")
    parser.add_argument("--telegram-only", action="store_true", help="only send the Telegram message")
    args = parser.parse_args()

    data = load_digest()
    stories = build_stories(data)
    if not stories:
        print("ℹ No stories to send")
        return 0

    generated = data.get("generated_at", "") or datetime.now(timezone.utc).isoformat()
    subject = f"{SITE_NAME} — {fmt_date(generated, '%b %d, %Y')}"

    if args.dry_run:
        print("=" * 60)
        print("DRY RUN — nothing will be sent")
        print("=" * 60)
        print("\n--- EMAIL SUBJECT:", subject)
        print("--- EMAIL HTML (first 600 chars):")
        print(build_html(stories, data)[:600], "...")
        print("\n--- EMAIL TEXT:")
        print(build_text(stories, data))
        print("\n--- TELEGRAM MESSAGE:")
        print(build_telegram(data))
        return 0

    ok = 0
    if not args.telegram_only:
        ok += int(send_resend(subject, build_html(stories, data), build_text(stories, data)))
    ok += int(send_telegram(build_telegram(data)))
    return 0 if ok > 0 or not any(
        os.environ.get(k) for k in ("RESEND_API_KEY", "TELEGRAM_BOT_TOKEN")
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
