#!/usr/bin/env python3
"""
AI Newsletter Fetcher - Fetches AI news from multiple sources, summarizes with LLM,
and outputs structured JSON + SQLite database for the newsletter page.
"""

import asyncio
import json
import os
import re
import sys
import time
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import anthropic
import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# Add the project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configuration
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "newsletter.json"
DB_FILE = DATA_DIR / "newsletter.db"
ARCHIVE_FILE = DATA_DIR / "archive.json"

# Cross-day dedup window: stories archived within this many days are treated as
# already covered and won't reappear in the digest. Older stories can return.
DEDUP_ARCHIVE_DAYS = int(os.environ.get("DEDUP_ARCHIVE_DAYS", "14"))

# How much history the committed archive.json keeps (bounded growth over years
# of daily runs). The dedup window above is the part that actually matters.
ARCHIVE_RETENTION_DAYS = int(os.environ.get("ARCHIVE_RETENTION_DAYS", "180"))

# LLM Configuration: Claude Haiku 4.5 via the Anthropic API (ANTHROPIC_API_KEY).
# Without a key every item uses the extractive fallback instead.
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_MAX_RETRIES = 4       # SDK retries 429/5xx/connection errors with exponential backoff
LLM_CALL_DELAY = 0.5      # seconds between calls, keeps a ~100-item run under rate limits

# RSS Feeds
RSS_FEEDS = {
    "arXiv AI/ML": "https://export.arxiv.org/rss/cs.AI",
    "arXiv ML": "https://export.arxiv.org/rss/cs.LG",
    "arXiv CV": "https://export.arxiv.org/rss/cs.CV",
    "arXiv CL": "https://export.arxiv.org/rss/cs.CL",
    "Hugging Face Blog": "https://huggingface.co/blog/feed.xml",
    "OpenAI Blog": "https://openai.com/blog/rss.xml",
    "Anthropic Blog": "https://www.anthropic.com/blog/rss.xml",
    "Google AI Blog": "https://ai.googleblog.com/feeds/posts/default",
    "Microsoft Research Blog": "https://www.microsoft.com/en-us/research/blog/feed/",
    "Meta AI Blog": "https://ai.meta.com/blog/rss/",
    "NVIDIA Blog": "https://blogs.nvidia.com/feed/",
    "Google DeepMind Blog": "https://deepmind.google/blog/rss.xml",
    "Cohere Blog": "https://cohere.com/blog/rss.xml",
    "LangChain Blog": "https://blog.langchain.dev/rss/",
    "Weights & Biases Blog": "https://wandb.ai/site/feed.xml",
    "AssemblyAI Blog": "https://www.assemblyai.com/blog/rss.xml",
    "Replicate Blog": "https://replicate.com/blog/rss.xml",
    "Together AI Blog": "https://www.together.ai/blog/rss.xml",
}

# Reddit feeds (using RSS)
REDDIT_FEEDS = {
    "r/MachineLearning": "https://www.reddit.com/r/MachineLearning/.rss",
    "r/ArtificialIntelligence": "https://www.reddit.com/r/ArtificialIntelligence/.rss",
    "r/LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/.rss",
    "r/MLQuestions": "https://www.reddit.com/r/MLQuestions/.rss",
    "r/Computervision": "https://www.reddit.com/r/ComputerVision/.rss",
    "r/NLP": "https://www.reddit.com/r/LanguageTechnology/.rss",
}

# Hacker News (using Algolia API for AI/ML tagged stories)
HN_API = "https://hn.algolia.com/api/v1/search_by_date"

# Nitter instances for Twitter/X (RSS bridges)
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.unixfox.eu",
    "https://nitter.himiko.cloud",
]

# Twitter accounts to follow via Nitter RSS
TWITTER_ACCOUNTS = [
    "OpenAI", "AnthropicAI", "GoogleAI", "MicrosoftResearch",
    "MetaAI", "DeepMind", "NVIDIAResearch", "HuggingFace",
    "LangChainAI", "WandB", "SimonsInstitute", "Karpathy",
    "ylecun", "AndrewYNg", "fchollet", "goodfellow_ian",
    "sama", "greg_brockman", "demishassabis", "hardmaru",
]

# Closed tag vocabulary. Articles are multi-label (1-3 tags) and the LLM is
# never allowed to invent a tag outside this list.
TOPIC_TAGS = [
    "llm", "reinforcement-learning", "world-models", "foundational-models",
    "multimodal", "robotics", "interpretability", "ai-safety",
    "simulation", "training-infra", "general-ml", "other",
]

# Keyword hints used only by the fallback path (when the LLM call fails)
FALLBACK_TAG_KEYWORDS = {
    "llm": ["llm", "language model", "gpt", "claude", "gemini", "llama", "transformer", "token", "prompt", "chatbot", "rag"],
    "reinforcement-learning": ["reinforcement learning", "rlhf", "reward model", "policy gradient", "ppo", "grpo", "q-learning"],
    "world-models": ["world model"],
    "foundational-models": ["foundation model", "foundational model", "pretrain", "pre-train", "frontier model"],
    "multimodal": ["multimodal", "vision-language", "vlm", "image", "video", "audio", "speech", "diffusion"],
    "robotics": ["robot", "embodied", "manipulation", "locomotion", "humanoid"],
    "interpretability": ["interpretab", "explainab", "mechanistic", "sparse autoencoder", "circuit"],
    "ai-safety": ["safety", "alignment", "jailbreak", "red team", "misuse", "guardrail"],
    "simulation": ["simulation", "simulator", "sim-to-real", "synthetic environment"],
    "training-infra": ["gpu", "inference", "quantization", "distributed training", "kernel", "cuda", "serving", "throughput", "compute"],
}

MAX_ARTICLES = 50  # items kept in newsletter.json
MAX_PER_SOURCE = 10  # candidate cap per source, keeps the digest varied
MAX_LLM_ITEMS = int(os.environ.get("MAX_LLM_ITEMS", "100"))  # candidates enriched per run

CURATOR_PROMPT = """You are a technical curator for a personal AI/ML research digest called "AR."

Given the title and abstract/content of one item, do three things:

1. RELEVANCE: Decide if this item is genuinely about AI/ML — not just
   mentioning "AI" in passing (marketing fluff, AI-adjacent business news,
   listicles). If not relevant, return only: {"relevant": false}

2. TAGS: If relevant, assign 1-3 tags from this exact list only. Never
   invent a new tag. Pick the most specific ones that apply:
   ["llm", "reinforcement-learning", "world-models", "foundational-models",
    "multimodal", "robotics", "interpretability", "ai-safety",
    "simulation", "training-infra", "general-ml", "other"]

3. SUMMARY: Write exactly 3 short lines, no fluff, no marketing tone:
   - WHAT: one sentence, what this actually is (paper/model/tool/announcement)
   - WHY: one sentence, why it matters or what's new about it
   - WHO: the lab, company, or author(s) behind it

Rules:
- Never copy sentences from the source. Fully rewrite in your own words.
- If you're unsure of "WHO", write "Unknown" rather than guessing.
- Be skeptical of hype language in the source; report what was actually
  claimed, not how exciting it sounds.
- Keep total summary under 60 words.

Return ONLY valid JSON, no markdown fences, no preamble:
{
  "relevant": true,
  "tags": ["llm", "training-infra"],
  "summary": {"what": "...", "why": "...", "who": "..."}
}

TITLE: {title}
SOURCE: {source_name}
CONTENT: {abstract_or_excerpt}
"""


def fallback_tags(title: str, content: str) -> list[str]:
    """Keyword-based tags for the fallback path. Always returns 1-3 tags from TOPIC_TAGS."""
    text = f"{title} {content}".lower()
    scores = {
        tag: sum(text.count(kw) for kw in kws)
        for tag, kws in FALLBACK_TAG_KEYWORDS.items()
    }
    tags = [t for t, n in sorted(scores.items(), key=lambda kv: -kv[1]) if n > 0][:3]
    return tags or ["general-ml"]


def parse_curation(raw: str) -> dict:
    """Parse and validate the curator's JSON reply.

    Returns {"relevant": False} or {"relevant": True, "tags": [...], "summary": {...}}.
    Raises ValueError on anything malformed so the caller can fall back.
    """
    text = raw.strip()
    # Tolerate a stray ```json fence even though the prompt forbids it
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("relevant"), bool):
        raise ValueError("missing boolean 'relevant'")
    if not data["relevant"]:
        return {"relevant": False}

    tags = [t for t in data.get("tags") or [] if t in TOPIC_TAGS]
    tags = list(dict.fromkeys(tags))[:3] or ["other"]

    summary = data.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("missing 'summary' object")
    clean = {k: str(summary.get(k) or "").strip() for k in ("what", "why", "who")}
    if not clean["what"]:
        raise ValueError("summary.what is empty")
    clean["who"] = clean["who"] or "Unknown"
    return {"relevant": True, "tags": tags, "summary": clean}


@dataclass
class Article:
    title: str
    url: str
    source: str
    summary: dict = field(default_factory=lambda: {"what": "", "why": "", "who": ""})
    published_at: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    author: str = ""
    tags: list[str] = field(default_factory=list)
    content: str = ""  # raw excerpt fed to the LLM; never written to JSON

    def to_dict(self):
        d = asdict(self)
        d.pop("content")
        return d

    def to_db_tuple(self):
        """Convert to tuple for database insertion."""
        return (
            self.title,
            self.url,
            self.source,
            json.dumps(self.summary, ensure_ascii=False),
            self.published_at,
            self.fetched_at,
            self.author,
            json.dumps(self.tags),
        )


class NewsFetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "AI-Newsletter-Bot/1.0 (+https://github.com/ai-newsletter)"},
            follow_redirects=True,
        )
        self.articles: list[Article] = []
        self.seen_urls: set[str] = set()
        self.seen_titles: set[str] = set()
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.llm = anthropic.Anthropic(api_key=api_key, max_retries=LLM_MAX_RETRIES) if api_key else None
        if self.llm is None:
            print("⚠ ANTHROPIC_API_KEY not set — using extractive summaries + keyword tags")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for deduplication."""
        parsed = urlparse(url)
        # Remove tracking params (utm_*, social share refs, Tw/X share code 's')
        clean_query = "&".join(
            p for p in parsed.query.split("&")
            if p and not p.startswith(("utm_", "ref", "source", "medium", "campaign", "fbclid", "gclid", "s=", "t="))
        )
        return parsed._replace(query=clean_query, fragment="").geturl().rstrip("/")

    def _is_duplicate(self, url: str) -> bool:
        norm = self._normalize_url(url)
        if norm in self.seen_urls:
            return True
        self.seen_urls.add(norm)
        return False

    def _normalize_title(self, title: str) -> str:
        """Normalize a title for near-duplicate detection."""
        t = title.strip()
        # Strip retweet / reply prefixes
        t = re.sub(r"^(RT by @[^:]+:|RT @[^:]+:|R to @[^:]+:|@\w+\s*:)\s*", "", t, flags=re.IGNORECASE)
        # Remove URLs
        t = re.sub(r"https?://\S+", "", t)
        # Keep letters, digits, spaces only
        t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
        t = re.sub(r"\s+", " ", t).strip()
        return t[:120]

    def _is_title_duplicate(self, title: str) -> bool:
        """Cross-source near-duplicate detection by fuzzy title similarity."""
        key = self._normalize_title(title)
        if not key:
            return True  # too noisy to be useful
        if key in self.seen_titles:
            return True
        for k in self.seen_titles:
            if SequenceMatcher(None, key, k).ratio() > 0.88:
                return True
        self.seen_titles.add(key)
        return False

    def _parse_date(self, date_str: str) -> str:
        """Parse various date formats to ISO format."""
        if not date_str:
            return datetime.now(timezone.utc).isoformat()
        try:
            dt = dateparser.parse(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

    def _is_recent(self, date_str: str, days: int = 2) -> bool:
        """Check if article is within the last N days."""
        try:
            dt = dateparser.parse(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            return dt >= cutoff
        except Exception:
            return True  # Include if can't parse

    def _clean_html(self, html: str) -> str:
        """Extract clean text from HTML."""
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        # Remove scripts, styles, etc.
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _call_llm(self, prompt: str) -> str:
        """Run one prompt through Claude and return the raw text response."""
        if self.llm is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        response = self.llm.messages.create(
            model=LLM_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def _fallback_curation(self, article: Article) -> dict:
        """Extractive summary + keyword tags, used whenever the LLM call fails."""
        content = article.content or article.title
        # arXiv RSS abstracts start with "arXiv:2609.12345v1 Announce Type: new Abstract:"
        content = re.sub(r"^\S*\s*Announce Type:\s*\S+\s*Abstract:\s*", "", content)
        sentences = re.split(r"(?<=[.!?])\s+", content)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
        what = sentences[0][:200] if sentences else article.title[:200]
        why = sentences[1][:200] if len(sentences) > 1 else ""
        return {
            "relevant": True,  # can't judge relevance without the LLM; sources are AI-focused
            "tags": fallback_tags(article.title, content),
            "summary": {"what": what, "why": why, "who": article.author or "Unknown"},
        }

    def curate(self, article: Article) -> Optional[Article]:
        """Classify + summarize one article. Returns None if it isn't AI/ML relevant."""
        prompt = (
            CURATOR_PROMPT
            .replace("{title}", article.title)
            .replace("{source_name}", article.source)
            .replace("{abstract_or_excerpt}", (article.content or "(no content)")[:3000])
        )
        try:
            result = parse_curation(self._call_llm(prompt))
        except Exception as e:
            if getattr(self, "llm", True) is not None:  # missing key was already reported once
                print(f"    ⚠ LLM curation failed ({type(e).__name__}: {e}), using extractive fallback", file=sys.stderr)
            result = self._fallback_curation(article)

        if not result["relevant"]:
            return None
        article.tags = result["tags"]
        article.summary = result["summary"]
        return article

    async def fetch_rss_feed(self, name: str, url: str) -> list[Article]:
        """Fetch and parse an RSS feed."""
        articles = []
        try:
            print(f"  📡 Fetching RSS: {name}...")
            response = await self.client.get(url)
            response.raise_for_status()

            feed = feedparser.parse(response.content)
            if feed.bozo and feed.bozo_exception:
                print(f"  ⚠ Feed parse warning: {feed.bozo_exception}")

            for entry in feed.entries[:30]:  # Limit per feed
                # Get URL
                link = entry.get("link", "")
                if not link or self._is_duplicate(link):
                    continue

                # Get title
                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Get date
                published = ""
                for key in ["published", "updated", "created", "pubDate"]:
                    if key in entry:
                        published = entry[key]
                        break
                published_iso = self._parse_date(published)

                # Skip old articles (older than 3 days for RSS)
                if not self._is_recent(published_iso, days=3):
                    continue

                # Skip duplicates AFTER the recency check, so a stale copy of a
                # story can't claim the title and suppress the fresh one.
                if self._is_title_duplicate(title):
                    continue

                # Get summary/content
                content = ""
                for key in ["summary", "description", "content", "contentSnippet"]:
                    if key in entry:
                        val = entry[key]
                        if isinstance(val, list) and val:
                            val = val[0].get("value", "")
                        content = self._clean_html(str(val))
                        if content:
                            break

                # Get author
                author = entry.get("author", "") or (entry.get("authors", [{}])[0].get("name", "") if entry.get("authors") else "")

                article = Article(
                    title=title,
                    url=link,
                    source=name,
                    content=content,
                    published_at=published_iso,
                    author=author,
                )
                articles.append(article)

            print(f"    ✅ Got {len(articles)} articles from {name}")

        except Exception as e:
            print(f"  ❌ Error fetching {name}: {e}")

        return articles

    async def fetch_reddit(self, name: str, url: str) -> list[Article]:
        """Fetch Reddit RSS feed."""
        articles = []
        try:
            print(f"  📡 Fetching Reddit: {name}...")
            response = await self.client.get(url)
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            for entry in feed.entries[:20]:
                link = entry.get("link", "")
                if not link or self._is_duplicate(link):
                    continue

                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Reddit RSS includes selftext in summary
                content = self._clean_html(entry.get("summary", "") or entry.get("description", ""))

                published = entry.get("published", "") or entry.get("updated", "")
                published_iso = self._parse_date(published)

                if not self._is_recent(published_iso, days=2):
                    continue

                # Extract subreddit from title if present
                subreddit_match = re.match(r"\[(r/[\w]+)\]", title)
                if subreddit_match:
                    title = title[subreddit_match.end():].strip()

                if self._is_title_duplicate(title):
                    continue

                article = Article(
                    title=title,
                    url=link,
                    source=name,
                    content=content,
                    published_at=published_iso,
                    author=entry.get("author", ""),
                )
                articles.append(article)

            print(f"    ✅ Got {len(articles)} articles from {name}")

        except Exception as e:
            print(f"  ❌ Error fetching {name}: {e}")

        return articles

    async def fetch_hacker_news(self) -> list[Article]:
        """Fetch AI/ML stories from Hacker News via Algolia API."""
        articles = []
        try:
            print("  📡 Fetching Hacker News (AI/ML)...")

            # Search for AI/ML related stories from last 48 hours
            tags = ["machine-learning", "artificial-intelligence", "llm", "gpt", "ai"]
            cutoff = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())

            all_hits = []
            for tag in tags:
                params = {
                    "tags": tag,
                    "numericFilters": f"created_at_i>={cutoff}",
                    "hitsPerPage": 20,
                }
                response = await self.client.get(HN_API, params=params)
                response.raise_for_status()
                data = response.json()
                all_hits.extend(data.get("hits", []))

            # Deduplicate by objectID
            seen_ids = set()
            for hit in all_hits:
                obj_id = hit.get("objectID")
                if obj_id in seen_ids:
                    continue
                seen_ids.add(obj_id)

                url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
                if not url or self._is_duplicate(url):
                    continue

                title = hit.get("title", "").strip()
                if not title:
                    continue

                # Get content from HN comment or story text
                content = hit.get("story_text", "") or ""
                if not content and hit.get("url"):
                    # Try to fetch article content
                    try:
                        resp = await self.client.get(hit["url"], timeout=10.0)
                        content = self._clean_html(resp.text)[:2000]
                    except Exception:
                        pass

                published_iso = datetime.fromtimestamp(hit.get("created_at_i", 0), tz=timezone.utc).isoformat()

                if not self._is_recent(published_iso, days=2):
                    continue

                # Skip duplicates AFTER the recency check, so a stale copy can't
                # claim the title and suppress the fresh one.
                if self._is_title_duplicate(title):
                    continue

                article = Article(
                    title=title,
                    url=url,
                    source="Hacker News",
                    content=content,
                    published_at=published_iso,
                    author=hit.get("author", ""),
                )
                articles.append(article)

            print(f"    ✅ Got {len(articles)} articles from Hacker News")

        except Exception as e:
            print(f"  ❌ Error fetching Hacker News: {e}")

        return articles

    async def fetch_nitter(self, username: str) -> list[Article]:
        """Fetch tweets from a user via Nitter RSS."""
        articles = []
        for instance in NITTER_INSTANCES:
            try:
                url = f"{instance}/{username}/rss"
                print(f"  🐦 Fetching @{username} via {instance}...")
                response = await self.client.get(url, timeout=15.0)
                if response.status_code != 200:
                    continue

                feed = feedparser.parse(response.content)
                for entry in feed.entries[:10]:
                    link = entry.get("link", "")
                    if not link or self._is_duplicate(link):
                        continue

                    title = entry.get("title", "").strip()
                    if not title:
                        continue

                    # Skip retweets and quote-RTs (the biggest source of duplicate noise)
                    if re.match(r"^(RT by @|RT @|R to @)", title):
                        continue

                    content = self._clean_html(entry.get("summary", "") or entry.get("description", ""))

                    published = entry.get("published", "") or entry.get("updated", "")
                    published_iso = self._parse_date(published)

                    if not self._is_recent(published_iso, days=1):
                        continue

                    # Skip replies unless they carry substantial content
                    if title.startswith("@") and len(content) < 100:
                        continue

                    if self._is_title_duplicate(title):
                        continue

                    article = Article(
                        title=title[:200],
                        url=link,
                        source=f"Twitter/@{username}",
                        content=content,
                        published_at=published_iso,
                        author=username,
                    )
                    articles.append(article)

                if articles:
                    break  # Success, don't try other instances

            except Exception as e:
                print(f"    ⚠ Nitter instance {instance} failed: {e}")
                continue

        return articles

    async def fetch_all(self) -> list[Article]:
        """Fetch from all sources."""
        all_articles = []

        # RSS Feeds
        print("\n📡 Fetching RSS feeds...")
        for name, url in RSS_FEEDS.items():
            articles = await self.fetch_rss_feed(name, url)
            all_articles.extend(articles)
            await asyncio.sleep(0.5)  # Rate limiting

        # Reddit
        print("\n📡 Fetching Reddit...")
        for name, url in REDDIT_FEEDS.items():
            articles = await self.fetch_reddit(name, url)
            all_articles.extend(articles)
            await asyncio.sleep(0.5)

        # Hacker News
        print("\n📡 Fetching Hacker News...")
        articles = await self.fetch_hacker_news()
        all_articles.extend(articles)

        # Twitter/X via Nitter (limited to avoid rate limits)
        print("\n🐦 Fetching Twitter/X (top accounts)...")
        top_accounts = TWITTER_ACCOUNTS[:8]  # Limit to avoid rate limits
        for username in top_accounts:
            articles = await self.fetch_nitter(username)
            all_articles.extend(articles)
            await asyncio.sleep(1.0)  # Be nice to Nitter instances

        # Sort by date (newest first), then pick candidates with a per-source cap
        # so one prolific feed (e.g. arXiv) can't crowd out everything else.
        all_articles.sort(key=lambda a: a.published_at, reverse=True)
        per_source: dict[str, int] = {}
        candidates = []
        for a in all_articles:
            if per_source.get(a.source, 0) >= MAX_PER_SOURCE:
                continue
            per_source[a.source] = per_source.get(a.source, 0) + 1
            candidates.append(a)
        candidates = candidates[:MAX_LLM_ITEMS]

        final_articles = self.curate_all(candidates)

        print(f"\n✅ Total articles kept: {len(final_articles)} (of {len(all_articles)} fetched)")
        for tag in TOPIC_TAGS:
            count = sum(1 for a in final_articles if tag in a.tags)
            if count:
                print(f"   {tag}: {count}")

        self.articles = final_articles
        return final_articles

    def curate_all(self, candidates: list[Article]) -> list[Article]:
        """Curate candidates in order, dropping irrelevant ones, until MAX_ARTICLES are kept."""
        kept = []
        print(f"\n🧠 Curating {len(candidates)} candidates...")
        for article in candidates:
            if len(kept) >= MAX_ARTICLES:
                break
            print(f"    📝 {article.title[:70]}")
            if getattr(self, "llm", None) is not None:
                time.sleep(LLM_CALL_DELAY)
            if self.curate(article) is None:
                print("       ↳ dropped (not AI/ML relevant)")
                continue
            kept.append(article)
        return kept


def init_database(db_path: Path):
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    # The DB is a disposable per-run cache (archive.json is the durable history),
    # so a table from the old category-based schema is simply rebuilt.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    if "category" in cols:
        conn.execute("DROP TABLE articles")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            summary TEXT,  -- JSON object {what, why, who}
            published_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            author TEXT,
            tags TEXT,  -- JSON array of TOPIC_TAGS
            date_key TEXT NOT NULL,  -- YYYY-MM-DD for date-based queries
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date_key ON articles(date_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_published_at ON articles(published_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
    conn.commit()
    return conn


def save_to_database(conn: sqlite3.Connection, articles: list[Article], date_key: str):
    """Save articles to SQLite database."""
    cursor = conn.cursor()
    saved = 0
    for article in articles:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO articles
                (title, url, source, summary, published_at, fetched_at, author, tags, date_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, article.to_db_tuple() + (date_key,))
            if cursor.rowcount > 0:
                saved += 1
        except Exception as e:
            print(f"  ⚠ DB insert error for {article.url}: {e}")
    conn.commit()
    return saved


def load_archive_seen(archive_file: Path, days: int = DEDUP_ARCHIVE_DAYS):
    """Load URLs and normalized titles from the committed archive for cross-day dedup.

    The SQLite DB is gitignored and rebuilt fresh each CI run, so the committed
    archive.json is the only persistent memory of what has already been covered.
    Returns (seen_urls, seen_titles) seed sets for the fetcher.
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    if not archive_file.exists():
        return seen_urls, seen_titles
    try:
        data = json.loads(archive_file.read_text(encoding="utf-8"))
        archive = data.get("archive", []) or []
    except Exception:
        return seen_urls, seen_titles

    # Reuse the normalize helpers without constructing a network client
    f = object.__new__(NewsFetcher)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    for day in archive:
        if day.get("date", "") < cutoff:
            continue
        for a in day.get("articles", []) or []:
            url = a.get("url") or ""
            if url:
                seen_urls.add(f._normalize_url(url))
            title = a.get("title") or ""
            key = f._normalize_title(title)
            if key:
                seen_titles.add(key)
    return seen_urls, seen_titles


def build_archive_index(conn: sqlite3.Connection, output_path: Path):
    """Build a static archive index JSON from the database, preserving history.

    Merges with any previously committed archive.json so past days accumulate
    (the DB is gitignored and doesn't survive between CI runs).
    """
    cursor = conn.cursor()

    cursor.execute("SELECT DISTINCT date_key FROM articles ORDER BY date_key DESC")
    date_keys = [row[0] for row in cursor.fetchall()]

    # Get articles for each date (limited for archive page)
    archive = []
    for date_key in date_keys:
        cursor.execute("SELECT COUNT(*) FROM articles WHERE date_key = ?", (date_key,))
        total = cursor.fetchone()[0]
        cursor.execute("""
            SELECT title, url, source, summary, published_at, author, tags
            FROM articles
            WHERE date_key = ?
            ORDER BY published_at DESC
            LIMIT 20
        """, (date_key,))
        articles = [
            {
                "title": a[0],
                "url": a[1],
                "source": a[2],
                "summary": json.loads(a[3]) if a[3] else {"what": "", "why": "", "who": "Unknown"},
                "published_at": a[4],
                "author": a[5],
                "tags": json.loads(a[6]) if a[6] else [],
            }
            for a in cursor.fetchall()
        ]
        tag_counts = {t: sum(1 for a in articles if t in a["tags"]) for t in TOPIC_TAGS}
        archive.append({
            "date": date_key,
            "total": total,
            "tags": {t: n for t, n in tag_counts.items() if n},
            "articles": articles,
        })

    # Merge with the previously committed archive so history accumulates across
    # runs (the DB is regenerated fresh in CI and would otherwise lose old days).
    prev_by_date = {}
    if output_path.exists():
        try:
            prev = json.loads(output_path.read_text(encoding="utf-8")).get("archive", [])
            prev_by_date = {d["date"]: d for d in prev if d.get("date")}
        except Exception:
            pass
    for entry in archive:
        prev_by_date[entry["date"]] = entry
    merged = sorted(prev_by_date.values(), key=lambda d: d["date"], reverse=True)

    # Bound archive growth: drop days older than the retention window.
    retention_cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)).date().isoformat()
    merged = [d for d in merged if d.get("date", "") >= retention_cutoff]

    with open(output_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "archive": merged
        }, f, indent=2, ensure_ascii=False)

    print(f"📚 Archive index built: {len(merged)} days, saved to {output_path}")


async def main():
    """Main entry point."""
    print("=" * 60)
    print("🤖 AI Newsletter Fetcher (with SQLite Archive)")
    print("=" * 60)

    start_time = time.time()

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize database
    conn = init_database(DB_FILE)

    async with NewsFetcher() as fetcher:
        # Cross-day dedup: seed seen sets from the committed archive so stories
        # already covered in previous days don't reappear in today's digest.
        seen_urls, seen_titles = load_archive_seen(ARCHIVE_FILE)
        fetcher.seen_urls |= seen_urls
        fetcher.seen_titles |= seen_titles
        if seen_urls or seen_titles:
            print(f"📚 Cross-day dedup: {len(seen_urls)} urls + {len(seen_titles)} titles loaded from archive")

        articles = await fetcher.fetch_all()

        # Date key for today's batch
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Save to database
        saved = save_to_database(conn, articles, date_key)
        print(f"\n💾 Saved {saved} new articles to database ({DB_FILE})")

        # Build archive index
        build_archive_index(conn, ARCHIVE_FILE)

        # Convert to dict for JSON serialization (current day only)
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "article_count": len(articles),
            "all_articles": [a.to_dict() for a in articles],
        }

        # Write current day's newsletter.json
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"💾 Saved current newsletter to {OUTPUT_FILE}")
        print(f"⏱ Completed in {time.time() - start_time:.1f}s")

    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n⚠ Interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)