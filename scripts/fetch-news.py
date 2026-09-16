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
from urllib.parse import urljoin, urlparse

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
SOURCES_FILE = Path(__file__).parent.parent / "sources.json"

# Cross-day dedup window: stories archived within this many days are treated as
# already covered and won't reappear in the digest. Older stories can return.
DEDUP_ARCHIVE_DAYS = int(os.environ.get("DEDUP_ARCHIVE_DAYS", "14"))

# How much history the committed archive.json keeps (bounded growth over years
# of daily runs). The dedup window above is the part that actually matters.
ARCHIVE_RETENTION_DAYS = int(os.environ.get("ARCHIVE_RETENTION_DAYS", "180"))

# LLM Configuration (uses Hermes Agent's built-in tools)
LLM_MODEL = os.environ.get("HERMES_MODEL", "nemotron-3-ultra-free")
LLM_PROVIDER = os.environ.get("HERMES_PROVIDER", "opencode-zen")

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

# Categorization keywords
CATEGORY_KEYWORDS = {
    "breaking": [
        "breaking", "announced", "launch", "release", "unveil", "introduce",
        "new model", "new version", "v1", "v2", "v3", "launch", "available now",
        "now available", "announcing", "introducing", "preview", "beta", "ga",
        "general availability", "open source", "open-source", "released"
    ],
    "research": [
        "paper", "research", "arxiv", "study", "experiment", "benchmark",
        "evaluation", "analysis", "theoretical", "proof", "algorithm", "method",
        "approach", "framework", "architecture", "model", "training", "pretrain",
        "fine-tun", "scaling law", "emergent", "capability", "alignment",
        "interpretability", "robustness", "generalization", "sample efficiency",
        "neurips", "icml", "iclr", "cvpr", "iccv", "acl", "emnlp", "naacl",
        "conference", "proceedings", "accepted", "published", "preprint"
    ],
    "industry": [
        "partnership", "acquisition", "funding", "investment", "series a", "series b",
        "series c", "ipo", "valuation", "billion", "million", "startup", "enterprise",
        "deployment", "production", "infrastructure", "cloud", "api", "platform",
        "commercial", "business", "revenue", "customer", "client", "adoption",
        "regulation", "policy", "law", "government", "eu ai act", "white house",
        "executive order", "copyright", "lawsuit", "legal", "ethics", "safety",
        "responsible ai", "governance", "standard", "certification"
    ],
    "tools": [
        "tool", "library", "framework", "sdk", "api", "cli", "gui", "interface",
        "release", "version", "update", "patch", "feature", "plugin", "extension",
        "integration", "wrapper", "binding", "python", "javascript", "typescript",
        "rust", "go", "docker", "kubernetes", "deployment", "inference", "serving",
        "quantization", "gguf", "ggml", "llama.cpp", "vllm", "tensorrt", "onnx",
        "hugging face", "transformers", "accelerate", "peft", "trl", "langchain",
        "llamaindex", "haystack", "chroma", "weaviate", "pinecone", "milvus",
        "qdrant", "redis", "vector database", "embedding", "rerank", "rag",
        "agent", "workflow", "orchestration", "autogen", "crewai", "langgraph",
        "composio", "browserbase", "playwright", "selenium", "puppeteer"
    ],
}

# Default categories for sources
SOURCE_DEFAULT_CATEGORY = {
    "arXiv": "research",
    "Hugging Face": "tools",
    "OpenAI": "breaking",
    "Anthropic": "breaking",
    "Google": "industry",
    "Microsoft": "industry",
    "Meta": "industry",
    "NVIDIA": "industry",
    "DeepMind": "research",
    "Cohere": "breaking",
    "LangChain": "tools",
    "Weights & Biases": "tools",
    "Reddit": "research",
    "Hacker News": "breaking",
    "Twitter": "breaking",
}


@dataclass
class Article:
    title: str
    url: str
    source: str
    summary: str = ""
    category: str = "industry"
    published_at: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    author: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_db_tuple(self):
        """Convert to tuple for database insertion."""
        return (
            self.title,
            self.url,
            self.source,
            self.summary,
            self.category,
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

    def _categorize(self, title: str, summary: str, source: str) -> str:
        """Categorize article based on content and source."""
        text = f"{title} {summary}".lower()

        # Check each category's keywords
        scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
        for cat, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    scores[cat] += 1

        # Boost based on source default
        source_lower = source.lower()
        for src_key, cat in SOURCE_DEFAULT_CATEGORY.items():
            if src_key.lower() in source_lower:
                scores[cat] = scores.get(cat, 0) + 2
                break

        # Return highest scoring category
        if max(scores.values()) > 0:
            return max(scores, key=scores.get)
        return "industry"

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

    async def _summarize_with_llm(self, title: str, content: str, source: str, max_length: int = 200) -> str:
        """Summarize article using LLM via terminal/hermes tools."""
        if not content or len(content) < 100:
            return content[:max_length] + "..." if len(content) > max_length else content

        # Truncate content for LLM
        content = content[:3000]

        prompt = f"""Summarize this AI news article in 2-3 sentences (max {max_length} chars).

Title: {title}
Source: {source}
Content: {content}

Requirements:
- Focus on key technical details, numbers, model names, benchmarks
- Mention what's new/novel
- Be concise and informative
- No fluff or marketing language

Summary:"""

        try:
            # Use hermes terminal to run the LLM
            import subprocess
            result = subprocess.run(
                [
                    "hermes", "run",
                    "--model", LLM_MODEL,
                    "--provider", LLM_PROVIDER,
                    "--prompt", prompt,
                    "--max-tokens", "300",
                    "--temperature", "0.3",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                summary = result.stdout.strip()
                # Clean up any preamble
                summary = re.sub(r"^(Summary:|Here is a summary:)\s*", "", summary, flags=re.IGNORECASE)
                return summary[:max_length]
        except Exception as e:
            print(f"  ⚠ LLM summarization failed: {e}", file=sys.stderr)

        # Fallback: extractive summary
        sentences = re.split(r"[.!?]+", content)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
        summary = " ".join(sentences[:3])
        return summary[:max_length]

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

                # Get tags/categories
                tags = [tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")]

                # Categorize
                category = self._categorize(title, content, name)

                # Summarize with LLM
                print(f"    📝 Summarizing: {title[:60]}...")
                summary = await self._summarize_with_llm(title, content, name)

                article = Article(
                    title=title,
                    url=link,
                    source=name,
                    summary=summary,
                    category=category,
                    published_at=published_iso,
                    author=author,
                    tags=tags[:5],
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

                category = self._categorize(title, content, name)

                print(f"    📝 Summarizing: {title[:60]}...")
                summary = await self._summarize_with_llm(title, content, name)

                article = Article(
                    title=title,
                    url=link,
                    source=name,
                    summary=summary,
                    category=category,
                    published_at=published_iso,
                    author=entry.get("author", ""),
                    tags=["reddit"],
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

                category = self._categorize(title, content, "Hacker News")

                print(f"    📝 Summarizing: {title[:60]}...")
                summary = await self._summarize_with_llm(title, content, "Hacker News")

                article = Article(
                    title=title,
                    url=url,
                    source="Hacker News",
                    summary=summary,
                    category=category,
                    published_at=published_iso,
                    author=hit.get("author", ""),
                    tags=["hacker-news", hit.get("tag", "")],
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

                    category = self._categorize(title, content, f"Twitter/@{username}")

                    print(f"    📝 Summarizing: {title[:60]}...")
                    summary = await self._summarize_with_llm(title, content, f"Twitter/@{username}")

                    article = Article(
                        title=title[:200],
                        url=link,
                        source=f"Twitter/@{username}",
                        summary=summary,
                        category=category,
                        published_at=published_iso,
                        author=username,
                        tags=["twitter", username.lower()],
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

        # Sort by date (newest first) and limit
        all_articles.sort(key=lambda a: a.published_at, reverse=True)

        # Limit per category for balance
        categorized = {}
        for cat in ["breaking", "research", "industry", "tools"]:
            categorized[cat] = [a for a in all_articles if a.category == cat]

        final_articles = []
        for cat in ["breaking", "research", "industry", "tools"]:
            final_articles.extend(categorized[cat][:15])  # Max 15 per category

        # Add remaining to fill up to 50 total
        remaining = [a for a in all_articles if a not in final_articles]
        final_articles.extend(remaining[:50 - len(final_articles)])

        print(f"\n✅ Total articles collected: {len(final_articles)}")
        for cat in ["breaking", "research", "industry", "tools"]:
            count = len([a for a in final_articles if a.category == cat])
            print(f"   {cat.capitalize()}: {count}")

        self.articles = final_articles
        return final_articles


def init_database(db_path: Path):
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            summary TEXT,
            category TEXT NOT NULL,
            published_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            author TEXT,
            tags TEXT,  -- JSON array
            date_key TEXT NOT NULL,  -- YYYY-MM-DD for date-based queries
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date_key ON articles(date_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON articles(category)")
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
                (title, url, source, summary, category, published_at, fetched_at, author, tags, date_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    # Get all unique dates with article counts
    cursor.execute("""
        SELECT date_key, COUNT(*) as count,
               SUM(CASE WHEN category='breaking' THEN 1 ELSE 0 END) as breaking,
               SUM(CASE WHEN category='research' THEN 1 ELSE 0 END) as research,
               SUM(CASE WHEN category='industry' THEN 1 ELSE 0 END) as industry,
               SUM(CASE WHEN category='tools' THEN 1 ELSE 0 END) as tools
        FROM articles
        GROUP BY date_key
        ORDER BY date_key DESC
    """)
    dates = cursor.fetchall()

    # Get articles for each date (limited for archive page)
    archive = []
    for row in dates:
        date_key = row[0]
        cursor.execute("""
            SELECT title, url, source, summary, category, published_at, author, tags
            FROM articles
            WHERE date_key = ?
            ORDER BY
                CASE category WHEN 'breaking' THEN 0 WHEN 'research' THEN 1 WHEN 'industry' THEN 2 WHEN 'tools' THEN 3 ELSE 4 END,
                published_at DESC
            LIMIT 20
        """, (date_key,))
        articles = cursor.fetchall()

        archive.append({
            "date": date_key,
            "total": row[1],
            "categories": {
                "breaking": row[2],
                "research": row[3],
                "industry": row[4],
                "tools": row[5],
            },
            "articles": [
                {
                    "title": a[0],
                    "url": a[1],
                    "source": a[2],
                    "summary": a[3],
                    "category": a[4],
                    "published_at": a[5],
                    "author": a[6],
                    "tags": json.loads(a[7]) if a[7] else [],
                }
                for a in articles
            ]
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
            "categories": {
                "breaking": [a.to_dict() for a in articles if a.category == "breaking"],
                "research": [a.to_dict() for a in articles if a.category == "research"],
                "industry": [a.to_dict() for a in articles if a.category == "industry"],
                "tools": [a.to_dict() for a in articles if a.category == "tools"],
            },
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