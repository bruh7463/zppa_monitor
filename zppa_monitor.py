"""
ZPPA Tender Monitor — GitHub Actions Edition
==============================================
Designed to run as a single-shot script on a GitHub Actions schedule.
Uses the correct ZPPA advanced search endpoint (viewCFTSAction.do) with
UNSPSC code 76000000 (Industrial Cleaning Services). Falls back to keyword
scanning of the opened-bids listing if UNSPSC returns nothing.

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
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# UNSPSC code + portal label (must match exactly what ZPPA uses).
# 76000000 = Industrial Cleaning Services (confirmed working on portal).
UNSPSC_CODE  = "76000000"
UNSPSC_LABEL = "Industrial Cleaning Services"

# Keyword validation — checked against tender TITLE ONLY (not ref/entity).
# Used as secondary filter on UNSPSC results and primary filter for fallback.
KEYWORDS = [
    "cleaning",
    "janitorial",
    "sanitation",
    "hygiene",
    "housekeeping",
    "pest control",
    "waste management",
    "facilities management",
    "cleaning services",
    "general maintenance",
    "waste collection",
    "refuse removal",
    "refuse collection",
    "garbage collection",
    "sweeping",
    "fumigation",
    "disinfection",
    "cleansing",
    "launder",
    "laundry",
    "waste disposal",
    "solid waste",
    "liquid waste",
    "sewage",
    "drain cleaning",
    "window cleaning",
    "carpet cleaning",
    "floor cleaning",
    "tank cleaning",
    "street cleaning",
    "debris removal",
    "rubbish removal",
    "portable toilet",
    "chemical toilet",
    "ablution cleaning",
    "restroom cleaning",
    "toilet cleaning",
    "office cleaning",
    "school cleaning",
    "hospital cleaning",
    "clinic cleaning",
    "market cleaning",
    "compound cleaning",
    "site cleaning",
    "post-construction cleaning",
    "deep cleaning",
    "industrial cleaning",
    "commercial cleaning",
    "institutional cleaning",
    "residential cleaning",
    "domestic cleaning",
    "upholstery cleaning",
    "pressure washing",
    "graffiti removal",
    "mould removal",
    "sanitary services",
    "environmental cleaning",
    "grounds maintenance",
    "weed control",
    "vermin control",
    "rodent control",
    "insect control",
    "vector control",
    "decontamination",
    "biohazard cleaning",
    "steam cleaning",
    "chemical cleaning",
    "oil spill",
    "hazardous material cleanup",
    "asbestos removal",
    "detergent",
    "sanitizer",
    "janitor",
    "cleaner",
    "custodial",
    "caretaker",
    "caretaking",
    "car wash",
    "vehicle wash",
    "fleet cleaning",
    "duct cleaning",
    "ventilation cleaning",
    "chimney cleaning",
    "sewer cleaning",
    "drainage cleaning",
    "reservoir cleaning",
    "silo cleaning",
    "spillage cleaning",
    "flood cleanup",
    "fire damage restoration",
    "smoke damage cleaning",
    "soot cleaning",
    "sandblasting",
    "dry ice cleaning",
    "ultrasonic cleaning",
]

# How many pages to scan per UNSPSC code per run
MAX_PAGES_UNSPSC = 20

# How many pages to scan for keyword fallback
MAX_PAGES_FALLBACK = 50

# Only tenders with this status will trigger a NEW alert.
ACTIONABLE_STATUSES = {"bid submission"}

# Tenders with fewer than this many days until deadline are skipped.
MIN_DAYS_BEFORE_DEADLINE = 7

# JSON file that tracks which tenders have already triggered an alert.
STATE_FILE = "zppa_seen_tenders.json"

# Network settings
REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3
RETRY_BACKOFF   = 5

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

BASE_URL              = "https://eprocure.zppa.org.zm"
OPENED_BIDS_URL       = f"{BASE_URL}/epps/common/viewOpenedTenders.do"
ADVANCED_SEARCH_URL   = f"{BASE_URL}/epps/viewCFTSAction.do"
TENDER_URL            = f"{BASE_URL}/epps/cft/prepareViewCfTWS.do"

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
# NETWORK HELPER
# ─────────────────────────────────────────────

def resilient_get(url: str, params=None) -> requests.Response | None:
    """GET with retries and exponential backoff. Params can be dict or list of tuples."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            print(f"  [RETRY {attempt}/{MAX_RETRIES}] {e}")
            print(f"  Waiting {wait}s before retry...")
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    return None


# ─────────────────────────────────────────────
# DEADLINE HELPERS
# ─────────────────────────────────────────────

def parse_deadline(deadline_str: str) -> datetime | None:
    formats = [
        "%a %b %d %H:%M:%S CAT %Y",
        "%a %b %d %H:%M:%S %Z %Y",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    clean = deadline_str.strip()
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def has_enough_time(deadline_str: str) -> bool:
    dt = parse_deadline(deadline_str)
    if dt is None:
        return True  # unknown — include, let user decide
    return dt >= datetime.now() + timedelta(days=MIN_DAYS_BEFORE_DEADLINE)


def days_remaining(deadline_str: str) -> str:
    dt = parse_deadline(deadline_str)
    if dt is None:
        return "unknown deadline"
    delta = dt - datetime.now()
    days = delta.days
    if days < 0:
        return "PAST deadline"
    if days == 0:
        return "due TODAY"
    return f"{days}d remaining"


# ─────────────────────────────────────────────
# KEYWORD MATCHING — TITLE ONLY
# ─────────────────────────────────────────────

def keyword_match(title: str) -> str | None:
    """
    Return the matched keyword, or None.
    Only checks the tender TITLE — NOT ref or entity.
    Prevents false positives like 'Ministry of Water Development
    and Sanitation' matching a bicycle tender.
    """
    title_lower = title.lower()
    for kw in KEYWORDS:
        if kw.lower() in title_lower:
            return kw
    return None


# ─────────────────────────────────────────────
# SCRAPING — CORRECT ADVANCED SEARCH ENDPOINT
# ─────────────────────────────────────────────

def build_advanced_search_params(page: int = 1) -> list[tuple]:
    """
    Build the exact query parameters the ZPPA advanced search form sends.
    Uses list of tuples because the portal expects duplicate keys
    (e.g. cpcCategory= appears twice, description= appears twice).

    Mirrors:
    viewCFTSAction.do?cpcCategory=&cpcCategory=0&isFTS=true&...
      &unspscArray=76000000-Industrial+Cleaning+Services
      &unspscLabels=76000000&...
      &d-3680175-p=1
    """
    return [
        ("cpcCategory",           ""),
        ("cpcCategory",           "0"),
        ("isFTS",                 "true"),
        ("popupMode",             ""),
        ("d-3680175-p",           str(page)),
        ("uniqueId",              ""),
        ("mode",                  "search"),
        ("publicationFromDate",   ""),
        ("title",                 ""),
        ("description",           ""),
        ("description",           ""),
        ("contractType",          ""),
        ("contractType",          ""),
        ("estimatedValueMin",     ""),
        ("submissionUntilDate",   ""),
        ("unspscArray",           f"{UNSPSC_CODE}-{UNSPSC_LABEL}"),
        ("tenderOpeningUntilDate", ""),
        ("isPopup",               "false"),
        ("procedure",             ""),
        ("procedure",             ""),
        ("tenderOpeningFromDate", ""),
        ("status",                ""),
        ("status",                ""),
        ("unspscLabels",          UNSPSC_CODE),
        ("publicationUntilDate",  ""),
        ("UNSPSCCodes",           ""),
        ("submissionFromDate",    ""),
        ("estimatedValueMax",     ""),
        ("contractAuthority",     ""),
    ]


def fetch_advanced_page(page: int) -> BeautifulSoup | None:
    """Fetch one page of the ZPPA advanced search for our UNSPSC code."""
    params = build_advanced_search_params(page)
    resp = resilient_get(ADVANCED_SEARCH_URL, params=params)
    if resp is None:
        print(f"[ERROR] Advanced search page {page}: all retries failed")
        return None
    return BeautifulSoup(resp.text, "html.parser")


def fetch_opened_bids_page(page: int) -> BeautifulSoup | None:
    """Fallback: fetch a page of the general opened-bids listing."""
    params = {"d-3680181-p": page, "d-3680181-n": 1}
    resp = resilient_get(OPENED_BIDS_URL, params=params)
    if resp is None:
        print(f"[ERROR] Opened bids page {page}: all retries failed")
        return None
    return BeautifulSoup(resp.text, "html.parser")


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


def scrape_unspsc() -> list[dict]:
    """Search UNSPSC code via viewCFTSAction.do. Deduplicate results."""
    all_tenders: dict[str, dict] = {}
    print(f"[UNSPSC] Searching code {UNSPSC_CODE} ({UNSPSC_LABEL})...")
    for page in range(1, MAX_PAGES_UNSPSC + 1):
        print(f"  Page {page}/{MAX_PAGES_UNSPSC}...")
        soup = fetch_advanced_page(page)
        if not soup:
            break
        rows = parse_tenders(soup)
        if not rows:
            print(f"  No rows on page {page}, stopping.")
            break
        for t in rows:
            if t["id"] not in all_tenders:
                t["unspsc_code"]  = UNSPSC_CODE
                t["unspsc_label"] = UNSPSC_LABEL
                t["matched_kw"]   = keyword_match(t["title"])
                all_tenders[t["id"]] = t
        time.sleep(2)
    print(f"[UNSPSC] {len(all_tenders)} result(s).")
    return list(all_tenders.values())


def scrape_keyword_fallback() -> list[dict]:
    """Fallback: scan opened-bids listing, filter by keywords (title only)."""
    print("[FALLBACK] Keyword scan of opened-bids listing...")
    all_tenders = []
    for page in range(1, MAX_PAGES_FALLBACK + 1):
        print(f"  Page {page}/{MAX_PAGES_FALLBACK}...")
        soup = fetch_opened_bids_page(page)
        if not soup:
            break
        rows = parse_tenders(soup)
        if not rows:
            print(f"  No rows on page {page}, stopping.")
            break
        all_tenders.extend(rows)
        time.sleep(2)

    matched = []
    for t in all_tenders:
        kw = keyword_match(t["title"])
        if kw:
            t["matched_kw"]    = kw
            t["unspsc_code"]   = ""
            t["unspsc_label"]  = ""
            matched.append(t)
    print(f"[FALLBACK] {len(matched)} keyword match(es) from {len(all_tenders)} total.")
    return matched


# ─────────────────────────────────────────────
# STATUS CHECK
# ─────────────────────────────────────────────

STATUS_WORDS = [
    "Bid Submission",
    "Awaiting Bid Opening",
    "Under Evaluation",
    "Approval",
    "Notice of Award",
    "Awarded",
    "Cancelled",
    "Established",
    "Suspended",
    "Closed",
]


def get_tender_status(resource_id: str) -> str:
    """Re-fetch a tender page and extract its current status."""
    if not resource_id.isdigit():
        return "Unknown"
    resp = resilient_get(TENDER_URL, params={"resourceId": resource_id})
    if resp is None:
        return "Unknown"
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text()
        for word in STATUS_WORDS:
            if word.lower() in text.lower():
                return word
        return "Unknown"
    except Exception:
        return "Unknown"


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def build_email(new_tenders: list[dict], status_changes: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    new_rows = ""
    for t in new_tenders:
        dr = days_remaining(t["deadline"])
        kw = t.get("matched_kw", "")
        unspsc_info = f"UNSPSC {t.get('unspsc_code', '')} ({t.get('unspsc_label', '')})" if t.get("unspsc_code") else "Keyword match"
        new_rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{t['link']}" style="color:#1a4f8a;font-weight:bold;text-decoration:none;">
              {t['title']}
            </a><br>
            <small style="color:#888;">{unspsc_info}</small><br>
            <small style="color:#6a6a2d;">Keyword: <em>{kw}</em></small>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;">{t['ref']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{t['entity']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;">
            {t['deadline']}<br>
            <span style="background:#e8f4e8;color:#2d6a2d;padding:2px 6px;border-radius:10px;font-size:11px;">
              {dr}
            </span>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <span style="background:#e8f4e8;color:#2d6a2d;padding:2px 8px;border-radius:12px;font-size:12px;">
              {t['status']}
            </span>
          </td>
        </tr>"""

    new_section = ""
    if new_tenders:
        new_section = f"""
        <h3 style="color:#1a4f8a;margin-top:24px;">🔔 New Cleaning Tenders ({len(new_tenders)})</h3>
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
        <h2 style="margin:0;">ZPPA Tender Monitor — Cleaning Services</h2>
        <p style="margin:4px 0 0;opacity:0.8;font-size:13px;">Report generated: {now}</p>
      </div>
      <div style="background:#f9f9f9;padding:20px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;">
        {new_section}
        {change_section}
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <p style="font-size:12px;color:#aaa;">
          UNSPSC: {UNSPSC_CODE} ({UNSPSC_LABEL})<br>
          Endpoint: viewCFTSAction.do (advanced search)<br>
          Keyword validation: {len(KEYWORDS)} terms (title only)<br>
          Only showing tenders in <strong>Bid Submission</strong> status
          with &ge; {MIN_DAYS_BEFORE_DEADLINE} days until deadline.<br>
          View all: <a href="{OPENED_BIDS_URL}">{OPENED_BIDS_URL}</a>
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

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
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
    lines = ["<b>ZPPA Cleaning Tender Alert</b>\n"]
    if new_tenders:
        lines.append(f"<b>🔔 {len(new_tenders)} New Cleaning Tender(s)</b>")
        for t in new_tenders[:5]:
            kw = t.get("matched_kw", "")
            lines.append(
                f"• <a href='{t['link']}'>{t['title']}</a>\n"
                f"  {t['entity']} | Due: {t['deadline']} | {t['status']}\n"
                f"  Keyword: {kw}"
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
    print(f"  UNSPSC      : {UNSPSC_CODE} ({UNSPSC_LABEL})")
    print(f"  Endpoint    : viewCFTSAction.do")
    print(f"  Keywords    : {len(KEYWORDS)} terms (title only)")
    print(f"  Actionable  : {', '.join(a.title() for a in ACTIONABLE_STATUSES)}")
    print(f"  Min deadline: {MIN_DAYS_BEFORE_DEADLINE} days")
    print(f"{'='*55}\n")

    # Load previously seen tenders
    state = load_state()
    print(f"[STATE] {len(state)} tender(s) already tracked.\n")

    # ── 1. UNSPSC advanced search ────────────────────────────────
    candidates = scrape_unspsc()

    # ── 2. Keyword fallback if UNSPSC returns nothing ────────────
    if not candidates:
        candidates = scrape_keyword_fallback()

    print(f"\n[TOTAL] {len(candidates)} candidate(s) found.")

    # ── 3. Filter by status + deadline ───────────────────────────
    actionable = []
    skipped_status   = 0
    skipped_deadline = 0
    for t in candidates:
        if t["status"].strip().lower() not in ACTIONABLE_STATUSES:
            skipped_status += 1
            continue
        if not has_enough_time(t["deadline"]):
            skipped_deadline += 1
            continue
        actionable.append(t)

    print(f"[FILTER] Skipped {skipped_status} (not 'Bid Submission'), "
          f"{skipped_deadline} (< {MIN_DAYS_BEFORE_DEADLINE} days).")
    print(f"[FILTER] {len(actionable)} actionable tender(s).")

    # ── 4. Find new ones ─────────────────────────────────────────
    new_tenders = [t for t in actionable if t["id"] not in state]
    print(f"[NEW]    {len(new_tenders)} new tender(s) to notify about.")

    # ── 5. Check status changes on tracked tenders ───────────────
    status_changes = []
    if state:
        print(f"\n[STATUS] Checking status of {len(state)} tracked tender(s)...")
        for tid, info in list(state.items()):
            current = get_tender_status(tid)
            old     = info.get("status", "Unknown")
            if current not in ("Unknown", "") and current != old:
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
            time.sleep(1)

    # ── 6. Register new tenders in state ─────────────────────────
    for t in new_tenders:
        state[t["id"]] = {
            "title":       t["title"],
            "ref":         t["ref"],
            "entity":      t["entity"],
            "deadline":    t["deadline"],
            "method":      t["method"],
            "status":      t["status"],
            "link":        t["link"],
            "unspsc_code": t.get("unspsc_code", ""),
            "unspsc_label": t.get("unspsc_label", ""),
            "matched_kw":  t.get("matched_kw", ""),
            "first_seen":  now_str,
        }
        dr = days_remaining(t["deadline"])
        print(f"\n  [NEW] {t['title']}")
        print(f"        UNSPSC: {t.get('unspsc_code', 'N/A')} | Keyword: \"{t.get('matched_kw', '')}\"")
        print(f"        Ref: {t['ref']} | Entity: {t['entity']}")
        print(f"        Deadline: {t['deadline']} ({dr}) | Status: {t['status']}")
        print(f"        Link: {t['link']}")

    # ── 7. Save state ────────────────────────────────────────────
    save_state(state)

    # ── 8. Notify ────────────────────────────────────────────────
    if new_tenders or status_changes:
        parts = []
        if new_tenders:     parts.append(f"{len(new_tenders)} new tender(s)")
        if status_changes:  parts.append(f"{len(status_changes)} status change(s)")
        subject = f"[ZPPA Cleaning Alert] {' & '.join(parts)}"

        html = build_email(new_tenders, status_changes)
        send_email(subject, html)

        tg_msg = build_telegram_message(new_tenders, status_changes)
        send_telegram(tg_msg)
    else:
        print("\n[DONE] No new cleaning tenders or status changes. No notification sent.")

    print(f"\n{'='*55}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
