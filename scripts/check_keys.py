#!/usr/bin/env python3
"""Check the LLM API keys (GitHub secrets). Never prints a key.

1. Runs the fetcher's real provider chain once to show which model a daily run will use.
2. Probes every text model each key can see with a tiny request and reports which
   ones this key can actually use (free) vs. need a paid plan / are unavailable.

Results go to the job log, the run summary page, and as annotations.
"""

import importlib.util
import os
import re
import sys
import time
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location("fetch_news", Path(__file__).parent / "fetch-news.py")
fn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fn)

PROMPT = fn.CURATOR_PROMPT.replace("{title}", "Scaling laws for sparse mixture-of-experts language models") \
    .replace("{source_name}", "arXiv") \
    .replace("{abstract_or_excerpt}", "We study how MoE transformer LLMs scale with expert count and compute.")
SKIP = re.compile(r"embed|image|tts|audio|live|vision|veo|imagen|aqa|robotics|computer-use|learnlm", re.I)


def redact(text, key):
    text = " ".join(str(text).split())[:170]
    if key:
        text = text.replace(key, "***")
    return re.sub(r"\*{2,}\S{1,8}", "***", text)


def verdict(resp):
    if resp.status_code == 200:
        return "✅ usable"
    msg = fn.error_message(resp)
    if resp.status_code == 402:
        return "💳 needs paid plan"
    if resp.status_code == 429 and re.search(r"limit: ?0\b", resp.text):
        return "💳 no free quota"
    if resp.status_code == 429:
        return f"⏳ rate limited ({msg[:60]})"
    if resp.status_code == 404:
        return "🚫 unavailable to this key"
    return f"❌ HTTP {resp.status_code} ({msg[:60]})"


def probe_gemini(http, key):
    headers = {"x-goog-api-key": key}
    names = list(fn.GEMINI_MODELS)
    try:
        resp = http.get(f"{fn.GEMINI_BASE}/models", headers=headers, params={"pageSize": 200})
        if resp.status_code != 200:
            return [("(list models)", f"❌ HTTP {resp.status_code} ({fn.error_message(resp)[:80]})")]
        for m in resp.json().get("models", []):
            name = m["name"].removeprefix("models/")
            if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in name \
                    and not SKIP.search(name) and name not in names:
                names.append(name)
    except Exception as e:
        return [("(list models)", f"❌ {type(e).__name__}")]
    rows = []
    for name in names:
        try:
            resp = http.post(f"{fn.GEMINI_BASE}/models/{name}:generateContent", headers=headers,
                             json={"contents": [{"parts": [{"text": "Reply OK"}]}], "generationConfig": {"maxOutputTokens": 16}})
            rows.append((name, verdict(resp)))
        except Exception as e:
            rows.append((name, f"❌ {type(e).__name__}"))
        time.sleep(4)  # stay under per-minute limits so results aren't skewed
    return rows


def probe_ollama(http, key):
    headers = {"Authorization": f"Bearer {key}"}
    names = list(fn.OLLAMA_MODELS) + [m for m in fn.ollama_catalogue(http, key) if m not in fn.OLLAMA_MODELS]
    rows = []
    for name in names:
        try:
            resp = http.post(f"{fn.OLLAMA_BASE}/chat", headers=headers, timeout=120,
                             json={"model": name, "messages": [{"role": "user", "content": "Reply OK"}],
                                   "stream": False, "options": {"num_predict": 16}})
            rows.append((name, verdict(resp)))
        except Exception as e:
            rows.append((name, f"❌ {type(e).__name__}"))
        time.sleep(1)
    return rows


PROVIDERS = [
    ("Gemini", fn.GEMINI_API_KEY_ENV, fn.GeminiProvider, probe_gemini),
    ("Ollama Cloud", fn.OLLAMA_API_KEY_ENV, fn.OllamaProvider, probe_ollama),
]


def main():
    out = []
    with httpx.Client(timeout=fn.LLM_TIMEOUT) as http:
        for name, env, cls, probe in PROVIDERS:
            key = (os.environ.get(env) or "").strip()
            out.append(f"### {name} (`{env}`)\n")
            if not key:
                out.append("❌ secret not set\n")
                print(f"::error title={name}::{env} is empty or not passed to the workflow")
                continue

            # What the daily run will actually do
            provider = cls(http, key)
            provider.min_interval = 0
            try:
                result = fn.parse_curation(provider.generate(PROMPT))
                chain = f"✅ daily run will use **{provider.model}** (tags {result.get('tags')})"
                print(f"::notice title={name}::works — daily run will use {provider.model}")
            except Exception as e:
                chain = f"❌ no working model — last tried {provider.model}: {redact(e, key)}"
                print(f"::error title={name}::{redact(chain, key)}")
            out.append(chain + "\n")

            rows = probe(http, key)
            usable = [m for m, v in rows if v.startswith("✅")]
            print(f"::notice title={name} models::usable with this key: {', '.join(usable) or 'none'}")
            out.append("| Model | Result |\n|---|---|")
            out += [f"| `{m}` | {redact(v, key).replace('|', '/')} |" for m, v in rows]
            out.append("")

    text = "\n".join(out)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## API key check\n\n" + text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
