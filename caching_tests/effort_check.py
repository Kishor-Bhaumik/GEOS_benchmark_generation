#!/usr/bin/env python3
"""Send the same request with effort "low" in two forms and record the exact bodies and the reasoning tokens.

  A: top-level "reasoning_effort": "low"        (what check_api_cost.py --reasoning-style reasoning_effort sends)
  B: "reasoning": {"effort": "low"}             (check_api_cost.py --reasoning-style reasoning_object)

Raw HTTP with Python urllib (no SDK). Key from NEMONIST_API_KEY, never printed. Output: effort_check.txt.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = 'https://nemonist.com/api/v1'
MODEL = 'anthropic/claude-sonnet-5.5'
PROMPT = ('How many positive divisors does 1260 have? Think it through, '
          'then end with one line: FINAL_ANSWER: <number>')     # same prompt as check_api_cost.py

key = os.environ.get('NEMONIST_API_KEY', '')
if not key:
    sys.exit('NEMONIST_API_KEY is not set')
bodies = {
    'A top-level reasoning_effort': {'model': MODEL, 'messages': [{'role': 'user', 'content': PROMPT}],
                                     'reasoning_effort': 'low'},
    'B reasoning.effort': {'model': MODEL, 'messages': [{'role': 'user', 'content': PROMPT}],
                           'reasoning': {'effort': 'low'}},
}
out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'effort_check.txt'), 'a')


def say(s=''):
    print(s)
    out.write(s + '\n')


say('=== effort_check.py | raw HTTP POST %s/chat/completions (Python %s, urllib) | headers: Authorization: '
    'Bearer <key>, Content-Type: application/json' % (BASE, sys.version.split()[0]))
for name, body in bodies.items():
    sent = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
    say('--- %s | sent %s' % (name, sent))
    say('body: ' + json.dumps(body))
    req = urllib.request.Request(BASE + '/chat/completions', method='POST', data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        say('HTTP %d: %s' % (e.code, e.read().decode()[:500]))
        continue
    u = resp.get('usage') or {}
    msg = (resp.get('choices') or [{}])[0].get('message') or {}
    say('response id %s | model %s | provider %s' % (resp.get('id'), resp.get('model'), resp.get('provider')))
    say('usage: prompt %s | completion %s | reasoning_tokens %s | cost %s' % (
        u.get('prompt_tokens'), u.get('completion_tokens'),
        (u.get('completion_tokens_details') or {}).get('reasoning_tokens'),
        (u.get('cost_details') or {}).get('upstream_inference_cost', u.get('cost'))))
    say('answer tail: ' + (msg.get('content') or '')[-80:].replace('\n', ' '))
out.close()
