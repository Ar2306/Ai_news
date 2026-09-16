# AI Daily Newsletter

[![Tests](https://github.com/Ar2306/Ai_news/actions/workflows/tests.yml/badge.svg)](https://github.com/Ar2306/Ai_news/actions/workflows/tests.yml)

An automated AI newsletter that fetches, deduplicates, summarizes, and categorizes AI news from multiple sources daily — then delivers a morning briefing to your inbox and Telegram.

🌐 **Live site:** [ai-news-blush.vercel.app](https://ai-news-blush.vercel.app) · [Archive](https://ai-news-blush.vercel.app/archive.html)

## Features

- **Automated daily fetching** at 6 AM UTC via GitHub Actions cron
- **Multiple sources**: RSS feeds, Reddit, Hacker News, Twitter/X (via Nitter)
- **Aggressive deduplication**: retweet/quote-RT filtering, cross-source fuzzy title matching, URL normalization (strips tracking params like `utm_*`, `s=`, `t=`), and **cross-day dedup** against the committed archive
- **Morning digest channels**:
  - 📧 **Email** via Resend (beautiful HTML + plain-text fallback, multi-recipient support)
  - ✈️ **Telegram** via bot (compact HTML message)
- **LLM curation** with **Gemini**, falling back to **Ollama Cloud** — one call per item returns relevance, tags, and a what/why/who summary; if both fail, an extractive summary + keyword tags are used so a run never breaks
- **Multi-label tags** from a fixed topic list (llm, robotics, ai-safety, …), with irrelevant items filtered out
- **AR web UI** — search with highlighting, topic and source filters, save-for-later, "new since your last visit", keyboard shortcuts (`/` `j` `k` `o` `s`), shareable filter URLs, automatic light/dark theme, and a searchable archive grouped by day
- **Locked-down static site** — strict Content-Security-Policy (no inline or third-party scripts), security headers, and a deploy allowlist so only the site files are ever public
- **Static site** — no backend needed, auto-deployed to Vercel on every push

## Project Structure

```
Ai_news/
├── index.html              # AR: today's digest, filterable by tag
├── archive.html            # AR: past days, filterable by tag
├── assets/
│   ├── site.css            # Shared minimal styles
│   └── site.js             # Shared rendering + tag filtering
├── data/
│   ├── newsletter.json     # Today's items (committed by the daily run)
│   ├── archive.json        # Daily archive (committed by the daily run)
│   └── newsletter.db       # SQLite cache (gitignored)
├── scripts/
│   ├── fetch-news.py       # Fetch, dedup, curate with Gemini/Ollama, write JSON
│   ├── send_digest.py      # Morning digest sender (email + Telegram)
│   └── run-fetch.sh        # Wrapper used by the workflow (logs to logs/)
├── .github/workflows/
│   ├── daily-fetch.yml     # 6 AM fetch + commit + send digest
│   └── tests.yml           # CI: compile, schema validation, curation + dedup checks
├── package.json / vercel.json  # Static Vercel deployment
└── requirements.txt        # Python dependencies
```

## Installation

```bash
cd Ai_news
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### LLM API keys (Gemini + Ollama Cloud)

Every item goes through a provider chain, stopping at the first that returns valid JSON:

1. **Gemini** (`GEMINI_API_KEY`) — tries `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.5-flash-lite`, `gemini-3.5-flash` in order, skipping any that are unavailable to the key or have no free quota (Google has closed 2.5 to new keys) — free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. **Ollama Cloud** (`OLLAMA_API_KEY`) — tries `qwen3.5:397b`, and if it needs a paid plan, the rest of the cloud catalogue (Qwen first) until one works on your plan — free key at [ollama.com/settings/keys](https://ollama.com/settings/keys)
3. **Extractive fallback** — first sentences of the source + keyword tags (no key needed)

Add the keys as GitHub secrets (repo → Settings → Secrets and variables → Actions) named exactly `GEMINI_API_KEY` and `OLLAMA_API_KEY`. For local runs, `export` them. Then run **Actions → Check API keys → Run workflow** to confirm both work.

**Free-tier limits:** Gemini calls are paced (`GEMINI_MIN_INTERVAL`, default 6.5 s ≈ 10/min), a run makes at most `MAX_LLM_ITEMS` (default 100) calls, and 429s are retried honouring the server's retry delay. A bad key or an exhausted daily quota switches that provider off for the rest of the run, and the next one takes over. Run **Actions → Check API keys** to see exactly which models each key can use for free.

## Usage

### Manual fetch
```bash
python3 scripts/fetch-news.py
```

### View newsletter
- **Live:** [ai-news-blush.vercel.app](https://ai-news-blush.vercel.app)
- **Locally:** open `index.html` in a browser, or serve it:
```bash
python3 -m http.server 8080
# Then open http://localhost:8080  (or: npm run dev)
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
| `GEMINI_API_KEY` | Gemini API key — primary LLM |
| `OLLAMA_API_KEY` | Ollama Cloud API key — fallback LLM |
| `GEMINI_MODELS` / `OLLAMA_MODELS` | *(optional)* Comma-separated model preference lists |
| `GEMINI_MIN_INTERVAL` | *(optional)* Seconds between Gemini calls (default 6.5) |
| `MAX_LLM_ITEMS` | Max candidates sent to the LLM per run (default 100) |

## Redundancy control

The fetcher applies **four** layers of deduplication, so the same story told by multiple accounts, sources, or days appears once:

1. **Retweet filtering** — entries starting with `RT by @`, `RT @`, or `R to @` are skipped outright (the biggest source of noise).
2. **Fuzzy title matching** — normalized titles (retweet prefixes, URLs, punctuation stripped) are compared across all sources with a `SequenceMatcher` similarity threshold of 0.88.
3. **URL normalization** — tracking params (`utm_*`, `ref`, `source`, `s=`, `t=`, etc.) and trailing slashes are removed before comparing URLs.
4. **Cross-day dedup** — before fetching, the fetcher loads the committed `archive.json` (last `DEDUP_ARCHIVE_DAYS`, default 14) and seeds its seen-URLs/titles sets, so a story already covered yesterday won't reappear in today's digest. The archive accumulates history across runs with a 180-day retention cap (`ARCHIVE_RETENTION_DAYS`).

These behaviors are covered by unit checks in `tests.yml`, so a regression fails CI.

## Deployment

The site is a static page served by Vercel and auto-deployed on every push to `master` (via the GitHub integration in `vercel.json`). `data/newsletter.json` and `data/archive.json` are committed by the daily cron, so each deploy publishes the freshest news.

To deploy your own copy, import this repo in [Vercel](https://vercel.com/new) — no build settings needed. The archive lives at `/archive.html` on the same domain.

## Security

- All feed and LLM text is treated as untrusted: the site escapes every value and only renders `http(s)` links; the email/Telegram digest does the same.
- `vercel.json` sends a strict CSP (`script-src 'self'`, no inline code, no third-party hosts), `X-Frame-Options: DENY`, `nosniff`, HSTS, and a restrictive `Permissions-Policy`.
- `.vercelignore` is an allowlist: only `index.html`, `archive.html`, `assets/` and `data/` are deployed — scripts, workflows and any local `.env` files never become public URLs.
- Secrets live only in GitHub Actions secrets; CI runs with a read-only token.
- Saved items and "last visit" live in your browser's `localStorage` only.

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
  "all_articles": [
    {
      "title": "...",
      "url": "https://...",
      "source": "arXiv AI/ML",
      "tags": ["llm", "training-infra"],
      "summary": {"what": "...", "why": "...", "who": "..."},
      "published_at": "2026-07-26T04:00:00Z",
      "fetched_at": "2026-07-26T06:01:12Z",
      "author": "..."
    }
  ]
}
```

- `tags` — 1-3 labels from a fixed list: `llm`, `reinforcement-learning`, `world-models`, `foundational-models`, `multimodal`, `robotics`, `interpretability`, `ai-safety`, `simulation`, `training-infra`, `general-ml`, `other`
- `summary` — three short lines: **what** it is, **why** it matters, **who** is behind it
- Items the LLM judges not genuinely about AI/ML are dropped before anything is written

`data/archive.json` holds one entry per day (`date`, `total`, per-tag `tags` counts, `articles` in the same shape).

## Troubleshooting

### No articles fetched
- Check network connectivity
- Some RSS feeds may have changed URLs
- Reddit rate limits may apply (429 errors)
- Nitter instances may be down

### LLM curation fails
- Run **Actions → Check API keys** — it tests each key with the exact request the daily run makes
- In the fetch log, `⚠ Gemini failed (...)` / `⚠ Ollama Cloud failed (...)` name the error; `⛔ ... disabled` means a bad key or used-up daily quota
- HTTP 400 "API key not valid" / 401: re-create the key and update the secret
- HTTP 429: free-tier limit — raise `GEMINI_MIN_INTERVAL` or lower `MAX_LLM_ITEMS`
- If every provider fails, items still get extractive summaries + keyword tags

### Digest not delivered
- Run `python3 scripts/send_digest.py` locally with the channel env vars set and check the output
- Confirm the GitHub secrets exist: repo → Settings → Secrets and variables → Actions
- Email: check Resend dashboard logs for delivery/domain verification status
- Telegram: confirm the bot can message the chat (private chats need the user to have started the bot)

## License

MIT
