"""
ZPPA Tender Monitor — GitHub Actions Edition
==============================================
Designed to run as a single-shot script on a GitHub Actions schedule.
State is persisted via a JSON file committed back to the repo between runs.

Notifications:
  - Email via Gmail SMTP  (set ZPPA_EMAIL_TO, ZPPA_EMAIL_FROM, ZPPA_EMAIL_PASS as repo secrets)
  - Telegram bot message  (set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID as repo secrets) [optional]

Requirements (requirements.txt):
  requests
  beautifulsoup4
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys
import time
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# CONFIGURATION
# Edit KEYWORDS to match the types of tenders you want alerts for.
# ─────────────────────────────────────────────

KEYWORDS = [
    "cleaning",
    "maintenance cleaning",
    "janitorial",
    "sanitation",
    "hygiene",
    "housekeeping",
    "pest control",
    "waste management",
    "facilities management",
    "general maintenance",
    "cleaning services",
]

# How many pages to scan per run (1 page = 10 tenders, most recent first)
# 5 pages = 50 tenders. Increase if tenders are posted very frequently.
MAX_PAGES = 30

# JSON file that tracks which tenders have already triggered an alert.
# This file is committed back to your repo by the workflow so state persists.
STATE_FILE = "zppa_seen_tenders.json"

# ─────────────────────────────────────────────
# SECRETS — loaded from environment (GitHub Secrets)
# ─────────────────────────────────────────────

EMAIL_TO        = os.environ.get("ZPPA_EMAIL_TO", "")
EMAIL_FROM      = os.environ.get("ZPPA_EMAIL_FROM", "")
EMAIL_PASS      = os.environ.get("ZPPA_EMAIL_PASS", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ─────────────────────────────────────────────
# PORTAL URLS
# ─────────────────────────────────────────────

BASE_URL        = "https://eprocure.zppa.org.zm"
OPENED_BIDS_URL = f"{BASE_URL}/epps/common/viewOpenedTenders.do"
TENDER_URL      = f"{BASE_URL}/epps/cft/prepareViewCfTWS.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("[WARN] State file corrupted, starting fresh.")
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"[STATE] Saved {len(state)} tracked tender(s) to {STATE_FILE}")


# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────

def fetch_page(page: int) -> BeautifulSoup | None:
    params = {"d-3680181-p": page, "d-3680181-n": 1}
    try:
        resp = requests.get(
            OPENED_BIDS_URL, params=params, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        print(f"[ERROR] Page {page} fetch failed: {e}")
        return None


def parse_tenders(soup: BeautifulSoup) -> list[dict]:
    tenders = []
    table = soup.find("table")
    if not table:
        return tenders

    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 7:
            continue

        title_tag = cols[1].find("a")
        if not title_tag:
            continue

        href  = title_tag.get("href", "")
        link  = href if href.startswith("http") else (BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}")
        rid   = href.split("resourceId=")[-1].split("&")[0] if "resourceId=" in href else ""
        title = title_tag.get_text(strip=True)

        tenders.append({
            "id":       rid or cols[2].get_text(strip=True),
            "title":    title,
            "ref":      cols[2].get_text(strip=True),
            "entity":   cols[3].get_text(strip=True),
            "deadline": cols[4].get_text(strip=True),
            "method":   cols[5].get_text(strip=True),
            "status":   cols[6].get_text(strip=True) if len(cols) > 6 else "Unknown",
            "link":     link,
        })
    return tenders


def scrape_all(max_pages: int) -> list[dict]:
    results = []
    for page in range(1, max_pages + 1):
        print(f"[SCRAPE] Page {page}/{max_pages}...")
        soup = fetch_page(page)
        if not soup:
            break
        rows = parse_tenders(soup)
        if not rows:
            print(f"[SCRAPE] No rows on page {page}, stopping.")
            break
        results.extend(rows)
        time.sleep(1.5)  # polite delay
    return results


def keyword_match(tender: dict) -> str | None:
    """Return the matched keyword, or None if no match."""
    title_lower = tender["title"].lower()
    for kw in KEYWORDS:
        if kw.lower() in title_lower:
            return kw
    return None


def get_tender_status(resource_id: str) -> str:
    """Re-fetch a tender page and extract its current status."""
    if not resource_id.isdigit():
        return "Unknown"
    try:
        resp = requests.get(
            TENDER_URL,
            params={"resourceId": resource_id},
            headers=HEADERS,
            timeout=20,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text()
        for word in ["Awarded", "Cancelled", "Evaluation", "Published", "Closed", "Pending", "Suspended"]:
            if word.lower() in text.lower():
                return word
        return "Unknown"
    except Exception as e:
        return f"Error ({e})"


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def build_email(new_tenders: list[dict], status_changes: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    new_rows = ""
    for t in new_tenders:
        new_rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{t['link']}" style="color:#1a4f8a;font-weight:bold;text-decoration:none;">
              {t['title']}
            </a><br>
            <small style="color:#888;">Matched keyword: <em>{t.get('matched_kw','')}</em></small>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;">{t['ref']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{t['entity']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;">{t['deadline']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <span style="background:#e8f4e8;color:#2d6a2d;padding:2px 8px;border-radius:12px;font-size:12px;">
              {t['status']}
            </span>
          </td>
        </tr>"""

    new_section = ""
    if new_tenders:
        new_section = f"""
        <h3 style="color:#1a4f8a;margin-top:24px;">🔔 New Tenders ({len(new_tenders)})</h3>
        <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
          <tr style="background:#1a4f8a;color:white;text-align:left;">
            <th style="padding:10px 8px;">Title</th>
            <th style="padding:10px 8px;">Ref No.</th>
            <th style="padding:10px 8px;">Procuring Entity</th>
            <th style="padding:10px 8px;">Deadline</th>
            <th style="padding:10px 8px;">Status</th>
          </tr>
          {new_rows}
        </table>"""

    change_rows = ""
    for c in status_changes:
        change_rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{c['link']}" style="color:#1a4f8a;text-decoration:none;">{c['title']}</a>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <span style="color:#888;text-decoration:line-through;">{c['old_status']}</span>
            &nbsp;→&nbsp;
            <span style="color:#c0392b;font-weight:bold;">{c['new_status']}</span>
          </td>
        </tr>"""

    change_section = ""
    if status_changes:
        change_section = f"""
        <h3 style="color:#c0392b;margin-top:24px;">⚡ Status Changes ({len(status_changes)})</h3>
        <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
          <tr style="background:#c0392b;color:white;text-align:left;">
            <th style="padding:10px 8px;">Tender</th>
            <th style="padding:10px 8px;">Status</th>
          </tr>
          {change_rows}
        </table>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
      <div style="background:#1a4f8a;color:white;padding:20px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;">ZPPA Tender Monitor</h2>
        <p style="margin:4px 0 0;opacity:0.8;font-size:13px;">Report generated: {now}</p>
      </div>
      <div style="background:#f9f9f9;padding:20px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;">
        {new_section}
        {change_section}
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <p style="font-size:12px;color:#aaa;">
          Keywords monitored: {', '.join(KEYWORDS)}<br>
          View all tenders: <a href="{OPENED_BIDS_URL}">{OPENED_BIDS_URL}</a>
        </p>
      </div>
    </body></html>
    """


def send_email(subject: str, html_body: str):
    if not all([EMAIL_TO, EMAIL_FROM, EMAIL_PASS]):
        print("[EMAIL] Skipped — credentials not configured.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"[EMAIL] Sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[EMAIL] Failed: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────
# TELEGRAM (optional)
# ─────────────────────────────────────────────

def send_telegram(message: str):
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT]):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.ok:
            print("[TELEGRAM] Message sent.")
        else:
            print(f"[TELEGRAM] Failed: {resp.text}")
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")


def build_telegram_message(new_tenders: list[dict], status_changes: list[dict]) -> str:
    lines = ["<b>ZPPA Tender Alert</b>\n"]
    if new_tenders:
        lines.append(f"<b>🔔 {len(new_tenders)} New Tender(s)</b>")
        for t in new_tenders[:5]:  # cap at 5 to avoid long messages
            lines.append(
                f"• <a href='{t['link']}'>{t['title']}</a>\n"
                f"  {t['entity']} | Due: {t['deadline']} | {t['status']}"
            )
    if status_changes:
        lines.append(f"\n<b>⚡ {len(status_changes)} Status Change(s)</b>")
        for c in status_changes[:5]:
            lines.append(f"• {c['title']}: {c['old_status']} → <b>{c['new_status']}</b>")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  ZPPA Monitor — {now_str}")
    print(f"  Keywords: {', '.join(KEYWORDS)}")
    print(f"  Max pages: {MAX_PAGES}")
    print(f"{'='*55}\n")

    # Load previously seen tenders
    state = load_state()
    print(f"[STATE] {len(state)} tender(s) already tracked.\n")

    # Scrape the portal
    all_tenders = scrape_all(MAX_PAGES)
    print(f"\n[SCRAPE] {len(all_tenders)} total tender(s) fetched.")

    # Filter by keyword
    matching = []
    for t in all_tenders:
        kw = keyword_match(t)
        if kw:
            t["matched_kw"] = kw
            matching.append(t)
    print(f"[FILTER] {len(matching)} tender(s) match keywords.")

    # Find new ones (not previously seen)
    new_tenders = [t for t in matching if t["id"] not in state]
    print(f"[NEW]    {len(new_tenders)} new tender(s) to notify about.")

    # Check for status changes on already-tracked tenders
    status_changes = []
    print(f"\n[STATUS] Checking status of {len(state)} tracked tender(s)...")
    for tid, info in list(state.items()):
        current = get_tender_status(tid)
        old     = info.get("status", "Unknown")
        if current not in ("Unknown", "Error") and current != old:
            print(f"  ⚡ CHANGED: {info['title'][:50]}")
            print(f"     {old} → {current}")
            status_changes.append({
                "title":      info["title"],
                "link":       info.get("link", ""),
                "old_status": old,
                "new_status": current,
            })
            state[tid]["status"] = current
        else:
            print(f"  ✓ {info['title'][:50]} [{current}]")
        time.sleep(0.8)

    # Register new tenders in state
    for t in new_tenders:
        state[t["id"]] = {
            "title":      t["title"],
            "ref":        t["ref"],
            "entity":     t["entity"],
            "deadline":   t["deadline"],
            "method":     t["method"],
            "status":     t["status"],
            "link":       t["link"],
            "matched_kw": t["matched_kw"],
            "first_seen": now_str,
        }
        print(f"\n  [NEW] {t['title']}")
        print(f"        Ref: {t['ref']} | Entity: {t['entity']}")
        print(f"        Deadline: {t['deadline']} | Status: {t['status']}")
        print(f"        Link: {t['link']}")

    # Save updated state (GitHub Actions will commit this file)
    save_state(state)

    # Send notifications if there's anything to report
    if new_tenders or status_changes:
        count = len(new_tenders)
        changes = len(status_changes)
        parts = []
        if count:    parts.append(f"{count} new tender(s)")
        if changes:  parts.append(f"{changes} status change(s)")
        subject = f"[ZPPA Alert] {' & '.join(parts)}"

        html = build_email(new_tenders, status_changes)
        send_email(subject, html)

        tg_msg = build_telegram_message(new_tenders, status_changes)
        send_telegram(tg_msg)
    else:
        print("\n[DONE] No new tenders or status changes. No notification sent.")

    print(f"\n{'='*55}\n")

    # Exit with code 0 always — don't fail the workflow on "no results"
    sys.exit(0)


if __name__ == "__main__":
    main()
