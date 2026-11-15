#!/usr/bin/env python3
"""Check what usage / cost fields the Nemonist gateway returns, including reasoning tokens.

The API key is read from the environment variable NEMONIST_API_KEY; it is never stored here.

Usage:
  python check_api_cost.py --model anthropic/claude-opus-5.5 --in-price 4 --out-price 20
  python check_api_cost.py --model <id> --effort high          # ask for reasoning, if the model supports it
  python check_api_cost.py --model <id> --show-model-info      # print the gateway's listing for this model
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("NEMONIST_BASE_URL", "https://nemonist.com/api/v1")

# A short problem that makes a reasoning model actually think, with a checkable answer (= 36).
PROMPT = ("How many positive divisors does 1260 have? Think it through, "
          "then end with one line: FINAL_ANSWER: <number>")


def call(method, path, key, body=None, timeout=600):
    req = urllib.request.Request(
        BASE_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {path}: {e.read().decode()[:1000]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--in-price", type=float, help="USD per 1M prompt tokens (your rate)")
    ap.add_argument("--out-price", type=float, help="USD per 1M completion tokens (your rate)")
    ap.add_argument("--cache-price", type=float, help="USD per 1M cached prompt tokens (optional)")
    ap.add_argument("--effort", help="reasoning effort to request, e.g. low/medium/high (optional)")
    ap.add_argument("--reasoning-style", choices=["reasoning_effort", "reasoning_object", "reasoning_budget"], default="reasoning_effort",
                    help="send --effort as reasoning_effort=<e> or as reasoning={\"effort\": <e>} (OpenRouter style)")
    ap.add_argument("--budget", type=int, default=4000, help="thinking budget for --reasoning-style reasoning_budget")
    ap.add_argument("--prompt", help="question to ask instead of the built-in divisor question")
    ap.add_argument("--show-model-info", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("NEMONIST_API_KEY")
    if not key:
        sys.exit("NEMONIST_API_KEY is not set. Run: source ~/.config/nemonist/env")

    if a.show_model_info:
        models = call("GET", "/models", key).get("data", [])
        hit = [m for m in models if m.get("id") == a.model]
        print(json.dumps(hit[0] if hit else {"note": f"{a.model} not in /models",
                                              "n_models": len(models)}, indent=2)[:3000])
        print()

    prompt = (a.prompt + " Think it through, then end with one line: FINAL_ANSWER: <number>") if a.prompt else PROMPT
    body = {"model": a.model, "messages": [{"role": "user", "content": prompt}]}
    if a.reasoning_style == "reasoning_budget":
        body["reasoning"] = {"max_tokens": a.budget}
    elif a.effort and a.reasoning_style == "reasoning_object":
        body["reasoning"] = {"effort": a.effort}
    elif a.effort:
        body["reasoning_effort"] = a.effort
    t0 = time.time()
    resp = call("POST", "/chat/completions", key, body)
    wall = time.time() - t0

    msg = resp["choices"][0]["message"]
    text = msg.get("content") or ""
    usage = resp.get("usage", {})

    print("=== Answer (last 300 chars) ===")
    print(text[-300:])
    print(f"\n=== Model reported: {resp.get('model')}   wall time: {wall:.1f} s   id: {resp.get('id')}")
    print("\n=== Raw usage block from the gateway ===")
    print(json.dumps(usage, indent=2))
    extra = {k: v for k, v in resp.items() if k not in ("choices", "usage", "id", "model",
                                                         "object", "created")}
    if extra:
        print("\n=== Other top-level fields ===")
        print(json.dumps(extra, indent=2)[:1500])
    for k in ("reasoning", "reasoning_content"):
        if msg.get(k):
            print("\n(message carries %s text: %d chars)" % (k, len(msg[k])))
    if msg.get("reasoning_details"):
        print("(reasoning_details: %d block(s))" % len(msg["reasoning_details"]))

    # ---- interpretation ----
    p = usage.get("prompt_tokens", 0)
    c = usage.get("completion_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    print("\n=== Summary ===")
    print(f"prompt tokens     : {p}  (cached: {cached})")
    print(f"completion tokens : {c}")
    print(f"reasoning tokens  : {reasoning if reasoning is not None else 'NOT REPORTED'}"
          + ("" if reasoning is None else f"  (visible answer ~ {c - reasoning})"))
    gw_cost = usage.get("cost", resp.get("cost"))
    upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    print(f"gateway cost field: {gw_cost if gw_cost is not None else 'NOT REPORTED'}"
          + ("  (BYOK: gateway charges 0, provider bills upstream)" if usage.get("is_byok") else ""))
    print(f"upstream cost     : {upstream if upstream is not None else 'NOT REPORTED'}")
    if not gw_cost and upstream:
        gw_cost = upstream
    if a.in_price is not None and a.out_price is not None:
        cp = a.cache_price if a.cache_price is not None else a.in_price
        est = ((p - cached) * a.in_price + cached * cp + c * a.out_price) / 1e6
        print(f"cost from your rates: ${est:.8f}  (reasoning billed as output, inside completion tokens)")
        if isinstance(gw_cost, (int, float)) and gw_cost:
            print(f"gateway / your-rate ratio: {gw_cost / est:.3f}" if est else "")


if __name__ == "__main__":
    main()
