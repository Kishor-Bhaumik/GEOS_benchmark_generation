#!/usr/bin/env python3
"""Does the gateway add its OWN cache_control blocks? (Nemonist or OpenRouter; standard library only.)

Claude accepts at most 4 cache_control blocks per request.

Test 1 (1 call):  we send EXACTLY 4 cache_control blocks (system prompt + 3 user messages).
                  If the gateway adds any of its own, the request fails with
                  "A maximum of 4 blocks with cache_control may be provided. Found N." -> it added N - 4.
Test 2 (6 calls): we send NO cache_control at all, in a growing conversation.
                  If "written to cache" is > 0, cache_control was added between us and the provider;
                  "read from cache" shows whether those blocks give any cache hits.

Both tests send three tool definitions (bash, read_file, write_file), like the GEOS QA agent. Tool calls are
NOT executed: each one is answered "(not executed in this test)", so it is safe on any machine.

Put your keys in the two strings below. DO NOT commit or upload this file with keys in it.

Usage
  python3 markers_check.py --gateway nemonist
  python3 markers_check.py --gateway openrouter --provider google-vertex
  python3 markers_check.py --gateway nemonist --test 1          # only test 1 (one call, ~$0.01)

Cost: test 1 ~$0.01, test 2 ~$0.15-0.35.
"""
import argparse
import json
import random
import re
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

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'bash', 'description': 'Run a bash command and return its output.',
        'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}},
                       'required': ['command']}}},
    {'type': 'function', 'function': {
        'name': 'read_file', 'description': 'Read a text file.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'write_file', 'description': 'Write a text file.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'},
                                                        'content': {'type': 'string'}},
                       'required': ['path', 'content']}}},
]

# Nemonist memory keys/folders may appear in tool-call arguments or errors: never print them
SECRET = re.compile(r'\b(nmk|fld)_[A-Za-z0-9]+')


def notes(turn, n_words, seed):
    rng = random.Random(seed * 1000 + turn)
    return '\n'.join('Note %d.%d: %s.' % (turn, i // 15, ' '.join(rng.choice(WORDS) for _ in range(15)))
                     for i in range(0, n_words, 15))


def marked(text):
    return [{'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}}]


def count_markers(body):
    return json.dumps(body).count('"cache_control"')


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


def make_body(a, messages):
    body = {'model': a.model, 'messages': messages, 'tools': TOOLS,
            'reasoning': {'max_tokens': THINKING_TOKENS}, 'max_tokens': THINKING_TOKENS + ANSWER_TOKENS}
    if a.provider:
        body['provider'] = {'order': [a.provider], 'allow_fallbacks': False}
    return body


def call(base, key, body):
    """Returns (response, None) or (None, error text)."""
    try:
        return post(base, key, body), None
    except urllib.error.HTTPError as e:
        return None, 'HTTP %d: %s' % (e.code, SECRET.sub(r'\1_***', e.read().decode()[:800]))
    except Exception as e:
        return None, '%s: %s' % (type(e).__name__, e)


def usage_line(resp):
    usage = resp.get('usage') or {}
    ptd = usage.get('prompt_tokens_details') or {}
    c = cost_of(usage)
    return (usage.get('prompt_tokens') or 0, ptd.get('cached_tokens') or 0, ptd.get('cache_write_tokens') or 0,
            resp.get('provider'), c)


def test1(a, base, key, seed):
    print('--- Test 1: we send EXACTLY 4 cache_control blocks (the maximum Claude allows)')
    messages = [{'role': 'system', 'content': marked(SYSTEM)}]
    for k in range(1, 4):
        messages.append({'role': 'user', 'content': marked('Project notes, part %d:\n%s' % (k, notes(k, 300, seed)))})
        if k < 3:
            messages.append({'role': 'assistant', 'content': 'Noted part %d.' % k})
    messages[-1]['content'][0]['text'] += '\n\nIn one sentence: which word appears first in part 3?'
    body = make_body(a, messages)
    print('  cache_control blocks in our request: %d' % count_markers(body))
    resp, err = call(base, key, body)
    if err:
        print('  REJECTED: %s' % err)
        m = re.search(r'Found (\d+)', err)
        if m:
            print('  -> RESULT: %d blocks arrived at the provider; we sent 4, so %d were ADDED on the way.'
                  % (int(m.group(1)), int(m.group(1)) - 4))
        else:
            print('  -> RESULT: rejected for another reason (see the error above).')
        return 0.0
    prompt, cached, written, provider, c = usage_line(resp)
    print('  accepted: prompt %d | read from cache %d | written to cache %d | provider %s | cost %s'
          % (prompt, cached, written, provider, '$%.4f' % c if c is not None else 'NOT REPORTED'))
    print('  -> RESULT: accepted, so at most 4 blocks arrived: nothing was added on the way (for this request).')
    return c or 0.0


def test2(a, base, key, seed):
    print('--- Test 2: we send NO cache_control at all (6 calls, growing conversation)')
    messages = [{'role': 'system', 'content': SYSTEM}]
    rows, total = [], 0.0
    for k in range(1, CALLS + 1):
        messages.append({'role': 'user', 'content': 'Project notes, part %d:\n%s\n\nIn one sentence: which word '
                         'appears first in part %d?' % (k, notes(k, 15 if k == 1 else 2200, seed), k)})
        body = make_body(a, messages)
        resp, err = call(base, key, body)
        if err:
            print('  call %d: %s' % (k, err))
            break
        prompt, cached, written, provider, c = usage_line(resp)
        total += c or 0
        rows.append((prompt, cached, written))
        msg = (resp.get('choices') or [{}])[0].get('message') or {}
        tcalls = msg.get('tool_calls') or []
        print('  call %d: cache_control in our request %d | prompt %6d | read from cache %6d | written to cache %6d'
              ' | provider %s | cost %s' % (k, count_markers(body), prompt, cached, written, provider,
                                            '$%.4f' % c if c is not None else 'NOT REPORTED'))
        reply = {'role': 'assistant', 'content': msg.get('content') or ''}   # no thinking sent back
        if tcalls:
            reply['tool_calls'] = tcalls
        messages.append(reply)
        for t in tcalls:
            messages.append({'role': 'tool', 'tool_call_id': t['id'], 'content': '(not executed in this test)'})
    if rows:
        w = sum(r[2] for r in rows)
        r_ = sum(r[1] for r in rows)
        print('  -> RESULT: we sent 0 blocks; written to cache in total %d tokens, read from cache %d tokens.' % (w, r_))
        if w:
            print('     Tokens were written to cache, so cache_control was ADDED between us and the provider.')
        else:
            print('     Nothing was written to cache: no cache_control was added on the way.')
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gateway', choices=GATEWAYS, required=True)
    ap.add_argument('--model', default='anthropic/claude-sonnet-5.5')
    ap.add_argument('--provider', help='pin to one provider, no fallbacks: e.g. google-vertex, anthropic '
                                       '(default: gateway routing)')
    ap.add_argument('--test', choices=['1', '2', 'all'], default='all')
    a = ap.parse_args()

    key = NEMONIST_KEY if a.gateway == 'nemonist' else OPENROUTER_KEY
    if not key.strip():
        sys.exit('No key for %s: fill %s at the top of this script.'
                 % (a.gateway, 'NEMONIST_KEY' if a.gateway == 'nemonist' else 'OPENROUTER_KEY'))
    base, seed = GATEWAYS[a.gateway], int(time.time())   # new seed: never reuses an earlier run's cache

    print('=== %s UTC | gateway %s | model %s | provider %s | tools on' % (
        time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()), a.gateway, a.model, a.provider or 'default routing'))
    total = 0.0
    if a.test in ('1', 'all'):
        total += test1(a, base, key, seed)
    if a.test in ('2', 'all'):
        total += test2(a, base, key, seed)
    print('total cost $%.4f' % total)


if __name__ == '__main__':
    main()
