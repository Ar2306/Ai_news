# AI Daily Newsletter

[![Tests](https://github.com/DavidGaso1/AI-News-Update/actions/workflows/tests.yml/badge.svg)](https://github.com/DavidGaso1/AI-News-Update/actions/workflows/tests.yml)

An automated AI newsletter that fetches, deduplicates, summarizes, and categorizes AI news from multiple sources daily — then delivers a morning briefing to your inbox and Telegram.

🌐 **Live site:** [ainl.vercel.app](https://ainl.vercel.app) — public, updated every morning at 6 AM UTC.

## Features

- **Automated daily fetching** at 6 AM UTC via GitHub Actions cron
- **Multiple sources**: RSS feeds, Reddit, Hacker News, Twitter/X (via Nitter)
- **Aggressive deduplication**: retweet/quote-RT filtering, cross-source fuzzy title matching, URL normalization (strips tracking params like `utm_*`, `s=`, `t=`), and **cross-day dedup** against the committed archive
- **Morning digest channels**:
  - 📧 **Email** via Resend (beautiful HTML + plain-text fallback, multi-recipient support)
  - ✈️ **Telegram** via bot (compact HTML message)
- **LLM-powered summarization** using Hermes Agent (falls back to extractive)
- **Smart categorization**: Breaking News, Research Papers, Industry Updates, Tools & Releases
- **Modern, responsive web UI** with dark/light theme toggle
- **Static site** — no backend needed, auto-deployed to [Vercel](https://ainl.vercel.app) on every push

## Project Structure

```
ai-newsletter/
├── index.html              # Main newsletter page
├── data/
│   ├── newsletter.json     # Generated newsletter data
│   ├── archive.json        # Daily archive index
│   └── newsletter.db       # SQLite history (gitignored)
├── scripts/
│   ├── fetch-news.py       # Main fetch & summarization script
│   ├── send_digest.py      # Morning digest sender (email + Telegram)
│   └── run-fetch.sh        # Cron wrapper script
├── .github/workflows/
│   ├── daily-fetch.yml     # 6 AM fetch + commit + send digest
│   └── tests.yml           # CI: compile, data validation, dedup checks
├── logs/                   # Fetch logs (gitignored)
├── requirements.txt        # Python dependencies
└── README.md              # This file
```

## Installation

```bash
cd AI-News-Update
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Manual fetch
```bash
python3 scripts/fetch-news.py
```

### View newsletter
- **Live:** [ainl.vercel.app](https://ainl.vercel.app) (public, auto-updated by the 6 AM cron)
- **Locally:** open `index.html` in a browser, or serve it:
```bash
python3 -m http.server 8080
# Then open http://localhost:8080
```

## Daily digest delivery

The `daily-fetch.yml` workflow fetches news at **6:00 UTC**, commits the fresh data, then runs `scripts/send_digest.py` to deliver the morning briefing to every configured channel. Channels are independent — a channel with missing credentials is skipped, so the workflow stays green while you're still setting one up.

### 1. Email (Resend)

1. Create an API key at [resend.com](https://resend.com/api-keys) (free tier included).
2. Verify your sending domain (or use Resend's shared `onboarding@resend.dev` sandbox for testing).
3. Add these GitHub secrets to the repo:
   - `RESEND_API_KEY`
   - `NEWSLETTER_FROM` — e.g. `AI News <news@yourdomain.com>`
   - `NEWSLETTER_TO` — the main recipient, e.g. `you@example.com`
   - `NEWSLETTER_SUBSCRIBERS` *(optional)* — comma-separated extra recipients, e.g. `a@x.com,b@y.com`. Everyone on this list gets the same morning briefing.

### 2. Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Get your chat ID (send a message to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`).
3. Add these GitHub secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

### Test delivery locally
```bash
# Build both digests and print them without sending anything:
python3 scripts/send_digest.py --dry-run

# Send only to Telegram (handy while configuring email):
python3 scripts/send_digest.py --telegram-only
```

### All environment variables

| Variable | Purpose |
|---|---|
| `RESEND_API_KEY` | Resend API key for email channel |
| `NEWSLETTER_FROM` | Sender address shown in the email |
| `NEWSLETTER_TO` | Main email recipient of the digest |
| `NEWSLETTER_SUBSCRIBERS` | *(optional)* Comma-separated extra recipients who also get the digest |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Chat/user ID to receive the Telegram digest |
| `DEDUP_ARCHIVE_DAYS` | Cross-day dedup window in days (default 14) |
| `ARCHIVE_RETENTION_DAYS` | How much archive history is kept (default 180) |
| `HERMES_API_URL` | LLM API endpoint (default: localhost:8080/v1) |
| `HERMES_MODEL` | Model name (default: nemotron-3-ultra-free) |
| `HERMES_PROVIDER` | Provider (default: opencode-zen) |

## Redundancy control

The fetcher applies **four** layers of deduplication, so the same story told by multiple accounts, sources, or days appears once:

1. **Retweet filtering** — entries starting with `RT by @`, `RT @`, or `R to @` are skipped outright (the biggest source of noise).
2. **Fuzzy title matching** — normalized titles (retweet prefixes, URLs, punctuation stripped) are compared across all sources with a `SequenceMatcher` similarity threshold of 0.88.
3. **URL normalization** — tracking params (`utm_*`, `ref`, `source`, `s=`, `t=`, etc.) and trailing slashes are removed before comparing URLs.
4. **Cross-day dedup** — before fetching, the fetcher loads the committed `archive.json` (last `DEDUP_ARCHIVE_DAYS`, default 14) and seeds its seen-URLs/titles sets, so a story already covered yesterday won't reappear in today's digest. The archive accumulates history across runs with a 180-day retention cap (`ARCHIVE_RETENTION_DAYS`).

These behaviors are covered by unit checks in `tests.yml`, so a regression fails CI.

## Deployment

The site is a static page served by Vercel and auto-deployed on every push to `master` (via the GitHub integration in `vercel.json`). `data/newsletter.json` and `data/archive.json` are committed by the daily cron, so each deploy publishes the freshest news.

- **Live URL:** `ainl.vercel.app`
- **Archive page:** `ainl.vercel.app/archive.html`
- The site is fully public — no login or SSO required.

## Data Sources

### RSS Feeds
arXiv (AI/ML/CV/CL), Hugging Face, OpenAI, Anthropic, Google AI, Microsoft Research, Meta AI, NVIDIA, Google DeepMind, Cohere, LangChain, Weights & Biases, AssemblyAI, Replicate, Together AI blogs.

### Reddit
r/MachineLearning, r/ArtificialIntelligence, r/LocalLLaMA, r/MLQuestions, r/ComputerVision, r/LanguageTechnology.

### Hacker News
AI/ML tagged stories via Algolia API.

### Twitter/X (via Nitter)
Top AI researchers and companies.

## Output Format

The script generates `data/newsletter.json` with:
```json
{
  "generated_at": "2026-07-26T08:00:00Z",
  "article_count": 50,
  "categories": {
    "breaking": [...],
    "research": [...],
    "industry": [...],
    "tools": [...]
  },
  "all_articles": [...]
}
```

Each article has:
- `title`, `url`, `source`
- `summary` (LLM-generated)
- `category` (breaking/research/industry/tools)
- `published_at` (ISO timestamp)
- `author`, `tags`

## Troubleshooting

### No articles fetched
- Check network connectivity
- Some RSS feeds may have changed URLs
- Reddit rate limits may apply (429 errors)
- Nitter instances may be down

### LLM summarization fails
- Ensure Hermes Agent is running with API server
- Check `HERMES_API_URL` environment variable
- Script falls back to extractive summarization

### Digest not delivered
- Run `python3 scripts/send_digest.py` locally with the channel env vars set and check the output
- Confirm the GitHub secrets exist: repo → Settings → Secrets and variables → Actions
- Email: check Resend dashboard logs for delivery/domain verification status
- Telegram: confirm the bot can message the chat (private chats need the user to have started the bot)

## License

MIT
