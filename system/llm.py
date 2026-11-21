"""Calls a language model through OpenRouter, the only route the hackathon allows.

Owner: Tony (C). Uses only the standard library (urllib), as the rules require.

- Reads the key from the OPENROUTER_API_KEY environment variable.
- Temperature 0 and a fixed seed, so answers repeat.
- Caches every answer in system/cache/, keyed by a hash of the model and prompts. A second
  run on the same cases reads the cache, so it gives identical output and costs nothing.
"""
import hashlib
import json
import os
import urllib.request

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"  # placeholder: Tony picks the model (cheap enough for ~$4 per 200 cases)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def available():
    """True if an API key is set. Without one, the system runs on Tom alone."""
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def ask_json(system_prompt, user_prompt, model=MODEL):
    """Send one chat request and return the model's reply parsed as a JSON dict."""
    # 1. Cache lookup.
    key = hashlib.sha256((model + "\n" + system_prompt + "\n" + user_prompt).encode("utf-8")).hexdigest()
    cache_path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    # 2. Call OpenRouter.
    body = {
        "model": model,
        "temperature": 0,
        "seed": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    answer = json.loads(data["choices"][0]["message"]["content"])

    # 3. Save to the cache.
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(answer, f)
    return answer
