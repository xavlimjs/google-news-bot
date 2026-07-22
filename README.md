# Stock News Bot

Pulls the latest news per stock ticker from Google News RSS and sends new
articles to a Telegram chat. Meant to run on a schedule.

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

## 8. Run it 24/7 with GitHub Actions + cron-job.org (recommended)

This is the "set and forget" setup: GitHub Actions runs the bot, an
external scheduler (cron-job.org) triggers it reliably, and a GitHub Gist
stores the dedup cache so it survives between runs without ever needing
a git commit.

**Why not GitHub's own `schedule:` trigger?** It technically works, but
in practice its timing can be very unreliable — ticks are sometimes
delayed by hours or dropped entirely under load. cron-job.org calling
the workflow directly (via the same mechanism as clicking "Run workflow"
manually) has proven far more consistent.

**Why not commit `seen_articles.json` back to the repo?** That was the
original approach, but it caused a merge conflict almost every time you
tried to push a local change, since the workflow and your local machine
were both racing to update the same file on `main`. Storing it in a
Gist instead means the repo's git history is untouched by the bot.

### 8a. Create the repo

1. On GitHub, create a new repository named `google-news-bot` (public is
   recommended — public repos get unlimited free Actions minutes;
   private repos get 2,000 free minutes/month, far more than this bot
   needs either way)
2. Push this project to it:
   ```bash
   cd google-news-bot
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/google-news-bot.git
   git push -u origin main
   ```

### 8b. Create a Gist for the dedup cache

1. Go to **gist.github.com** → create a **new Gist**
2. Filename: `seen_articles.json`
3. Content: `[]`
4. Choose **secret** or **public** (doesn't matter — it only ever
   contains hashed article IDs, nothing sensitive) → **Create gist**
5. Copy the **Gist ID** from the URL — it's the string of letters/numbers
   after your username, e.g. `gist.github.com/<username>/`**`a1b2c3d4e5f6...`**

### 8c. Create a GitHub token for Gist access

1. Go to **github.com/settings/tokens** → **Tokens (classic)** →
   **Generate new token (classic)**
2. Give it a name, set an expiration you're comfortable with
3. Under scopes, check only **`gist`** — nothing else needed
4. Generate, and copy the token immediately (shown once)

### 8d. Add repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository
secret**. Add all four:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GIST_ID` — from step 8b
- `GIST_TOKEN` — from step 8c

### 8e. Confirm the workflow is set up correctly

`.github/workflows/news-bot.yml` should only have `workflow_dispatch: {}`
as its trigger (no `schedule:` block) — the external scheduler in the
next step handles timing instead.

### 8f. Set up cron-job.org as the scheduler

You'll need a second Personal Access Token for this one — **fine-grained**
this time, scoped to just this repo with **Actions: Read and write**
permission (github.com/settings/tokens → "Fine-grained tokens" →
generate, select the repo, set Actions permission to Read and write).

1. Create a free account at **cron-job.org**
2. Create a new cron job:
   - **URL**: `https://api.github.com/repos/<your-username>/google-news-bot/actions/workflows/news-bot.yml/dispatches`
   - **Request method**: `POST`
   - **Headers**:
     - `Authorization: Bearer <your fine-grained token>`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - **Request body**: `{"ref":"main"}`
   - **Schedule**: minutes 3 and 33 of every hour (avoids the
     `:00`/`:30` marks, which are the most congested times across all of
     GitHub's shared runners)
3. Save and enable the job

### 8g. Verify

- Check cron-job.org's execution history after the next `:03`/`:33` —
  should show a success status
- Check the repo's **Actions** tab — a new run should appear at that
  same time, labeled as triggered via the API rather than "Scheduled"
- Check your Telegram chat for messages

**Notes:**
- If you ever want to pause the bot, disable the workflow from the
  Actions tab — this blocks the API trigger too, so cron-job.org's calls
  will fail harmlessly (visible as an error in its execution history)
  until you re-enable it.
- To change tickers or config, edit `tickers.txt` or `stock_news_bot.py`
  locally and push — no redeploy step needed, the next triggered run
  picks up the change automatically.
- The two tokens (Gist token and Actions-trigger token) have expiration
  dates you set yourself — put a reminder in your calendar to rotate
  them before they lapse, since an expired token fails silently until
  you notice messages have stopped.

## Files

- `stock_news_bot.py` — the bot itself
- `tickers.txt` — list of tickers to track, one per line
- `requirements.txt` — Python dependencies
- `.env.example` — template for local credentials; copy to `.env` and fill in
- `.env` — your actual local credentials (created by you; never committed)
- `.gitignore` — keeps `.env`, `seen_articles.json`, and other runtime
  files out of git
- `.github/workflows/news-bot.yml` — GitHub Actions workflow, triggered
  via `workflow_dispatch` (see "Run it 24/7" above)
- `seen_articles.json` — **local-only fallback** dedup cache, used only
  when `GIST_ID`/`GIST_TOKEN` aren't set (e.g. testing locally). When
  running via GitHub Actions with a Gist configured, this file isn't
  used at all — the cache lives in the Gist instead.