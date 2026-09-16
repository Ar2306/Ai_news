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

# LLM Configuration. Providers are tried in order for every item:
#   1. Gemini (GEMINI_API_KEY)        2. Ollama Cloud (OLLAMA_API_KEY)
#   3. extractive summary + keyword tags (always works, no key needed)
# A provider without a key is skipped; one that keeps failing is switched off for the run.
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
# Free-tier models confirmed by the "Check API keys" workflow (2026-09-16), tried in order.
# Each model has its own free daily quota, so when one runs out the next takes over.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-flash-latest").split(",") if m.strip()]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "6.5"))  # free tier ≈ 10 requests/min

OLLAMA_API_KEY_ENV = "OLLAMA_API_KEY"
# Free-plan Ollama Cloud models confirmed by "Check API keys" (Qwen models need a paid plan).
# If these ever stop working (HTTP 402/404), the rest of the cloud catalogue is tried automatically.
OLLAMA_MODELS = [m.strip() for m in os.environ.get(
    "OLLAMA_MODELS", "gemma4:31b,gpt-oss:120b,nemotron-3-super,gpt-oss:20b").split(",") if m.strip()]
OLLAMA_BASE = "https://ollama.com/api"
OLLAMA_MIN_INTERVAL = float(os.environ.get("OLLAMA_MIN_INTERVAL", "1.0"))

LLM_TIMEOUT = 90.0
LLM_MAX_RETRIES = 3          # retries on 429 / 5xx / network errors, honouring retry hints
LLM_MAX_BACKOFF = 60.0       # never wait longer than this for a single retry
LLM_DISABLE_AFTER = 3        # consecutive failures before a provider is skipped for the run

SITE_URL = os.environ.get("SITE_URL", "https://ai-news-blush.vercel.app")
STATUS_FILE = DATA_DIR / "status.json"
FEED_FILE = DATA_DIR / "feed.xml"
USER_AGENT = "Mozilla/5.0 (compatible; AR-digest/1.0; +https://ai-news-blush.vercel.app)"

# Source groups keep the digest balanced: candidates are picked round-robin across
# groups (each capped), so one prolific feed like arXiv can't crowd out the rest.
# Order here is also dedup priority: when the same story appears twice, the earlier
# group's copy is kept and later copies (e.g. a Reddit/HN thread) become discussion links.
GROUPS = {
    #  group        candidate cap   recency (days)
    "papers":    {"cap": 14, "days": 3},   # Hugging Face daily papers (upvotes, code links)
    "labs":      {"cap": 18, "days": 4},   # AI lab & company blogs
    "arxiv":     {"cap": 14, "days": 2},
    "blogs":     {"cap": 10, "days": 5},   # researchers & newsletters
    "news":      {"cap": 12, "days": 2},
    "medium":    {"cap": 12, "days": 2},
    "community": {"cap": 10, "days": 2},   # Hacker News
    "reddit":    {"cap": 12, "days": 2},
}
PER_SOURCE_CAP = 4  # max candidates from any single feed

# RSS/Atom feeds, verified working 2026-09-16. Broken ones (Anthropic, Meta AI, Cohere,
# LangChain, W&B, Replicate, AssemblyAI, old Google AI blog, Nitter/Twitter) were removed.
RSS_SOURCES = [
    # arXiv
    ("arXiv cs.AI", "arxiv", "https://export.arxiv.org/rss/cs.AI"),
    ("arXiv cs.LG", "arxiv", "https://export.arxiv.org/rss/cs.LG"),
    ("arXiv cs.CL", "arxiv", "https://export.arxiv.org/rss/cs.CL"),
    ("arXiv cs.CV", "arxiv", "https://export.arxiv.org/rss/cs.CV"),
    ("arXiv cs.RO", "arxiv", "https://export.arxiv.org/rss/cs.RO"),
    # AI labs & companies
    ("OpenAI", "labs", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "labs", "https://deepmind.google/blog/rss.xml"),
    ("Google Research", "labs", "https://research.google/blog/rss/"),
    ("Google AI", "labs", "https://blog.google/technology/ai/rss/"),
    ("Microsoft Research", "labs", "https://www.microsoft.com/en-us/research/blog/feed/"),
    ("NVIDIA Blog", "labs", "https://blogs.nvidia.com/feed/"),
    ("NVIDIA Technical Blog", "labs", "https://developer.nvidia.com/blog/feed"),
    ("Apple ML Research", "labs", "https://machinelearning.apple.com/rss.xml"),
    ("AWS Machine Learning", "labs", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("Hugging Face Blog", "labs", "https://huggingface.co/blog/feed.xml"),
    ("Mistral AI", "labs", "https://mistral.ai/rss.xml"),
    ("Allen AI", "labs", "https://allenai.org/rss.xml"),
    ("Together AI", "labs", "https://www.together.ai/blog/rss.xml"),
    ("Meta Engineering (ML)", "labs", "https://engineering.fb.com/category/ml-applications/feed/"),
    ("MIT News (AI)", "labs", "https://news.mit.edu/rss/topic/artificial-intelligence2"),
    ("Stanford AI Lab", "labs", "https://ai.stanford.edu/blog/feed.xml"),
    # researchers & newsletters
    ("Simon Willison", "blogs", "https://simonwillison.net/atom/everything/"),
    ("Latent Space", "blogs", "https://www.latent.space/feed"),
    ("Interconnects", "blogs", "https://www.interconnects.ai/feed"),
    ("Import AI", "blogs", "https://importai.substack.com/feed"),
    ("Sebastian Raschka", "blogs", "https://magazine.sebastianraschka.com/feed"),
    ("Lil'Log", "blogs", "https://lilianweng.github.io/index.xml"),
    ("Chip Huyen", "blogs", "https://huyenchip.com/feed.xml"),
    ("Eugene Yan", "blogs", "https://eugeneyan.com/rss/"),
    ("Hamel Husain", "blogs", "https://hamel.dev/index.xml"),
    ("Andrej Karpathy", "blogs", "https://karpathy.bearblog.dev/feed/"),
    ("The Gradient", "blogs", "https://thegradient.pub/rss/"),
    # tech news
    ("TechCrunch AI", "news", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "news", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Ars Technica AI", "news", "https://arstechnica.com/ai/feed/"),
    ("MIT Technology Review AI", "news", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    # Medium
    ("Medium · AI", "medium", "https://medium.com/feed/tag/artificial-intelligence"),
    ("Medium · Machine Learning", "medium", "https://medium.com/feed/tag/machine-learning"),
    ("Medium · LLM", "medium", "https://medium.com/feed/tag/llm"),
    ("Medium · Deep Learning", "medium", "https://medium.com/feed/tag/deep-learning"),
    ("Medium · Generative AI", "medium", "https://medium.com/feed/tag/generative-ai"),
    ("Medium · Reinforcement Learning", "medium", "https://medium.com/feed/tag/reinforcement-learning"),
    ("Towards Data Science", "medium", "https://towardsdatascience.com/feed"),
    ("Data Science Collective", "medium", "https://medium.com/feed/data-science-collective"),
]

# Fetched together in a single request (top posts of the day).
REDDIT_SUBS = [
    "MachineLearning", "LocalLLaMA", "artificial", "singularity", "OpenAI",
    "reinforcementlearning", "deeplearning", "StableDiffusion", "robotics", "computervision",
]

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_QUERIES = ["AI", "LLM", "GPT", "machine learning", "OpenAI", "Anthropic", "Gemini", "neural network", "model"]
HN_MIN_POINTS = 40

HF_PAPERS_API = "https://huggingface.co/api/daily_papers"

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
MAX_LLM_ITEMS = int(os.environ.get("MAX_LLM_ITEMS", "100"))  # candidates enriched per run

CURATOR_PROMPT = """You are a technical curator for a personal AI/ML research digest called "AR."

Given the title and abstract/content of one item, do four things:

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

4. IMPORTANCE: Rate 1-5 how much a busy AI/ML researcher should care:
   5 = major release or landmark result (new frontier model, field-changing paper)
   4 = significant, widely relevant work from a credible source
   3 = solid, useful but incremental
   2 = niche or minor
   1 = low-signal (beginner tutorial, opinion without substance)

Return ONLY valid JSON, no markdown fences, no preamble:
{
  "relevant": true,
  "tags": ["llm", "training-infra"],
  "summary": {"what": "...", "why": "...", "who": "..."},
  "importance": 3
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


def short_authors(author: str) -> str:
    """'A, B, C, D' -> 'A, B, C et al.' so long arXiv author lists stay one line."""
    names = [n.strip() for n in re.split(r",|;| and ", author) if n.strip()]
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "") if names else "Unknown"


ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")
GITHUB_REPO = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)")


def paper_links(url: str, content: str = "") -> dict:
    """PDF link for arXiv papers and the first GitHub repo mentioned in the abstract."""
    links = {}
    m = ARXIV_ID.search(url or "")
    if m:
        links["pdf"] = f"https://arxiv.org/pdf/{m.group(1)}"
    g = GITHUB_REPO.search(content or "")
    if g:
        links["code"] = "https://github.com/" + g.group(1).rstrip(".")
    return links


def signal_importance(article: "Article") -> int:
    """Importance floor from community signals (upvotes, points)."""
    if article.group == "papers" and article.signal >= 50 or article.group == "community" and article.signal >= 300:
        return 4
    if article.group == "papers" and article.signal >= 15 or article.group == "community" and article.signal >= 120:
        return 3
    return 1


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
    try:
        importance = min(5, max(1, int(round(float(data.get("importance", 3))))))
    except (TypeError, ValueError):
        importance = 3
    return {"relevant": True, "tags": tags, "summary": clean, "importance": importance}


class LLMError(RuntimeError):
    """fatal=True means retrying this provider is pointless for the rest of the run
    (bad key, no access, daily quota used up)."""

    def __init__(self, message: str, fatal: bool = False, status: int = 0):
        super().__init__(message)
        self.fatal = fatal
        self.status = status


def check_response(resp: httpx.Response) -> None:
    if resp.status_code == 200:
        return
    msg = error_message(resp)
    fatal = (
        resp.status_code in (401, 403)
        or "API_KEY_INVALID" in resp.text
        or "api key not valid" in msg.lower()
        or (resp.status_code == 429 and re.search(r"per ?day|daily", resp.text, re.I) is not None)
    )
    raise LLMError(f"HTTP {resp.status_code}: {msg}", fatal=fatal, status=resp.status_code)


class LLMProvider:
    """One LLM backend. Subclasses implement _request(prompt) -> text."""

    name = "llm"
    min_interval = 0.0

    def __init__(self, http: httpx.Client, api_key: str):
        self.http = http
        self.api_key = api_key
        self.failures = 0
        self.disabled = False
        self._last_call = 0.0

    models: list[str] = []
    model_index = 0

    @property
    def model(self) -> str:
        return self.models[self.model_index] if self.models else ""

    def _switchable(self, e: "LLMError") -> bool:
        """Errors that are specific to the current model (so another model may work)."""
        return False

    def _more_models(self) -> bool:
        return self.model_index + 1 < len(self.models)

    def _request(self, prompt: str) -> str:
        while True:
            try:
                return self._request_model(prompt)
            except LLMError as e:
                if self._switchable(e) and self._more_models():
                    nxt = self.models[self.model_index + 1]
                    print(f"    ↻ {self.name} {self.model} unavailable ({str(e)[:110]}); trying {nxt}")
                    self.model_index += 1
                    continue
                raise

    def generate(self, prompt: str) -> str:
        # Pace requests to stay under free-tier per-minute limits
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            return self._request(prompt)
        finally:
            self._last_call = time.monotonic()

    def _post(self, url: str, **kwargs) -> httpx.Response:
        """POST with retry-with-backoff on 429, 5xx and network errors."""
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                resp = self.http.post(url, **kwargs)
            except httpx.TransportError as e:
                if attempt == LLM_MAX_RETRIES:
                    raise LLMError(f"network error: {e}") from e
                time.sleep(min(2 ** attempt, LLM_MAX_BACKOFF))
                continue
            if resp.status_code == 429 and re.search(r"per ?day|daily", resp.text, re.I):
                check_response(resp)  # daily quota gone: waiting won't help
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == LLM_MAX_RETRIES:
                    break
                time.sleep(min(retry_after_seconds(resp, default=2 ** (attempt + 1)), LLM_MAX_BACKOFF))
                continue
            return resp
        raise LLMError(f"HTTP {resp.status_code} after {LLM_MAX_RETRIES} retries: {error_message(resp)}", status=resp.status_code)


class GeminiProvider(LLMProvider):
    name = "Gemini"
    min_interval = GEMINI_MIN_INTERVAL

    def __init__(self, http, api_key, models=None):
        super().__init__(http, api_key)
        self.models = list(models or GEMINI_MODELS)
        self.model_index = 0

    def _switchable(self, e):
        if e.status == 404:
            # Google names the replacement ("...use models/gemini-3.5-flash-lite..."): queue it next
            m = re.search(r"use models/([\w.-]+)", str(e))
            if m and m.group(1) not in self.models[: self.model_index + 1]:
                if m.group(1) in self.models:
                    self.models.remove(m.group(1))
                self.models.insert(self.model_index + 1, m.group(1))
            return True
        return e.status == 429 and e.fatal  # no quota for this model on this key

    def _request_model(self, prompt: str) -> str:
        resp = self._post(
            f"{GEMINI_BASE}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 2048,  # headroom for models that think before answering
                    "responseMimeType": "application/json",
                },
            },
        )
        check_response(resp)
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"empty response ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise LLMError(f"empty text (finishReason={candidates[0].get('finishReason')})")
        return text


class OllamaProvider(LLMProvider):
    name = "Ollama Cloud"
    min_interval = OLLAMA_MIN_INTERVAL

    def __init__(self, http, api_key, models=None):
        super().__init__(http, api_key)
        self.models = list(models or OLLAMA_MODELS)
        self.model_index = 0
        self._catalogue_loaded = False

    def _switchable(self, e):
        # 402 = needs a paid plan/credits, 404 = model not in the catalogue
        return e.status in (402, 404)

    def _more_models(self) -> bool:
        if not super()._more_models() and not self._catalogue_loaded:
            self._catalogue_loaded = True
            self.models += [m for m in ollama_catalogue(self.http, self.api_key) if m not in self.models]
        return super()._more_models()

    def _request_model(self, prompt: str) -> str:
        resp = self._post(
            f"{OLLAMA_BASE}/chat",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            },
        )
        check_response(resp)
        text = ((resp.json().get("message") or {}).get("content") or "")
        if not text.strip():
            raise LLMError("empty response")
        return text


def ollama_catalogue(http: httpx.Client, api_key: str) -> list[str]:
    """Cloud model names, Qwen first, then small/open models before the giant ones."""
    try:
        resp = http.get(f"{OLLAMA_BASE}/tags", headers={"Authorization": f"Bearer {api_key}"})
        names = [m.get("name") or m.get("model") for m in resp.json().get("models", [])]
    except Exception:
        return []
    names = [n for n in names if n]
    rank = lambda n: (0 if n.startswith("qwen") else 1 if n.startswith(("gpt-oss", "gemma")) else 2, n)
    return sorted(names, key=rank)


def retry_after_seconds(resp: httpx.Response, default: float) -> float:
    """Honour Retry-After or Gemini's RetryInfo.retryDelay ("23s")."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay")
            if delay:
                return float(str(delay).rstrip("s"))
    except Exception:
        pass
    return default


def error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return str(msg or resp.text)[:200]
    except Exception:
        return resp.text[:200]


def build_providers(http: httpx.Client) -> list[LLMProvider]:
    providers: list[LLMProvider] = []
    if os.environ.get(GEMINI_API_KEY_ENV, "").strip():
        providers.append(GeminiProvider(http, os.environ[GEMINI_API_KEY_ENV].strip()))
    if os.environ.get(OLLAMA_API_KEY_ENV, "").strip():
        providers.append(OllamaProvider(http, os.environ[OLLAMA_API_KEY_ENV].strip()))
    return providers


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
    importance: int = 3
    group: str = ""
    links: dict = field(default_factory=dict)          # {"pdf", "code", "hf"}
    discussions: list = field(default_factory=list)    # [{"source", "url", "points"}]
    # internal only (never written to JSON)
    content: str = ""                                  # raw excerpt fed to the LLM
    signal: int = 0                                    # upvotes / points, for ranking
    external_url: str = ""                             # link target of a Reddit/HN post
    discussion: dict = field(default_factory=dict)     # this item's own thread, if it is one

    INTERNAL = ("content", "signal", "external_url", "discussion")

    def to_dict(self):
        d = asdict(self)
        for k in self.INTERNAL:
            d.pop(k)
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
            self.importance,
            self.group,
            json.dumps(self.links),
            json.dumps(self.discussions),
        )


class NewsFetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self.articles: list[Article] = []
        self.seen_urls: set[str] = set()
        self.seen_titles: set[str] = set()
        self.run_urls: dict[str, Article] = {}     # this run's kept articles, for attaching discussions
        self.run_titles: dict[str, Article] = {}
        self.source_status: dict[str, dict] = {}
        self.llm_stats: dict[str, int] = {}
        self.http = httpx.Client(timeout=LLM_TIMEOUT)
        self.providers = build_providers(self.http)
        if self.providers:
            print(f"🧠 LLM providers (in order): {', '.join(p.name for p in self.providers)}")
        else:
            print(f"⚠ Neither {GEMINI_API_KEY_ENV} nor {OLLAMA_API_KEY_ENV} is set — using extractive summaries + keyword tags")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()
        self.http.close()

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

    def _match_title(self, key: str) -> Optional[str]:
        """Return the already-seen title key that `key` duplicates, if any."""
        if key in self.seen_titles:
            return key
        for k in self.seen_titles:
            sm = SequenceMatcher(None, key, k)
            # Cheap upper bounds first; the full ratio() only runs for plausible matches
            if sm.real_quick_ratio() > 0.88 and sm.quick_ratio() > 0.88 and sm.ratio() > 0.88:
                return k
        return None

    def _is_title_duplicate(self, title: str) -> bool:
        """Cross-source near-duplicate detection by fuzzy title similarity."""
        key = self._normalize_title(title)
        if not key:
            return True  # too noisy to be useful
        if self._match_title(key):
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
            "importance": 2,
        }

    def curate(self, article: Article) -> Optional[Article]:
        """Classify + summarize one article. Returns None if it isn't AI/ML relevant."""
        prompt = (
            CURATOR_PROMPT
            .replace("{title}", article.title)
            .replace("{source_name}", article.source)
            .replace("{abstract_or_excerpt}", (article.content or "(no content)")[:3000])
        )
        result = None
        for provider in getattr(self, "providers", []):
            if provider.disabled:
                continue
            try:
                result = parse_curation(provider.generate(prompt))
                provider.failures = 0
                stats = getattr(self, "llm_stats", {})
                label = f"{provider.name} ({provider.model})"
                stats[label] = stats.get(label, 0) + 1
                break
            except Exception as e:
                provider.failures += 1
                print(f"    ⚠ {provider.name} failed ({type(e).__name__}: {str(e)[:160]})", file=sys.stderr)
                if getattr(e, "fatal", False) or provider.failures >= LLM_DISABLE_AFTER:
                    provider.disabled = True
                    print(f"    ⛔ {provider.name} disabled for the rest of this run", file=sys.stderr)
        if result is None:
            result = self._fallback_curation(article)
            stats = getattr(self, "llm_stats", None)
            if stats is not None:
                stats["extractive fallback"] = stats.get("extractive fallback", 0) + 1

        if not result["relevant"]:
            return None
        article.tags = result["tags"]
        article.summary = result["summary"]
        article.importance = max(result.get("importance", 2), signal_importance(article))
        if article.summary.get("who", "Unknown") == "Unknown" and article.author:
            article.summary["who"] = short_authors(article.author)
        return article

    def _status(self, name: str, group: str, items: int = 0, error: str = ""):
        self.source_status[name] = {"group": group, "ok": not error, "items": items, "error": error[:120]}

    def _entry_content(self, entry) -> str:
        for key in ["summary", "description", "content", "contentSnippet"]:
            if key in entry:
                val = entry[key]
                if isinstance(val, list) and val:
                    val = val[0].get("value", "")
                text = self._clean_html(str(val))
                if text:
                    return text
        return ""

    async def fetch_rss_feed(self, name: str, url: str, group: str = "labs") -> list[Article]:
        """Fetch and parse an RSS/Atom feed. Dedup happens later, across all sources."""
        articles = []
        days = GROUPS.get(group, {}).get("days", 3)
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:60]:
                link = entry.get("link", "")
                title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
                if not link or not title:
                    continue
                published = next((entry[k] for k in ["published", "updated", "created", "pubDate"] if k in entry), "")
                published_iso = self._parse_date(published)
                if not self._is_recent(published_iso, days=days):
                    continue
                content = self._entry_content(entry)
                author = entry.get("author", "") or (entry.get("authors", [{}])[0].get("name", "") if entry.get("authors") else "")
                articles.append(Article(
                    title=title, url=link, source=name, group=group, content=content,
                    published_at=published_iso, author=author, links=paper_links(link, content),
                ))
            self._status(name, group, len(articles))
            print(f"  ✅ {name}: {len(articles)}")
        except Exception as e:
            self._status(name, group, error=f"{type(e).__name__}: {e}")
            print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:100]}")
        return articles

    async def fetch_reddit(self) -> list[Article]:
        """Top posts of the day across all subreddits in ONE request (r/A+B+C), since
        Reddit rate-limits anonymous clients that make several requests in a row."""
        name = "Reddit"
        url = f"https://www.reddit.com/r/{'+'.join(REDDIT_SUBS)}/top/.rss?t=day&limit=100"
        articles = []
        try:
            response = await self.client.get(url)
            if response.status_code == 429:
                await asyncio.sleep(min(float(response.headers.get("retry-after", 15) or 15), 30))
                response = await self.client.get(url)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                permalink = entry.get("link", "")
                title = entry.get("title", "").strip()
                if not permalink or not title:
                    continue
                published_iso = self._parse_date(entry.get("published", "") or entry.get("updated", ""))
                if not self._is_recent(published_iso, days=GROUPS["reddit"]["days"]):
                    continue
                sub = ((entry.get("tags") or [{}])[0].get("term") or "").strip()
                source = f"r/{sub}" if sub else name
                raw = entry.get("summary", "") or entry.get("description", "")
                # Link posts carry the external URL as the "[link]" anchor
                external = ""
                for a_tag in BeautifulSoup(raw, "html.parser").find_all("a", href=True):
                    href = a_tag["href"]
                    if a_tag.get_text(strip=True) == "[link]" and "reddit.com" not in href and "redd.it" not in href:
                        external = href
                        break
                content = re.sub(r"submitted by\s+/u/\S+|\[link\]|\[comments\]", " ", self._clean_html(raw))
                articles.append(Article(
                    title=title, url=external or permalink, source=source, group="reddit",
                    content=content.strip(), published_at=published_iso,
                    author=(entry.get("author", "") or "").replace("/u/", ""),
                    external_url=external, links=paper_links(external, content),
                    discussion={"source": source, "url": permalink},
                ))
            self._status(name, "reddit", len(articles))
            print(f"  ✅ {name} ({len(REDDIT_SUBS)} subreddits): {len(articles)}")
        except Exception as e:
            self._status(name, "reddit", error=f"{type(e).__name__}: {e}")
            print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:100]}")
        return articles

    async def fetch_hacker_news(self) -> list[Article]:
        """AI/ML stories with real traction from Hacker News (Algolia search)."""
        name = "Hacker News"
        articles, seen_ids = [], set()
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=GROUPS["community"]["days"])).timestamp())
        try:
            for q in HN_QUERIES:
                response = await self.client.get(HN_API, params={
                    "query": q, "tags": "story", "hitsPerPage": 50,
                    "numericFilters": f"created_at_i>{cutoff},points>{HN_MIN_POINTS}",
                })
                response.raise_for_status()
                for hit in response.json().get("hits", []):
                    obj_id = hit.get("objectID")
                    title = (hit.get("title") or "").strip()
                    if not title or obj_id in seen_ids:
                        continue
                    seen_ids.add(obj_id)
                    thread = f"https://news.ycombinator.com/item?id={obj_id}"
                    external = hit.get("url") or ""
                    points = int(hit.get("points") or 0)
                    articles.append(Article(
                        title=title, url=external or thread, source=name, group="community",
                        content=self._clean_html(hit.get("story_text") or "") or title,
                        published_at=datetime.fromtimestamp(hit.get("created_at_i", 0), tz=timezone.utc).isoformat(),
                        author=hit.get("author", ""), signal=points, external_url=external,
                        links=paper_links(external), discussion={"source": name, "url": thread, "points": points},
                    ))
            # Pull page text for the most-upvoted link stories so the LLM has something to summarize
            top = sorted([a for a in articles if a.external_url], key=lambda a: -a.signal)[:12]
            async def enrich(a):
                try:
                    resp = await self.client.get(a.external_url, timeout=10.0)
                    if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                        a.content = self._clean_html(resp.text[:400_000])[:3000] or a.content
                except Exception:
                    pass
            await asyncio.gather(*(enrich(a) for a in top))
            self._status(name, "community", len(articles))
            print(f"  ✅ {name}: {len(articles)}")
        except Exception as e:
            self._status(name, "community", error=f"{type(e).__name__}: {e}")
            print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:100]}")
        return articles

    async def fetch_hf_papers(self) -> list[Article]:
        """Hugging Face daily papers: community-upvoted arXiv papers with code links."""
        name = "Hugging Face Papers"
        articles = []
        try:
            response = await self.client.get(HF_PAPERS_API, params={"limit": 100})
            response.raise_for_status()
            for item in response.json():
                paper = item.get("paper") or {}
                arxiv_id = paper.get("id") or ""
                title = re.sub(r"\s+", " ", paper.get("title") or item.get("title") or "").strip()
                if not arxiv_id or not title:
                    continue
                published_iso = self._parse_date(paper.get("submittedOnDailyAt") or item.get("publishedAt") or "")
                if not self._is_recent(published_iso, days=GROUPS["papers"]["days"]):
                    continue
                links = {"pdf": f"https://arxiv.org/pdf/{arxiv_id}", "hf": f"https://huggingface.co/papers/{arxiv_id}"}
                if paper.get("githubRepo"):
                    links["code"] = paper["githubRepo"]
                authors = ", ".join(a.get("name", "") for a in paper.get("authors", []) if a.get("name"))
                articles.append(Article(
                    title=title, url=f"https://arxiv.org/abs/{arxiv_id}", source=name, group="papers",
                    content=paper.get("summary") or item.get("summary") or "", published_at=published_iso,
                    author=authors, signal=int(paper.get("upvotes") or 0), links=links,
                ))
            self._status(name, "papers", len(articles))
            print(f"  ✅ {name}: {len(articles)}")
        except Exception as e:
            self._status(name, "papers", error=f"{type(e).__name__}: {e}")
            print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:100]}")
        return articles

    def dedupe(self, items: list[Article]) -> list[Article]:
        """Cross-source dedup. Earlier groups win; a later copy that is a Reddit/HN
        thread about an already-kept story is attached to it as a discussion link."""
        order = {g: i for i, g in enumerate(GROUPS)}
        items = sorted(items, key=lambda a: (order.get(a.group, 99), -a.signal, a.published_at), reverse=False)
        kept = []
        for a in items:
            url_keys = [self._normalize_url(u) for u in {a.url, a.external_url} if u]
            title_key = self._normalize_title(a.title)
            existing = next((self.run_urls[k] for k in url_keys if k in self.run_urls), None)
            if existing is None and title_key:
                match = self._match_title(title_key)
                existing = self.run_titles.get(match) if match else None
            if existing is not None:
                if a.discussion and existing.source != a.source and \
                        all(d["url"] != a.discussion["url"] for d in existing.discussions):
                    existing.discussions.append(a.discussion)
                    existing.signal = max(existing.signal, a.signal)
                continue
            # Covered on a previous day (seeded from archive.json)
            if any(k in self.seen_urls for k in url_keys) or not title_key or self._match_title(title_key):
                continue
            for k in url_keys:
                self.seen_urls.add(k)
                self.run_urls[k] = a
            self.seen_titles.add(title_key)
            self.run_titles[title_key] = a
            if a.discussion:
                a.discussions.append(a.discussion)
            kept.append(a)
        return kept

    def select_candidates(self, items: list[Article]) -> list[Article]:
        """Round-robin across groups (strongest signal, then newest, first) with group
        and per-source caps, so the LLM sees a balanced mix."""
        queues = {}
        for group, cfg in GROUPS.items():
            pool = sorted((a for a in items if a.group == group), key=lambda a: (a.signal, a.published_at), reverse=True)
            per_source, queue = {}, []
            for a in pool:
                if per_source.get(a.source, 0) >= PER_SOURCE_CAP or len(queue) >= cfg["cap"]:
                    continue
                per_source[a.source] = per_source.get(a.source, 0) + 1
                queue.append(a)
            queues[group] = queue
        picked = []
        while len(picked) < MAX_LLM_ITEMS and any(queues.values()):
            for group in GROUPS:
                if queues[group] and len(picked) < MAX_LLM_ITEMS:
                    picked.append(queues[group].pop(0))
        return picked

    async def fetch_all(self) -> list[Article]:
        """Fetch every source, dedupe, pick a balanced candidate set, and curate it."""
        print(f"\n📡 Fetching {len(RSS_SOURCES)} feeds + Reddit + Hacker News + Hugging Face Papers...")
        sem = asyncio.Semaphore(8)
        fetch_started = time.time()

        async def rss(name, group, url):
            async with sem:
                return await self.fetch_rss_feed(name, url, group)

        results = await asyncio.gather(
            *(rss(n, g, u) for n, g, u in RSS_SOURCES),
            self.fetch_reddit(), self.fetch_hacker_news(), self.fetch_hf_papers(),
        )
        raw = [a for batch in results for a in batch]
        print(f"⏱ fetched in {time.time() - fetch_started:.0f}s")
        unique = self.dedupe(raw)
        candidates = self.select_candidates(unique)
        mix = {g: sum(1 for a in candidates if a.group == g) for g in GROUPS}
        print(f"\n🔎 {len(raw)} fetched → {len(unique)} unique → {len(candidates)} candidates {mix}")

        final_articles = self.curate_all(candidates)
        # Most important first, newest first within the same importance
        final_articles.sort(key=lambda a: (a.importance, a.published_at), reverse=True)

        print(f"\n✅ Total articles kept: {len(final_articles)} (of {len(raw)} fetched)")
        for tag in TOPIC_TAGS:
            count = sum(1 for a in final_articles if tag in a.tags)
            if count:
                print(f"   {tag}: {count}")

        self.articles = final_articles
        self.stats = {"fetched": len(raw), "unique": len(unique), "candidates": len(candidates), "kept": len(final_articles)}
        return final_articles

    def curate_all(self, candidates: list[Article]) -> list[Article]:
        """Curate candidates in order, dropping irrelevant ones, until MAX_ARTICLES are kept."""
        kept = []
        print(f"\n🧠 Curating {len(candidates)} candidates...")
        for article in candidates:
            if len(kept) >= MAX_ARTICLES:
                break
            print(f"    📝 [{article.group}] {article.title[:70]}")
            if self.curate(article) is None:
                print("       ↳ dropped (not AI/ML relevant)")
                continue
            kept.append(article)
        return kept


DB_COLUMNS = ("title", "url", "source", "summary", "published_at", "fetched_at", "author",
              "tags", "importance", "grp", "links", "discussions")


def init_database(db_path: Path):
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    # The DB is a disposable per-run cache (archive.json is the durable history),
    # so a table from an older schema is simply rebuilt.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    if cols and not set(DB_COLUMNS) <= cols:
        conn.execute("DROP TABLE articles")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            summary TEXT,        -- JSON object {what, why, who}
            published_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            author TEXT,
            tags TEXT,           -- JSON array of TOPIC_TAGS
            importance INTEGER NOT NULL DEFAULT 3,
            grp TEXT,            -- source group (papers, labs, arxiv, ...)
            links TEXT,          -- JSON object {pdf, code, hf}
            discussions TEXT,    -- JSON array [{source, url, points}]
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
    placeholders = ", ".join("?" for _ in DB_COLUMNS)
    for article in articles:
        try:
            cursor.execute(
                f"INSERT OR IGNORE INTO articles ({', '.join(DB_COLUMNS)}, date_key) VALUES ({placeholders}, ?)",
                article.to_db_tuple() + (date_key,),
            )
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
        # Every item of the day, most important first
        cursor.execute("""
            SELECT title, url, source, summary, published_at, author, tags, importance, grp, links, discussions
            FROM articles
            WHERE date_key = ?
            ORDER BY importance DESC, published_at DESC
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
                "importance": a[7] or 3,
                "group": a[8] or "",
                "links": json.loads(a[9]) if a[9] else {},
                "discussions": json.loads(a[10]) if a[10] else [],
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


def write_status(path: Path, fetcher: "NewsFetcher", started: float):
    """Public health report: per-source results and which LLM summarized what."""
    sources = [{"name": n, **v} for n, v in sorted(fetcher.source_status.items(), key=lambda kv: (kv[1]["group"], kv[0]))]
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 1),
        "counts": getattr(fetcher, "stats", {}),
        "summarized_by": fetcher.llm_stats,
        "sources_ok": sum(1 for x in sources if x["ok"]),
        "sources_failed": sum(1 for x in sources if not x["ok"]),
        "sources": sources,
    }
    path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def write_feed(path: Path, articles: list[Article], generated_at: str):
    """RSS 2.0 feed of today's digest so AR can be read in any feed reader."""
    from email.utils import format_datetime
    from xml.sax.saxutils import escape

    def rfc822(iso):
        try:
            return format_datetime(datetime.fromisoformat(iso))
        except Exception:
            return format_datetime(datetime.now(timezone.utc))

    items = []
    for a in articles:
        s = a.summary or {}
        lines = [f"<p><b>{label}:</b> {escape(s.get(key, ''))}</p>" for key, label in (("what", "What"), ("why", "Why"), ("who", "Who")) if s.get(key)]
        lines.append(f"<p>{escape(a.source)} · importance {a.importance}/5 · {escape(', '.join(a.tags))}</p>")
        link = a.url if urlparse(a.url).scheme in ("http", "https") else SITE_URL
        items.append(
            "    <item>\n"
            f"      <title>{escape(a.title)}</title>\n"
            f"      <link>{escape(link)}</link>\n"
            f"      <guid isPermaLink=\"false\">{escape(link)}</guid>\n"
            f"      <pubDate>{rfc822(a.published_at)}</pubDate>\n"
            + "".join(f"      <category>{escape(t)}</category>\n" for t in a.tags)
            + f"      <description>{escape(''.join(lines))}</description>\n"
            "    </item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0">\n  <channel>\n'
        "    <title>AR — daily AI/ML digest</title>\n"
        f"    <link>{escape(SITE_URL)}</link>\n"
        "    <description>Curated AI/ML research and releases with what/why/who summaries.</description>\n"
        f"    <lastBuildDate>{rfc822(generated_at)}</lastBuildDate>\n"
        + "\n".join(items) + "\n  </channel>\n</rss>\n"
    )
    path.write_text(xml, encoding="utf-8")


async def main():
    """Main entry point."""
    print("=" * 60)
    print("🤖 AR fetcher")
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
        generated_at = datetime.now(timezone.utc).isoformat()
        data = {
            "generated_at": generated_at,
            "article_count": len(articles),
            "all_articles": [a.to_dict() for a in articles],
        }
        write_feed(FEED_FILE, articles, generated_at)
        write_status(STATUS_FILE, fetcher, start_time)
        print(f"💾 Wrote {FEED_FILE.name} and {STATUS_FILE.name}")

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