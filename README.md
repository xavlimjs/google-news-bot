# Stock News Bot

Pulls the latest news per stock ticker from Google News RSS, filters by
keyword, and sends new matching articles to a Telegram chat. Meant to run
on a schedule (cron).

## 1. Install dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

(Drop `--break-system-packages` if you're using a virtualenv, which is
recommended.)

## 2. Create a Telegram bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts
3. BotFather gives you a **bot token** — save it

## 3. Get your chat ID

1. Send your new bot any message (e.g. "hi")
2. Visit this URL in your browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id": ...}` in the JSON — that number is your chat ID

## 4. Set credentials

Copy the example env file and fill in your real values:

```bash
cp .env.example .env
```

Then open `.env` and replace the placeholders:

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-telegram-bot-token-here
TELEGRAM_CHAT_ID=123456789
```

The script loads `.env` automatically (via `python-dotenv`) as long as it's
in the same folder. `.env` is listed in `.gitignore`, so it won't get
committed if you push this project to GitHub — only `.env.example`
(with placeholder values) should ever go into version control.

## 5. Configure tickers and lookback window

Open `tickers.txt` and list one ticker per line:

```
AAPL
TSLA
NVDA
```

Lines starting with `#` are treated as comments and ignored, as are blank
lines. You don't need to touch `stock_news_bot.py` to add or remove
tickers — just edit this file.

For the lookback window, open `stock_news_bot.py` and edit the CONFIG
section near the top:

- `LOOKBACK_HOURS` — only articles published within this many hours of
  "now" are considered (default: 24). This is intentionally wider than
  the run interval so a delayed or missed cron run doesn't create gaps —
  the seen-articles cache (see below) makes sure nothing gets sent twice
  even though the window overlaps between runs.
- `FETCH_LIMIT_PER_TICKER` — a safety cap on how many entries to pull per
  feed per run (Google News RSS has no "max results" parameter). The
  real filtering is via `LOOKBACK_HOURS` and the seen-articles cache, so
  you shouldn't need to touch this.
- `MAX_MESSAGE_LENGTH` — Telegram caps messages at 4096 characters; if a
  ticker's consolidated message would exceed this, it's automatically
  split into multiple parts instead of failing to send.

## 6. Test it

```bash
python3 stock_news_bot.py
```

The first run will likely send a burst of articles, since nothing has
been marked "seen" yet. That's expected — it settles down after that.

## 7. Schedule it with cron

Open your crontab:

```bash
crontab -e
```

Add a line to run every 30 minutes (adjust the path and Python
executable as needed). Since credentials now come from `.env` (loaded
automatically by the script), you don't need to inline them in the cron
line — just make sure `.env` sits in the same folder as the script:

```
*/30 * * * * cd /full/path/to/stock_news_bot && /usr/bin/python3 stock_news_bot.py >> stock_news_bot.log 2>&1
```

Or, if you exported the env vars in your shell profile already, you can
omit them from the cron line — but note cron runs with a minimal
environment, so it's often safer to set them inline as shown above.

## 8. Run it 24/7 with GitHub Actions (recommended for "set and forget")

Instead of your own machine's cron, you can let GitHub run this on a
schedule for free. Steps:

1. **Create the repo.** On GitHub, create a new repository named
   `google-news-bot` (public is recommended — public repos get unlimited
   free Actions minutes; private repos get 2,000 free minutes/month,
   which is far more than this bot needs either way).

2. **Push this project to it:**
   ```bash
   cd google-news-bot   # the folder containing these files
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/google-news-bot.git
   git push -u origin main
   ```
   `.env` will NOT be pushed (it's in `.gitignore`) — good, since Actions
   uses GitHub Secrets instead (next step).

3. **Add your credentials as repo secrets**, not a `.env` file:
   - Go to the repo's **Settings → Secrets and variables → Actions**
   - Click **New repository secret**, add:
     - `TELEGRAM_BOT_TOKEN`
     - `TELEGRAM_CHAT_ID`

4. **That's it.** The workflow at
   `.github/workflows/news-bot.yml` is already set up to:
   - Run every 30 minutes (`schedule: cron: "*/30 * * * *"`)
   - Also let you trigger it manually anytime from the repo's **Actions**
     tab (`workflow_dispatch`)
   - Install dependencies, run the bot, and commit the updated
     `seen_articles.json` back to the repo so dedup state survives
     between runs (each Actions run starts from a clean, throwaway
     machine — nothing else persists automatically)

5. **Check it worked:** go to the **Actions** tab in your repo — you
   should see "Stock News Bot" runs appearing every 30 minutes, and a
   small automated commit updating `seen_articles.json` after each run
   that finds new articles.

**Notes:**
- GitHub's scheduled triggers aren't guaranteed to fire at the exact
  minute — under load they can be a few minutes late. Harmless here.
- If you ever want to pause it, disable the workflow from the Actions
  tab rather than deleting the file.
- To change tickers or config, just edit `tickers.txt` or
  `stock_news_bot.py` and push — no redeploy step needed, the next
  scheduled run picks up the change automatically.

## Files

- `stock_news_bot.py` — the bot itself
- `tickers.txt` — list of tickers to track, one per line
- `requirements.txt` — Python dependencies
- `.env.example` — template for credentials; copy to `.env` and fill in
- `.env` — your actual credentials (created by you; never committed)
- `.gitignore` — keeps `.env` and runtime files out of git
- `.github/workflows/news-bot.yml` — GitHub Actions workflow that runs
  the bot every 30 minutes (see "Run it 24/7 with GitHub Actions" above)
- `seen_articles.json` — tracks which articles have already been sent.
  Created empty; updated automatically each run (and committed back by
  the GitHub Actions workflow if you use that route). Safe to delete/
  empty (`[]`) if you want a fresh start, but you'll get a burst of "new"
  articles again.
