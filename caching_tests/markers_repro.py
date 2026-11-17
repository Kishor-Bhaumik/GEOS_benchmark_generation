#!/usr/bin/env python3
"""Try to reproduce "A maximum of 4 blocks with cache_control may be provided. Found 5." while WE send 0 blocks.

The real GEOS run failed this way at turn 5 of the controller session. This script rebuilds a conversation of the
same shape (same tools, same rounds of tool calls, tool results of similar length) and sends it with NO
cache_control at all, growing one round per request:

  request 1: system + user                                   (0 tool results)
  request 2: + round 1 (3 tool calls: read .nemonist/key, read .nemonist/config, bash)   (3 tool results)
  request 3: + round 2 (1 read_file)                          (4)
  request 4: + round 3 (1 read_file)                          (5)
  request 5: + round 4 (2 read_file)                          (7)   <- the real run failed here
  request 6: + round 5 (1 bash)                               (8)
  request 7: + round 6 (1 bash)                               (9)

The conversation is fixed (the model's replies are NOT used), so every run sends the same requests. For each request
the script prints how many cache_control blocks WE sent (always 0) and whether the request was accepted or rejected.
A rejection "Found N" means N blocks arrived at the provider although we sent none.

Put your keys in the two strings below. DO NOT commit or upload this file with keys in it.

Usage
  python3 markers_repro.py --gateway nemonist
  python3 markers_repro.py --gateway openrouter --provider google-vertex      # same requests, for comparison
Cost: about $0.10-0.15 per run (7 requests, 2.5k-12k prompt tokens).
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
THINKING_TOKENS = 1024
ANSWER_TOKENS = 300

WORDS = ('pressure stress strain mesh element node solver iteration residual tolerance wellbore casing '
         'cement fracture aperture permeability porosity viscosity density modulus poisson ratio boundary '
         'condition timestep newton linear krylov preconditioner multigrid region field output table '
         'function gradient flux traction displacement velocity temperature saturation phase component '
         'reservoir caprock fault injection production rate volume time depth azimuth inclination').split()

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'bash', 'description': 'Run a bash command and return its exit code, stdout and stderr.',
        'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'},
                                                        'timeout_s': {'type': 'integer'}},
                       'required': ['command']}}},
    {'type': 'function', 'function': {
        'name': 'read_file', 'description': 'Read a text file; returns at most `limit` lines starting at `offset`.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'offset': {'type': 'integer'},
                                                        'limit': {'type': 'integer'}},
                       'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'write_file', 'description': 'Write a text file.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'},
                                                        'content': {'type': 'string'}},
                       'required': ['path', 'content']}}},
]

# Nemonist memory keys/folders may appear in errors: never print them
SECRET = re.compile(r'(nmk|fld)_[A-Za-z0-9]+')

RNG = random.Random(20261115)            # fixed: every run sends the same conversation


def lines(n, start=1):
    """read_file-style numbered lines (~2,000 characters for n = 20)."""
    return ''.join('%6d\t%s.\n' % (start + i, ' '.join(RNG.choice(WORDS) for _ in range(14))) for i in range(n))


SYSTEM = ('You are the controller of a GEOS benchmark run. You explore one GEOS input deck, run a parameter sweep '
          'with the tools, and write one multiple-choice question.\n\n' +
          ''.join('Rule %d: %s.\n' % (i, ' '.join(RNG.choice(WORDS) for _ in range(16))) for i in range(1, 70)))
USER = 'Start attempt 1 of the deck. The protocol file is protocol.md; your memory folder is the current folder.'

# rounds of (tool call name, arguments, tool result), shaped like the real controller session
ROUNDS = [
    [('read_file', {'path': '.nemonist/key'}, 'ERROR: [Errno 2] No such file or directory: .nemonist/key'),
     ('read_file', {'path': '.nemonist/config'}, 'ERROR: [Errno 2] No such file or directory: .nemonist/config'),
     ('bash', {'command': 'wc -l protocol.md'}, 'exit code: 0\n--- stdout ---\n1066 protocol.md\n\n--- stderr ---\n')],
    [('read_file', {'path': 'protocol.md', 'limit': 300}, lines(20, 260))],
    [('read_file', {'path': 'protocol.md', 'offset': 58, 'limit': 110}, lines(20, 135))],
    [('read_file', {'path': 'protocol.md', 'offset': 101, 'limit': 36}, lines(20, 112)),
     ('read_file', {'path': 'protocol.md', 'offset': 300, 'limit': 80}, lines(20, 340))],
    [('bash', {'command': 'sed -n 167,270p protocol.md | cut -c1-400'},
      'exit code: 0\n--- stdout ---\n' + lines(19, 167) + '\n--- stderr ---\n')],
    [('bash', {'command': 'sed -n 270,330p protocol.md | cut -c1-400'},
      'exit code: 0\n--- stdout ---\n' + lines(19, 270) + '\n--- stderr ---\n')],
]


def conversation(n_rounds):
    msgs = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': USER}]
    for r, rnd in enumerate(ROUNDS[:n_rounds], 1):
        calls = [{'id': 'call_r%d_%d' % (r, i), 'type': 'function',
                  'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args, _) in enumerate(rnd)]
        msgs.append({'role': 'assistant', 'content': None, 'tool_calls': calls})
        for c, (_, _, result) in zip(calls, rnd):
            msgs.append({'role': 'tool', 'tool_call_id': c['id'], 'content': result})
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
    ap.add_argument('--provider', help='pin to one provider, no fallbacks: e.g. google-vertex (default: gateway routing)')
    ap.add_argument('--dry-run', action='store_true', help='print the requests\' shape only, no API call')
    a = ap.parse_args()

    key = NEMONIST_KEY if a.gateway == 'nemonist' else OPENROUTER_KEY
    if not a.dry_run and not key.strip():
        sys.exit('No key for %s: fill %s at the top of this script.'
                 % (a.gateway, 'NEMONIST_KEY' if a.gateway == 'nemonist' else 'OPENROUTER_KEY'))
    base = GATEWAYS[a.gateway]
    print('=== %s UTC | gateway %s | model %s | provider %s | tools on | cache_control sent by us: none' % (
        time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()), a.gateway, a.model, a.provider or 'default routing'))
    total, rejected = 0.0, []
    for n in range(len(ROUNDS) + 1):
        msgs = conversation(n)
        body = {'model': a.model, 'messages': msgs, 'tools': TOOLS,
                'reasoning': {'max_tokens': THINKING_TOKENS}, 'max_tokens': THINKING_TOKENS + ANSWER_TOKENS}
        if a.provider:
            body['provider'] = {'order': [a.provider], 'allow_fallbacks': False}
        ours = json.dumps(body).count('"cache_control"')
        n_tool = sum(1 for m in msgs if m['role'] == 'tool')
        head = '  request %d: %d tool results | %d messages | cache_control sent by us: %d' % (n + 1, n_tool, len(msgs), ours)
        if a.dry_run:
            print(head + ' | %d characters' % len(json.dumps(body)))
            continue
        try:
            resp = post(base, key, body)
        except urllib.error.HTTPError as e:
            err = SECRET.sub(r'\1_***', e.read().decode()[:600])
            m = re.search(r'Found (\d+)', err)
            print(head + ' | REJECTED HTTP %d: %s' % (e.code, err))
            if m:
                rejected.append((n + 1, int(m.group(1))))
            continue
        except Exception as e:
            print(head + ' | %s: %s' % (type(e).__name__, e))
            continue
        usage = resp.get('usage') or {}
        ptd = usage.get('prompt_tokens_details') or {}
        c = cost_of(usage)
        total += c or 0
        print(head + ' | accepted: prompt %d | read from cache %d | written to cache %d | provider %s | cost %s' % (
            usage.get('prompt_tokens') or 0, ptd.get('cached_tokens') or 0, ptd.get('cache_write_tokens') or 0,
            resp.get('provider'), '$%.4f' % c if c is not None else 'NOT REPORTED'))
    if a.dry_run:
        print('dry run: no API call made')
        return
    print('total cost $%.4f' % total)
    if rejected:
        for req, found in rejected:
            print('-> RESULT: request %d: we sent 0 cache_control blocks, the provider counted %d.' % (req, found))
    else:
        print('-> RESULT: no request was rejected for too many cache_control blocks.')


if __name__ == '__main__':
    main()
