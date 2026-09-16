#!/usr/bin/env python3
"""Check that the LLM API keys (GitHub secrets) work, using the fetcher's own
provider code — the same request the daily run makes. Never prints a key.

Results go to the job log, the run summary page, and as annotations.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location("fetch_news", Path(__file__).parent / "fetch-news.py")
fetch_news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_news)

PROMPT = fetch_news.CURATOR_PROMPT.replace("{title}", "Scaling laws for sparse mixture-of-experts language models") \
    .replace("{source_name}", "arXiv") \
    .replace("{abstract_or_excerpt}", "We study how MoE transformer LLMs scale with expert count and compute.")

PROVIDERS = [
    ("Gemini", fetch_news.GEMINI_API_KEY_ENV, fetch_news.GeminiProvider),
    ("Ollama Cloud", fetch_news.OLLAMA_API_KEY_ENV, fetch_news.OllamaProvider),
]


def redact(text, key):
    text = " ".join(str(text).split())[:220]
    if key:
        text = text.replace(key, "***")
    return re.sub(r"\*{2,}\S{1,8}", "***", text)


def gemini_models(http, key):
    try:
        resp = http.get(f"{fetch_news.GEMINI_BASE}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200})
        if resp.status_code != 200:
            return f"list failed (HTTP {resp.status_code})"
        names = [m["name"].removeprefix("models/") for m in resp.json().get("models", [])]
        return ", ".join(n for n in names if "2.5-flash" in n) or "none"
    except Exception as e:
        return f"list failed ({type(e).__name__})"


def main():
    rows = []
    with httpx.Client(timeout=fetch_news.LLM_TIMEOUT) as http:
        for name, env, cls in PROVIDERS:
            key = (os.environ.get(env) or "").strip()
            if not key:
                rows.append((name, env, "❌ not set", ""))
                print(f"::error title={name}::{env} is empty or not passed to the workflow")
                continue
            provider = cls(http, key)
            provider.min_interval = 0
            try:
                result = fetch_news.parse_curation(provider.generate(PROMPT))
                status, level = "✅ works", "notice"
                detail = f"model {provider.model} · tags {result.get('tags')}"
            except Exception as e:
                status, level = "❌ failed", "error"
                detail = f"model {provider.model} · {type(e).__name__}: {e}"
                if name == "Gemini":
                    detail += f" · 2.5 models visible to this key: {gemini_models(http, key)}"
            detail = redact(detail, key)
            rows.append((name, env, status, detail))
            print(f"::{level} title={name}::{status} — {detail}")

    table = ["| Provider | Secret | Result | Details |", "|---|---|---|---|"]
    table += [f"| {n} | `{e}` | {s} | {d.replace('|', '/')} |" for n, e, s, d in rows]
    print("\n".join(table))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## API key check\n\n" + "\n".join(table) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
