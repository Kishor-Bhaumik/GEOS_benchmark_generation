#!/usr/bin/env python3
"""Prompt-caching check for Claude through Nemonist or OpenRouter (6-call growing conversation).

Put your keys in the two strings below. DO NOT commit or upload this file with keys in it.

Usage
  python caching_check.py --gateway nemonist
  python caching_check.py --gateway openrouter
  python caching_check.py --gateway nemonist --provider anthropic
  python caching_check.py --gateway openrouter --provider google-vertex --model anthropic/claude-sonnet-5.5

Each run: 6 calls in one conversation, ~1.5k tokens at call 1, +~7k tokens per call (about $0.15).
With working caching, call k reads about the whole prompt of call k-1 from cache.
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request

# ---- API keys (strings) ----
NEMONIST_KEY = ""
OPENROUTER_KEY = ""

GATEWAYS = {
    'nemonist': 'https://nemonist.com/api/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
}
CALLS = 6
THINKING_TOKENS = 1024
ANSWER_TOKENS = 300

SYSTEM = ('You are a careful assistant helping with a numerical simulation study. ' * 6 +
          '\n\nRules:\n' + ''.join('%d. Keep answers short and factual; reply in one sentence unless asked '
                                   'otherwise; never invent numbers that are not in the notes.\n' % i
                                   for i in range(1, 31)))

WORDS = ('pressure stress strain mesh element node solver iteration residual tolerance wellbore casing '
         'cement fracture aperture permeability porosity viscosity density modulus poisson ratio boundary '
         'condition timestep newton linear krylov preconditioner multigrid region field output table '
         'function gradient flux traction displacement velocity temperature saturation phase component '
         'reservoir caprock fault injection production rate volume time depth azimuth inclination').split()


def notes(turn, n_words, seed):
    rng = random.Random(seed * 1000 + turn)
    return '\n'.join('Note %d.%d: %s.' % (turn, i // 15, ' '.join(rng.choice(WORDS) for _ in range(15)))
                     for i in range(0, n_words, 15))


def with_markers(messages):
    """cache_control on the system prompt and the last two user messages (same as the QA harness)."""
    msgs = json.loads(json.dumps(messages))

    def mark(m):
        if isinstance(m.get('content'), str) and m['content']:
            m['content'] = [{'type': 'text', 'text': m['content'], 'cache_control': {'type': 'ephemeral'}}]
    mark(msgs[0])
    for m in [m for m in msgs[1:] if m['role'] == 'user'][-2:]:
        mark(m)
    return msgs


def post(base, key, body):
    req = urllib.request.Request(base + '/chat/completions', method='POST', data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def cost_of(usage):
    """BYOK (Nemonist): real cost = upstream_inference_cost (+ gateway fee in usage.cost)."""
    if usage.get('is_byok'):
        up = (usage.get('cost_details') or {}).get('upstream_inference_cost')
        return None if up is None else up + (usage.get('cost') or 0)
    return usage.get('cost')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gateway', choices=GATEWAYS, required=True)
    ap.add_argument('--model', default='anthropic/claude-sonnet-5.5')
    ap.add_argument('--provider', help='pin to one provider, no fallbacks: e.g. anthropic, google-vertex, '
                                       'amazon-bedrock (default: gateway routing)')
    a = ap.parse_args()

    key = NEMONIST_KEY if a.gateway == 'nemonist' else OPENROUTER_KEY
    if not key.strip():
        sys.exit('No key for %s: fill %s at the top of this script.'
                 % (a.gateway, 'NEMONIST_KEY' if a.gateway == 'nemonist' else 'OPENROUTER_KEY'))
    base, seed = GATEWAYS[a.gateway], int(time.time())   # new seed: never reuses an earlier run's cache

    print('=== %s | gateway %s | model %s | provider %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), a.gateway,
                                                          a.model, a.provider or 'default routing'))
    messages = [{'role': 'system', 'content': SYSTEM}]
    rows, total = [], 0.0
    for k in range(1, CALLS + 1):
        text = notes(k, 15 if k == 1 else 2200, seed)
        messages.append({'role': 'user', 'content': 'Project notes, part %d:\n%s\n\nIn one sentence: which word '
                         'appears first in part %d?' % (k, text, k)})
        body = {'model': a.model, 'messages': with_markers(messages),
                'reasoning': {'max_tokens': THINKING_TOKENS}, 'max_tokens': THINKING_TOKENS + ANSWER_TOKENS}
        if a.provider:
            body['provider'] = {'order': [a.provider], 'allow_fallbacks': False}
        try:
            resp = post(base, key, body)
        except urllib.error.HTTPError as e:
            print('call %d: HTTP %d: %s' % (k, e.code, e.read().decode()[:800]))
            break
        except Exception as e:
            print('call %d: %s: %s' % (k, type(e).__name__, e))
            break
        usage = resp.get('usage') or {}
        ptd = usage.get('prompt_tokens_details') or {}
        prompt, cached, written = (usage.get('prompt_tokens') or 0, ptd.get('cached_tokens') or 0,
                                   ptd.get('cache_write_tokens') or 0)
        c = cost_of(usage)
        total += c or 0
        rows.append((prompt, cached))
        print('  call %d: prompt %6d | read from cache %6d | written to cache %6d | provider %s | cost %s' % (
            k, prompt, cached, written, resp.get('provider'), '$%.4f' % c if c is not None else 'NOT REPORTED'))
        msg = (resp.get('choices') or [{}])[0].get('message') or {}
        messages.append({'role': 'assistant', 'content': msg.get('content') or ''})   # no thinking sent back

    print('total cost $%.4f' % total)
    for k in range(1, len(rows)):
        want, cached = rows[k - 1][0], rows[k][1]
        share = cached / want if want else 0
        print('  call %d read %d of ~%d reusable tokens from cache (%.0f%%) -> %s' % (
            k + 1, cached, want, 100 * share, 'CACHING WORKS' if share >= 0.8 else 'CACHING BROKEN'))
    if len(rows) > 1:
        reusable = sum(r[0] for r in rows[:-1])
        print('overall: %.0f%% of reusable tokens read from cache' % (100 * sum(r[1] for r in rows[1:]) / reusable))


if __name__ == '__main__':
    main()
