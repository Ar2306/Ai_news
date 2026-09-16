#!/usr/bin/env python3
"""
AI Newsletter Fetcher v2 - Fast, reliable, parallel fetching with batch LLM summarization.
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
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from collections import defaultdict

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ─── Config ──────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "newsletter.json"
DB_FILE = DATA_DIR / "newsletter.db"
SOURCES_FILE = Path(__file__).parent.parent / "sources.json"

LLM_MODEL = os.environ.get("HERMES_MODEL", "nemotron-3-ultra-free")
LLM_PROVIDER = os.environ.get("HERMES_PROVIDER", "opencode-zen")

# ─── Validated Working Sources ──────────────────────────────────────────
RSS_FEEDS = {
    "NVIDIA Blog": "https://blogs.nvidia.com/feed/",
    "Together AI Blog": "https://www.together.ai/blog/rss.xml",
    "Hugging Face Blog": "https://huggingface.co/blog/feed.xml",
    "LangChain Blog": "https://blog.langchain.dev/rss/",
}

REDDIT_FEEDS = {
    "r/MachineLearning": "https://www.reddit.com/r/MachineLearning/.rss",
    "r/LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/.rss",
}

# New reliable sources
HACKER_NEWS_API = "https://hn.algolia.com/api/v1/search_by_date"
DEVTO_API = "https://dev.to/api/articles"
PAPERS_WITH_CODE_API = "https://paperswithcode.com/api/v1/papers"
ARXIV_API = "http://export.arxiv.org/api/query"

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
]
TWITTER_ACCOUNTS = ["HuggingFace", "OpenAI", "AnthropicAI", "GoogleAI", "MetaAI"]

# ─── Categorization ─────────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "breaking": ["breaking", "announced", "launch", "release", "unveil", "introduce", "new model", "new version", "available now", "announcing", "introducing", "preview", "beta", "ga", "general availability", "open source", "released"],
    "research": ["paper", "research", "arxiv", "study", "experiment", "benchmark", "evaluation", "analysis", "theoretical", "proof", "algorithm", "method", "approach", "framework", "architecture", "training", "pretrain", "fine-tun", "scaling law", "emergent", "capability", "alignment", "interpretability", "robustness", "generalization", "neurips", "icml", "iclr", "cvpr", "iccv", "acl", "emnlp", "conference", "proceedings", "accepted", "published", "preprint"],
    "industry": ["partnership", "acquisition", "funding", "investment", "series", "ipo", "valuation", "billion", "million", "startup", "enterprise", "deployment", "production", "infrastructure", "cloud", "api", "platform", "commercial", "business", "revenue", "customer", "adoption", "regulation", "policy", "law", "government", "copyright", "lawsuit", "legal", "ethics", "safety", "responsible ai", "governance"],
    "tools": ["tool", "library", "framework", "sdk", "api", "cli", "gui", "release", "version", "update", "feature", "plugin", "extension", "integration", "wrapper", "binding", "docker", "kubernetes", "inference", "serving", "quantization", "gguf", "llama.cpp", "vllm", "tensorrt", "onnx", "transformers", "accelerate", "peft", "trl", "langchain", "llamaindex", "haystack", "chroma", "weaviate", "pinecone", "milvus", "qdrant", "redis", "vector database", "embedding", "rerank", "rag", "agent", "workflow", "orchestration", "autogen", "crewai", "langgraph"],
}

SOURCE_DEFAULT_CATEGORY = {
    "NVIDIA": "industry", "Together AI": "industry", "Hugging Face": "tools",
    "LangChain": "tools", "Reddit": "research", "Hacker News": "breaking",
    "Twitter": "breaking", "Dev.to": "tools", "Papers with Code": "research",
    "arXiv": "research",
}

# ─── Data Models ────────────────────────────────────────────────────────
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

    def to_dict(self): return asdict(self)
    def to_db_tuple(self):
        return (self.title, self.url, self.source, self.summary, self.category,
                self.published_at, self.fetched_at, self.author, json.dumps(self.tags))

# ─── NewsFetcher v2 ─────────────────────────────────────────────────────
class NewsFetcher:
    def __init__(self, max_concurrent=5):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": "AI-Newsletter-Bot/2.0"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
        )
        self.articles: list[Article] = []
        self.seen_urls: set[str] = set()
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.failed_sources = defaultdict(int)

    async def __aenter__(self): return self
    async def __aexit__(self, *args): await self.client.aclose()

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        clean_query = "&".join(p for p in parsed.query.split("&")
                               if not p.startswith(("utm_", "ref", "source", "medium", "campaign", "fbclid", "gclid")))
        return parsed._replace(query=clean_query, fragment="").geturl().rstrip("/")

    def _is_duplicate(self, url: str) -> bool:
        norm = self._normalize_url(url)
        if norm in self.seen_urls: return True
        self.seen_urls.add(norm)
        return False

    def _parse_date(self, date_str: str) -> str:
        if not date_str: return datetime.now(timezone.utc).isoformat()
        try:
            dt = dateparser.parse(date_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception: return datetime.now(timezone.utc).isoformat()

    def _is_recent(self, date_str: str, days: int = 3) -> bool:
        try:
            dt = dateparser.parse(date_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt >= datetime.now(timezone.utc) - timedelta(days=days)
        except Exception: return True

    def _categorize(self, title: str, summary: str, source: str) -> str:
        text = f"{title} {summary}".lower()
        scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
        for cat, kws in CATEGORY_KEYWORDS.items():
            for kw in kws:
                if kw.lower() in text: scores[cat] += 1
        src_l = source.lower()
        for sk, cat in SOURCE_DEFAULT_CATEGORY.items():
            if sk.lower() in src_l: scores[cat] = scores.get(cat, 0) + 2; break
        return max(scores, key=scores.get) if max(scores.values()) > 0 else "industry"

    def _clean_html(self, html: str) -> str:
        if not html: return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]): tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    # ─── Batch LLM Summarization ────────────────────────────────────────
    async def _summarize_batch(self, items: list[tuple[str, str, str]], max_len: int = 180) -> list[str]:
        """Summarize multiple articles in one LLM call."""
        if not items: return []
        # Filter out items with no content
        valid = [(t, c, s) for t, c, s in items if c and len(c) >= 100]
        if not valid: return [""] * len(items)

        # Build batch prompt
        prompt_parts = [f"Summarize each AI news article in 1-2 sentences (max {max_len} chars each).\n"]
        for i, (title, content, source) in enumerate(valid):
            prompt_parts.append(f"\n--- Article {i+1} ---\nTitle: {title}\nSource: {source}\nContent: {content[:2000]}")
        prompt_parts.append("\n\nReturn JSON array of summaries in order:")
        prompt = "".join(prompt_parts)

        try:
            import subprocess
            result = subprocess.run(
                ["hermes", "run", "--model", LLM_MODEL, "--provider", LLM_PROVIDER,
                 "--prompt", prompt, "--max-tokens", str(max_len * len(valid) + 100),
                 "--temperature", "0.2"],
                capture_output=True, text=True, timeout=90
            )
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                # Extract JSON array
                match = re.search(r'\[.*\]', output, re.DOTALL)
                if match:
                    summaries = json.loads(match.group())
                    # Map back to original positions
                    out = []
                    idx = 0
                    for t, c, s in items:
                        if c and len(c) >= 100:
                            out.append(summaries[idx][:max_len] if idx < len(summaries) else "")
                            idx += 1
                        else:
                            out.append(c[:max_len] + "..." if len(c) > max_len else c)
                    return out
        except Exception as e:
            print(f"  ⚠ Batch LLM failed: {e}")

        # Fallback: extractive
        return [c[:max_len] + "..." if len(c) > max_len else c for _, c, _ in items]

    # ─── Source Fetchers (Parallel) ─────────────────────────────────────
    async def _fetch_with_sem(self, coro):
        async with self.semaphore:
            return await coro

    async def fetch_rss_feed(self, name: str, url: str) -> list[Article]:
        articles = []
        try:
            print(f"  📡 RSS: {name}...")
            resp = await self.client.get(url)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:15]:
                link = entry.get("link", "")
                if not link or self._is_duplicate(link): continue
                title = entry.get("title", "").strip()
                if not title: continue
                published = next((entry.get(k, "") for k in ["published", "updated", "created", "pubDate"] if k in entry), "")
                pub_iso = self._parse_date(published)
                if not self._is_recent(pub_iso, days=3): continue
                content = ""
                for k in ["summary", "description", "content", "contentSnippet"]:
                    if k in entry:
                        v = entry[k]
                        if isinstance(v, list) and v: v = v[0].get("value", "")
                        content = self._clean_html(str(v))
                        if content: break
                author = entry.get("author", "") or (entry.get("authors", [{}])[0].get("name", "") if entry.get("authors") else "")
                tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")][:5]
                cat = self._categorize(title, content, name)
                articles.append(Article(
                    title=title,
                    url=link,
                    source=name,
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=author,
                    tags=tags
                ))
            print(f"    ✅ {len(articles)} from {name}")
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            self.failed_sources[name] += 1
        return articles

    async def fetch_reddit(self, name: str, url: str) -> list[Article]:
        articles = []
        try:
            print(f"  📡 Reddit: {name}...")
            resp = await self.client.get(url)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:15]:
                link = entry.get("link", "")
                if not link or self._is_duplicate(link): continue
                title = entry.get("title", "").strip()
                if not title: continue
                m = re.match(r"\[(r/[\w]+)\]", title)
                if m: title = title[m.end():].strip()
                content = self._clean_html(entry.get("summary", "") or entry.get("description", ""))
                pub = entry.get("published", "") or entry.get("updated", "")
                pub_iso = self._parse_date(pub)
                if not self._is_recent(pub_iso, days=2): continue
                cat = self._categorize(title, content, name)
                articles.append(Article(
                    title=title,
                    url=link,
                    source=name,
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=entry.get("author", ""),
                    tags=["reddit"]
                ))
            print(f"    ✅ {len(articles)} from {name}")
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            self.failed_sources[name] += 1
        return articles

    async def fetch_hacker_news(self) -> list[Article]:
        articles = []
        try:
            print("  📡 Hacker News...")
            tags = ["machine-learning", "artificial-intelligence", "llm", "gpt", "ai"]
            cutoff = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
            seen_ids = set()
            for tag in tags:
                params = {"tags": tag, "numericFilters": f"created_at_i>={cutoff}", "hitsPerPage": 15}
                resp = await self.client.get(HACKER_NEWS_API, params=params)
                resp.raise_for_status()
                for hit in resp.json().get("hits", []):
                    oid = hit.get("objectID")
                    if oid in seen_ids: continue
                    seen_ids.add(oid)
                    url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
                    if not url or self._is_duplicate(url): continue
                    title = hit.get("title", "").strip()
                    if not title: continue
                    content = hit.get("story_text", "") or ""
                    if not content and hit.get("url"):
                        try:
                            r = await self.client.get(hit["url"], timeout=8.0)
                            content = self._clean_html(r.text)[:1500]
                        except Exception: pass
                    pub_iso = datetime.fromtimestamp(hit.get("created_at_i", 0), tz=timezone.utc).isoformat()
                    if not self._is_recent(pub_iso, days=2): continue
                    cat = self._categorize(title, content, "Hacker News")
                    articles.append(Article(
                    title=title,
                    url=url,
                    source="Hacker News",
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=hit.get("author", ""),
                    tags=["hacker-news", hit.get("tag", "")]
                ))
            print(f"    ✅ {len(articles)} from Hacker News")
        except Exception as e:
            print(f"  ❌ Hacker News: {e}")
        return articles

    async def fetch_devto(self) -> list[Article]:
        articles = []
        try:
            print("  📡 Dev.to...")
            params = {"tag": "ai,machinelearning,deeplearning", "per_page": 20}
            resp = await self.client.get(DEVTO_API, params=params)
            resp.raise_for_status()
            for item in resp.json():
                url = item.get("url", "")
                if not url or self._is_duplicate(url): continue
                title = item.get("title", "").strip()
                if not title: continue
                content = self._clean_html(item.get("description", "") or item.get("body_markdown", "")[:2000])
                pub_iso = self._parse_date(item.get("published_at", ""))
                if not self._is_recent(pub_iso, days=3): continue
                cat = self._categorize(title, content, "Dev.to")
                articles.append(Article(
                    title=title,
                    url=url,
                    source="Dev.to",
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=item.get("user", {}).get("name", ""),
                    tags=item.get("tags", [])
                ))
            print(f"    ✅ {len(articles)} from Dev.to")
        except Exception as e:
            print(f"  ❌ Dev.to: {e}")
        return articles

    async def fetch_papers_with_code(self) -> list[Article]:
        articles = []
        try:
            print("  📡 Papers with Code...")
            params = {"page": 1, "page_size": 20, "ordering": "-published"}
            resp = await self.client.get(PAPERS_WITH_CODE_API, params=params)
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                url = item.get("url_abs") or item.get("url_pdf") or ""
                if not url or self._is_duplicate(url): continue
                title = item.get("title", "").strip()
                if not title: continue
                content = item.get("abstract", "")[:2000]
                pub_iso = self._parse_date(item.get("published", ""))
                if not self._is_recent(pub_iso, days=5): continue
                cat = self._categorize(title, content, "Papers with Code")
                articles.append(Article(
                    title=title,
                    url=url,
                    source="Papers with Code",
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author="",
                    tags=["paper"]
                ))
            print(f"    ✅ {len(articles)} from Papers with Code")
        except Exception as e:
            print(f"  ❌ Papers with Code: {e}")
        return articles

    async def fetch_arxiv(self) -> list[Article]:
        articles = []
        try:
            print("  📡 arXiv (AI/ML)...")
            # Search recent AI/ML papers
            query = "cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CV+OR+cat:cs.CL+AND+submittedDate:[20240720+TO+*]"
            params = {"search_query": query, "start": 0, "max_results": 20, "sortBy": "submittedDate", "sortOrder": "descending"}
            resp = await self.client.get(ARXIV_API, params=params)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                link = entry.get("link", "")
                if not link or self._is_duplicate(link): continue
                title = entry.get("title", "").strip()
                if not title: continue
                content = self._clean_html(entry.get("summary", ""))
                pub_iso = self._parse_date(entry.get("published", ""))
                if not self._is_recent(pub_iso, days=5): continue
                cat = self._categorize(title, content, "arXiv")
                authors = [a.get("name", "") for a in entry.get("authors", [])]
                articles.append(Article(
                    title=title,
                    url=link,
                    source="arXiv",
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=", ".join(authors[:3]),
                    tags=["arxiv"]
                ))
            print(f"    ✅ {len(articles)} from arXiv")
        except Exception as e:
            print(f"  ❌ arXiv: {e}")
        return articles

    async def fetch_nitter(self, username: str) -> list[Article]:
        articles = []
        for inst in NITTER_INSTANCES:
            try:
                url = f"{inst}/{username}/rss"
                resp = await self.client.get(url, timeout=12.0)
                if resp.status_code != 200: continue
                feed = feedparser.parse(resp.content)
                for entry in feed.entries[:8]:
                    link = entry.get("link", "")
                    if not link or self._is_duplicate(link): continue
                    title = entry.get("title", "").strip()
                    if not title: continue
                    content = self._clean_html(entry.get("summary", "") or entry.get("description", ""))
                    pub = entry.get("published", "") or entry.get("updated", "")
                    pub_iso = self._parse_date(pub)
                    if not self._is_recent(pub_iso, days=1): continue
                    if title.startswith(("RT @", "@")) and len(content) < 80: continue
                    cat = self._categorize(title, content, f"Twitter/@{username}")
                    articles.append(Article(
                    title=title[:200],
                    url=link,
                    source=f"Twitter/@{username}",
                    summary="",
                    category=cat,
                    published_at=pub_iso,
                    author=username,
                    tags=["twitter", username.lower()]
                ))
                if articles: break
            except Exception: continue
        return articles

    # ─── Main Orchestration ──────────────────────────────────────────────
    async def fetch_all(self) -> list[Article]:
        print("\n🚀 Starting parallel fetch...\n")
        start = time.time()

        # Phase 1: Parallel fetch all sources
        tasks = [
            self._fetch_with_sem(self.fetch_rss_feed(n, u)) for n, u in RSS_FEEDS.items()
        ] + [
            self._fetch_with_sem(self.fetch_reddit(n, u)) for n, u in REDDIT_FEEDS.items()
        ] + [
            self._fetch_with_sem(self.fetch_hacker_news()),
            self._fetch_with_sem(self.fetch_devto()),
            self._fetch_with_sem(self.fetch_papers_with_code()),
            self._fetch_with_sem(self.fetch_arxiv()),
        ] + [
            self._fetch_with_sem(self.fetch_nitter(u)) for u in TWITTER_ACCOUNTS[:5]
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_articles = []
        for r in results:
            if isinstance(r, list): all_articles.extend(r)
            elif isinstance(r, Exception): print(f"  ❌ Task failed: {r}")

        # Phase 2: Batch LLM summarization
        print(f"\n📝 Summarizing {len(all_articles)} articles in batches...")
        batch_size = 10
        for i in range(0, len(all_articles), batch_size):
            batch = all_articles[i:i+batch_size]
            summaries = await self._summarize_batch([(a.title, a.summary or self._clean_html(""), a.source) for a in batch])
            for a, s in zip(batch, summaries):
                a.summary = s

        # Phase 3: Deduplicate, categorize, limit
        all_articles.sort(key=lambda a: a.published_at, reverse=True)
        cats = defaultdict(list)
        for a in all_articles:
            cats[a.category].append(a)

        final = []
        for cat in ["breaking", "research", "industry", "tools"]:
            final.extend(cats[cat][:12])

        print(f"\n✅ Done in {time.time()-start:.1f}s | Total: {len(final)}")
        for cat in ["breaking", "research", "industry", "tools"]:
            print(f"   {cat}: {len([a for a in final if a.category==cat])}")
        if self.failed_sources:
            print(f"   Failed: {dict(self.failed_sources)}")

        self.articles = final
        return final


# ─── Database ────────────────────────────────────────────────────────────
def init_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, url TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
            summary TEXT, category TEXT NOT NULL, published_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL, author TEXT, tags TEXT,
            date_key TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
        )""")
    for idx in ["idx_date_key", "idx_category", "idx_published_at", "idx_source"]:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON articles({idx.replace('idx_', '')})")
    conn.commit()
    return conn

def save_db(conn, articles, date_key):
    cur = conn.cursor()
    saved = 0
    for a in articles:
        try:
            cur.execute("INSERT OR IGNORE INTO articles (title,url,source,summary,category,published_at,fetched_at,author,tags,date_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        a.to_db_tuple() + (date_key,))
            if cur.rowcount: saved += 1
        except Exception as e:
            print(f"  ⚠ DB: {e}")
    conn.commit()
    return saved

def build_archive(conn, out_path):
    cur = conn.cursor()
    cur.execute("""
        SELECT date_key, COUNT(*),
               SUM(category='breaking'), SUM(category='research'),
               SUM(category='industry'), SUM(category='tools')
        FROM articles GROUP BY date_key ORDER BY date_key DESC
    """)
    archive = []
    for dk, tot, b, r, i, t in cur.fetchall():
        cur.execute("""SELECT title,url,source,summary,category,published_at,author,tags
                       FROM articles WHERE date_key=? ORDER BY
                       CASE category WHEN 'breaking' THEN 0 WHEN 'research' THEN 1 WHEN 'industry' THEN 2 WHEN 'tools' THEN 3 ELSE 4 END,
                       published_at DESC LIMIT 20""", (dk,))
        arts = cur.fetchall()
        archive.append({"date": dk, "total": tot,
            "categories": {"breaking": b, "research": r, "industry": i, "tools": t},
            "articles": [{"title":a[0],"url":a[1],"source":a[2],"summary":a[3],"category":a[4],"published_at":a[5],"author":a[6],"tags":json.loads(a[7]) if a[7] else []} for a in arts]})
    with open(out_path, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "archive": archive}, f, indent=2)
    print(f"📚 Archive: {len(archive)} days → {out_path}")


# ─── Main ────────────────────────────────────────────────────────────────
async def main():
    print("="*60); print("🤖 AI Newsletter Fetcher v2"); print("="*60)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db(DB_FILE)

    async with NewsFetcher(max_concurrent=6) as fetcher:
        articles = await fetcher.fetch_all()
        dk = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        saved = save_db(conn, articles, dk)
        print(f"💾 DB: {saved} new → {DB_FILE}")
        build_archive(conn, DATA_DIR / "archive.json")

        data = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "article_count": len(articles),
                "categories": {c: [a.to_dict() for a in articles if a.category==c] for c in ["breaking","research","industry","tools"]},
                "all_articles": [a.to_dict() for a in articles]}
        with open(OUTPUT_FILE, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"💾 JSON → {OUTPUT_FILE}")

    conn.close()
    return 0

if __name__ == "__main__":
    try: sys.exit(asyncio.run(main()))
    except KeyboardInterrupt: sys.exit(130)
    except Exception as e:
        print(f"❌ {e}"); import traceback; traceback.print_exc(); sys.exit(1)