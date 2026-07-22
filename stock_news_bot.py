#!/usr/bin/env python3
"""
Stock News Bot
--------------
Pulls the latest news for a list of stock tickers from Google News RSS
(no scraping/CAPTCHA issues, since this is a public, sanctioned feed)
and sends new articles to a Telegram chat.

Designed to be run on a schedule (cron), e.g. every 30 minutes. Each run:
  1. Fetches the RSS feed for each ticker
  2. Keeps only articles published within the lookback window (default: 1 day)
  3. Filters out articles already seen (tracked via a GitHub Gist, or a
     local seen_articles.json file as a fallback)
  4. Groups any new articles by ticker and sends ONE consolidated Telegram
     message per ticker (title + URL for each article). If a ticker has
     no new articles, sends a short "no new articles" notice instead.
  5. Updates the seen-articles cache, auto-pruning any entries older
     than PRUNE_AFTER_DAYS so it never needs manual clearing

Tickers to track are read from tickers.txt (one per line) — see that file.

See README.md for setup instructions.
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env in the same directory, if present

# ---------------------------------------------------------------------------
# CONFIG — edit this section
# ---------------------------------------------------------------------------

# Tickers are read from tickers.txt (one per line) — see that file to add
# or remove tickers. TICKERS_FILE points to it; no need to edit this script
# just to change which tickers are tracked.
TICKERS_FILE = Path(__file__).parent / "tickers.txt"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Only consider articles published within this many hours of "now".
# Meant to be run every 30 min via cron; this window is intentionally wider
# than the run interval so a missed/delayed run doesn't cause gaps.
LOOKBACK_HOURS = 24

# Safety cap on how many entries to pull per feed per run (Google News RSS
# doesn't take a "max results" param). This just bounds worst-case work;
# the real filtering happens via LOOKBACK_HOURS and the seen-articles cache.
FETCH_LIMIT_PER_TICKER = 50

# Telegram caps messages at 4096 characters. If a ticker's consolidated
# message would exceed this, it gets split into multiple parts instead of
# failing to send. This is set with a safety margin below the hard limit.
MAX_MESSAGE_LENGTH = 4000

# Article IDs are auto-pruned from the dedup cache once they're older than
# this many days. Keeps the cache small indefinitely without ever needing
# to clear it by hand. Should stay comfortably larger than LOOKBACK_HOURS
# (converted to days) so nothing gets pruned before it's even had a chance
# to be deduped against.
PRUNE_AFTER_DAYS = 3

# Where dedup state is stored locally (only used as a fallback — see
# GIST_ID/GIST_TOKEN below for the primary storage method)
SEEN_FILE = Path(__file__).parent / "seen_articles.json"

# --- Dedup cache storage: GitHub Gist (recommended for GitHub Actions) -----
# If both of these are set, seen-article IDs are read/written to a GitHub
# Gist instead of a local file. This avoids the local file ever needing to
# be committed back to the repo, which is what caused repeated merge
# conflicts when running via GitHub Actions.
# - GIST_ID: the ID of a Gist you create yourself, containing a file named
#   exactly "seen_articles.json" with initial content "[]"
# - GIST_TOKEN: a GitHub Personal Access Token (classic) with only the
#   "gist" scope
# If either is unset, the bot falls back to the local SEEN_FILE above —
# useful for local testing without needing Gist credentials.
GIST_ID = os.environ.get("GIST_ID", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_FILENAME = "seen_articles.json"

# ---------------------------------------------------------------------------
# CORE LOGIC — shouldn't need to touch this
# ---------------------------------------------------------------------------


def load_tickers() -> list:
    """Reads tickers.txt: one ticker per line, blank lines and lines
    starting with # are ignored. Returns tickers uppercased, in file order,
    with duplicates removed."""
    if not TICKERS_FILE.exists():
        print(f"[ERROR] Tickers file not found: {TICKERS_FILE}")
        return []

    tickers = []
    seen_tickers = set()
    for raw_line in TICKERS_FILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ticker = line.upper()
        if ticker not in seen_tickers:
            seen_tickers.add(ticker)
            tickers.append(ticker)
    return tickers


def google_news_rss_url(ticker: str) -> str:
    query = quote_plus(f"{ticker} stock")
    return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def article_id(entry) -> str:
    """Stable hash for dedup, based on link (falls back to title)."""
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _gist_headers() -> dict:
    return {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _prune_seen(seen: dict) -> dict:
    """Drops any article ID whose recorded timestamp is older than
    PRUNE_AFTER_DAYS. Keeps the cache size bounded automatically, based on
    age rather than a raw count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    pruned = {}
    for aid, ts_str in seen.items():
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            # Malformed/unknown timestamp — keep it rather than risk
            # dropping something that should still be deduped.
            pruned[aid] = ts_str
            continue
        if ts >= cutoff:
            pruned[aid] = ts_str
    return pruned


def _normalize_seen(raw) -> dict:
    """Accepts either the current dict format ({id: timestamp}) or the
    older flat-list format (["id1", "id2", ...]) and returns a dict.
    Legacy list entries get "now" as their timestamp, since we don't know
    when they were actually first seen — they'll age out normally from
    here on."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        now_iso = datetime.now(timezone.utc).isoformat()
        return {aid: now_iso for aid in raw}
    return {}


def load_seen() -> dict:
    if GIST_ID and GIST_TOKEN:
        try:
            resp = requests.get(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            gist_data = resp.json()
            content = gist_data["files"][GIST_FILENAME]["content"]
            return _normalize_seen(json.loads(content))
        except Exception as e:
            print(f"[ERROR] Failed to load seen-articles from Gist: {e}")
            return {}

    # Local file fallback (used when GIST_ID/GIST_TOKEN aren't set)
    if SEEN_FILE.exists():
        try:
            return _normalize_seen(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_seen(seen: dict):
    pruned = _prune_seen(seen)
    content = json.dumps(pruned)

    if GIST_ID and GIST_TOKEN:
        try:
            payload = {"files": {GIST_FILENAME: {"content": content}}}
            resp = requests.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"[ERROR] Failed to save seen-articles to Gist: {e}")
        return

    # Local file fallback
    SEEN_FILE.write_text(content)


def fetch_ticker_news(ticker: str):
    url = google_news_rss_url(ticker)
    feed = feedparser.parse(url)
    return feed.entries[:FETCH_LIMIT_PER_TICKER]


def is_within_lookback(entry) -> bool:
    """True if the entry's published date is within LOOKBACK_HOURS of now.
    If no parseable published date exists, the entry is kept (better to
    show it than silently drop it)."""
    struct = entry.get("published_parsed")
    if not struct:
        return True
    published_dt = datetime(*struct[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return published_dt >= cutoff


def format_ticker_messages(ticker: str, entries: list) -> list:
    """Builds one or more consolidated messages for a ticker, listing each
    new article's title and URL. Splits into multiple parts if the combined
    text would exceed Telegram's message length limit."""
    total = len(entries)
    blocks = []
    for entry in entries:
        title = entry.get("title", "No title")
        link = entry.get("link", "")
        blocks.append(f"{title}\n{link}")

    # Group blocks into chunks that stay under MAX_MESSAGE_LENGTH, leaving
    # room for the header line added below.
    chunks = []
    current_blocks = []
    current_len = 0
    for block in blocks:
        block_len = len(block) + 2  # + blank line separator
        if current_blocks and current_len + block_len > MAX_MESSAGE_LENGTH:
            chunks.append(current_blocks)
            current_blocks = []
            current_len = 0
        current_blocks.append(block)
        current_len += block_len
    if current_blocks:
        chunks.append(current_blocks)

    total_parts = len(chunks)
    messages = []
    for i, chunk_blocks in enumerate(chunks, start=1):
        if total_parts > 1:
            header = f"📈 *{ticker}* — {total} new article(s) (part {i}/{total_parts})"
        else:
            header = f"📈 *{ticker}* — {total} new article(s)"
        body = "\n\n".join(chunk_blocks)
        messages.append(f"{header}\n\n{body}")

    return messages


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials not set — printing instead:\n", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code != 200:
            print(f"[ERROR] Telegram send failed ({resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"[ERROR] Telegram request failed: {e}")


def main():
    tickers = load_tickers()
    if not tickers:
        print("[ERROR] No tickers loaded — check tickers.txt. Exiting.")
        return

    seen = load_seen()
    new_seen = dict(seen)
    now_iso = datetime.now(timezone.utc).isoformat()
    total_sent = 0
    total_out_of_window = 0
    tickers_with_news = 0

    for ticker in tickers:
        try:
            entries = fetch_ticker_news(ticker)
        except Exception as e:
            print(f"[ERROR] Failed to fetch news for {ticker}: {e}")
            continue

        new_entries_for_ticker = []

        for entry in entries:
            if not is_within_lookback(entry):
                total_out_of_window += 1
                continue

            aid = article_id(entry)
            if aid in seen:
                continue

            new_seen[aid] = now_iso
            new_entries_for_ticker.append(entry)

        if new_entries_for_ticker:
            messages = format_ticker_messages(ticker, new_entries_for_ticker)
            for message in messages:
                send_telegram_message(message)
                time.sleep(1)  # be gentle on Telegram's rate limits
            total_sent += len(new_entries_for_ticker)
            tickers_with_news += 1
        else:
            send_telegram_message(f"📭 *{ticker}* — no new articles")
            time.sleep(1)  # be gentle on Telegram's rate limits

    save_seen(new_seen)
    print(
        f"Done. Sent {total_sent} new article(s) across {tickers_with_news} ticker "
        f"message(s); {total_out_of_window} outside the {LOOKBACK_HOURS}h lookback window."
    )


if __name__ == "__main__":
    main()