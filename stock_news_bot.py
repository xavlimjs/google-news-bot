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
  3. Filters out articles already seen (tracked in seen_articles.json)
  4. Groups any new articles by ticker and sends ONE consolidated Telegram
     message per ticker (title + URL for each article)
  5. Updates the seen-articles cache

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

# Where dedup state is stored (created automatically)
SEEN_FILE = Path(__file__).parent / "seen_articles.json"

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


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set):
    # Keep the file from growing forever — cap at the most recent 5000 ids
    trimmed = list(seen)[-5000:]
    SEEN_FILE.write_text(json.dumps(trimmed))


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
    new_seen = set(seen)
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

            new_seen.add(aid)
            new_entries_for_ticker.append(entry)

        if new_entries_for_ticker:
            messages = format_ticker_messages(ticker, new_entries_for_ticker)
            for message in messages:
                send_telegram_message(message)
                time.sleep(1)  # be gentle on Telegram's rate limits
            total_sent += len(new_entries_for_ticker)
            tickers_with_news += 1

    save_seen(new_seen)
    print(
        f"Done. Sent {total_sent} new article(s) across {tickers_with_news} ticker "
        f"message(s); {total_out_of_window} outside the {LOOKBACK_HOURS}h lookback window."
    )


if __name__ == "__main__":
    main()
