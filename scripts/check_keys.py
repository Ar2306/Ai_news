#!/usr/bin/env python3
"""Check which LLM API keys (GitHub secrets) actually work. Never prints a key.

For each provider: is the key set, does it authenticate (list models), and can it
generate (one ~1-token request, so billing/credit problems show up too).
Results go to the job log, the run summary page, and as annotations.
"""

import os
import re
import sys

import anthropic
import httpx

TIMEOUT = 30.0


def redact(text, key):
    text = " ".join(str(text).split())[:200]
    if key:
        text = text.replace(key, "***")
    # Some providers echo the key's last characters ("****Y123"); Actions logs are public here
    return re.sub(r"\*{2,}\S{1,8}", "***", text)


def http_error(resp):
    try:
        body = resp.json()
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else err
    except Exception:
        msg = resp.text
    return f"HTTP {resp.status_code}: {msg}"


def check_anthropic(key):
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=TIMEOUT)
    try:
        client.models.list(limit=1)
    except anthropic.APIStatusError as e:
        return False, False, f"auth failed (HTTP {e.status_code}): {e.message}"
    try:
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
    except anthropic.APIStatusError as e:
        return True, False, f"key valid but generation failed (HTTP {e.status_code}): {e.message}"
    return True, True, "claude-haiku-4-5 responded"


def check_openai_style(key, base, model_hint):
    """xAI and DeepSeek both expose OpenAI-style /models and /chat/completions."""
    headers = {"Authorization": f"Bearer {key}"}
    r = httpx.get(f"{base}/models", headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        return False, False, f"auth failed ({http_error(r)})"
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    model = next((m for m in ids if model_hint in m), ids[0] if ids else model_hint)
    r = httpx.post(
        f"{base}/chat/completions",
        headers=headers,
        json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return True, False, f"key valid but generation failed on {model} ({http_error(r)})"
    return True, True, f"{model} responded"


def check_gemini(key):
    headers = {"x-goog-api-key": key}
    base = "https://generativelanguage.googleapis.com/v1beta"
    r = httpx.get(f"{base}/models", headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        return False, False, f"auth failed ({http_error(r)})"
    models = [
        m["name"] for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    model = next((m for m in models if "flash" in m and "lite" not in m), models[0] if models else "models/gemini-2.5-flash")
    r = httpx.post(
        f"{base}/{model}:generateContent",
        headers=headers,
        json={"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 1}},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return True, False, f"key valid but generation failed on {model} ({http_error(r)})"
    return True, True, f"{model} responded"


def deepseek_balance(key):
    try:
        r = httpx.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        if r.status_code == 200:
            infos = r.json().get("balance_infos") or []
            return ", ".join(f"{b.get('total_balance')} {b.get('currency')}" for b in infos) or "no balance info"
    except Exception:
        pass
    return None


CHECKS = [
    ("Claude", "ANTHROPIC_API_KEY", check_anthropic),
    ("Gemini", "GEMINI_API_KEY", check_gemini),
    ("Grok (xAI)", "XAI_API_KEY", lambda k: check_openai_style(k, "https://api.x.ai/v1", "grok")),
    ("DeepSeek", "DEEPSEEK_API_KEY", lambda k: check_openai_style(k, "https://api.deepseek.com", "deepseek-chat")),
]


def main():
    rows = []
    for name, env, check in CHECKS:
        key = (os.environ.get(env) or "").strip()
        if not key:
            rows.append((name, env, "❌ not set", "", ""))
            print(f"::error title={name}::{env} is empty or not passed to the workflow")
            continue
        try:
            auth_ok, gen_ok, detail = check(key)
        except Exception as e:
            auth_ok, gen_ok, detail = False, False, f"{type(e).__name__}: {e}"
        detail = redact(detail, key)
        if name == "DeepSeek" and auth_ok:
            bal = deepseek_balance(key)
            if bal:
                detail += f" · balance {bal}"
        status = "✅ works" if gen_ok else ("⚠️ key valid, can't generate" if auth_ok else "❌ invalid")
        rows.append((name, env, status, "yes" if auth_ok else "no", detail))
        level = "notice" if gen_ok else ("warning" if auth_ok else "error")
        print(f"::{level} title={name}::{status} — {detail}")

    table = ["| Provider | Secret | Result | Auth | Details |", "|---|---|---|---|---|"]
    table += [f"| {n} | `{e}` | {s} | {a} | {d.replace('|', '/')} |" for n, e, s, a, d in rows]
    print("\n".join(table))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## API key check\n\n" + "\n".join(table) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
