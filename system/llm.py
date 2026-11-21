"""Calls a language model through OpenRouter, the only route the hackathon allows.

Owner: Tony (C). Uses only the standard library (urllib), as the rules require.

- Reads the key from the OPENROUTER_API_KEY environment variable. The key is never printed or logged.
- Temperature 0 and a fixed seed, so answers repeat.
- Caches every answer in system/cache/, keyed by a hash of the model and prompts. A second
  run on the same cases reads the cache, so it gives identical output and costs nothing.
- Only good answers (a JSON object) are cached. A failed call is retried, then raises, and the
  officer that called it returns None so main.py falls back to Tom.
- Logs prompt tokens, completion tokens and cost for every real call to system/cache/usage.jsonl.
  `python3 llm.py` prints the totals.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Not a reasoning model, so it honours temperature 0 and the seed. At $0.40 / $1.60 per million
# tokens, two calls of about 8k tokens come to under 1 cent per case (about $1.50 per 200 cases).
MODEL = "openai/gpt-4.1-mini"
PRICE_PER_MILLION = {"openai/gpt-4.1-mini": (0.40, 1.60)}  # (prompt, completion), used if the API gives no cost

SEED = 7
MAX_TOKENS = 900        # the answer is a small JSON object
TIMEOUT_SECONDS = 45
ATTEMPTS = 3            # one call plus two retries
RETRY_WAIT_SECONDS = [1, 4]

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
USAGE_LOG = os.path.join(CACHE_DIR, "usage.jsonl")


def available():
    """True if an API key is set. Without one, the system runs on Tom alone."""
    return bool(os.environ.get("OPENROUTER_API_KEY"))


# ---------------------------------------------------------------- cache
def _cache_path(model, system_prompt, user_prompt):
    text = model + "\n" + system_prompt + "\n" + user_prompt
    return os.path.join(CACHE_DIR, hashlib.sha256(text.encode("utf-8")).hexdigest() + ".json")


def _write_atomic(path, obj):
    """Write through a temporary file, so a crash or a parallel run never leaves half a file."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + "." + str(os.getpid()) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- parsing
def parse_json_object(text):
    """Parse the model's reply as a JSON object, tolerating code fences or text around it."""
    text = (text or "").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the model's reply")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("the model's reply is not a JSON object")
    return obj


# ---------------------------------------------------------------- usage log
def _log_usage(model, usage, seconds, label):
    """Append one line per real call. Logging must never break a decision."""
    try:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            price_in, price_out = PRICE_PER_MILLION.get(model, (0.0, 0.0))
            cost = (prompt_tokens * price_in + completion_tokens * price_out) / 1e6
        line = {"label": label, "model": model, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "cost_usd": round(float(cost), 6),
                "seconds": round(seconds, 2)}
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def usage_totals():
    """Totals from the usage log: calls, tokens, cost and seconds, overall and per label."""
    totals = {}
    if os.path.exists(USAGE_LOG):
        with open(USAGE_LOG, encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                for name in ("all", row.get("label") or "?"):
                    t = totals.setdefault(name, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                 "cost_usd": 0.0, "seconds": 0.0})
                    t["calls"] += 1
                    for k in ("prompt_tokens", "completion_tokens", "cost_usd", "seconds"):
                        t[k] += row.get(k) or 0
    return totals


# ---------------------------------------------------------------- the call
def _post(body):
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def ask_json(system_prompt, user_prompt, model=MODEL, label=""):
    """Send one chat request and return the model's reply parsed as a JSON dict.

    Raises if there is no key or every attempt fails. `label` (e.g. the officer's name) only
    tags the usage log; it is not part of the cache key.
    """
    # 1. Cache lookup.
    cache_path = _cache_path(model, system_prompt, user_prompt)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                return cached
        except (OSError, ValueError):
            pass  # unreadable cache file: ask again and overwrite it

    # 2. Call OpenRouter, retrying errors that can pass (network, rate limit, server, bad JSON).
    body = {
        "model": model,
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    last_error = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_WAIT_SECONDS[min(attempt - 1, len(RETRY_WAIT_SECONDS) - 1)])
        started = time.time()
        try:
            data = _post(body)
            if "choices" not in data:
                raise ValueError("no choices in the reply: " + str(data.get("error"))[:200])
            _log_usage(model, data.get("usage") or {}, time.time() - started, label)
            answer = parse_json_object(data["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in (401, 402, 403, 404):
                break  # bad key, no credit or unknown model: retrying cannot help
            continue
        except Exception as error:
            last_error = error
            continue

        # 3. Save to the cache.
        try:
            _write_atomic(cache_path, answer)
        except OSError:
            pass
        return answer
    raise RuntimeError("OpenRouter call failed: " + type(last_error).__name__)


if __name__ == "__main__":
    # python3 llm.py  -> totals from the usage log (cache hits cost nothing and are not logged).
    all_totals = usage_totals()
    if not all_totals:
        print("no calls logged yet in", USAGE_LOG, file=sys.stderr)
    for name in sorted(all_totals):
        t = all_totals[name]
        print(f"{name:<8} calls {t['calls']:>5}  prompt {t['prompt_tokens']:>9}  completion "
              f"{t['completion_tokens']:>7}  cost ${t['cost_usd']:.4f}  time {t['seconds']:.0f}s")
