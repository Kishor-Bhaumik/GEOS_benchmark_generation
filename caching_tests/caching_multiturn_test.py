#!/usr/bin/env python3
"""Multi-turn prompt-caching test for Claude through an OpenAI-compatible gateway (Nemonist or OpenRouter).

Reproduces the caching tests behind evidence_caching/*.txt. No dependency on the QA harness: standard
library only. Requests are built the same way as the harness (qa_llm.py): cache_control on the system prompt
and on the last two user/tool messages, thinking as reasoning={"max_tokens": N}, provider pinned without
fallbacks, earlier thinking never sent back.

Modes
  grow : a conversation that grows by ~5k tokens per call (like an agent session). With working prefix
         caching, call k should read about the whole prompt of call k-1 from cache.
  same : the identical request sent twice. The 2nd call should read ~100% from cache.

Keys are read from the environment, never printed or stored:
  NEMONIST_API_KEY    (gateway nemonist,   base URL NEMONIST_BASE_URL,   default https://nemonist.com/api/v1)
  OPENROUTER_API_KEY  (gateway openrouter, base URL OPENROUTER_BASE_URL, default https://openrouter.ai/api/v1)

Examples
  # free: print the request bodies only, no API call
  python caching_multiturn_test.py --dry-run
  # Nemonist, default routing, 3 growing calls (~$0.05)
  python caching_multiturn_test.py --gateway nemonist
  # Nemonist pinned to Google Vertex / Anthropic / Bedrock
  python caching_multiturn_test.py --gateway nemonist --provider google-vertex
  python caching_multiturn_test.py --gateway nemonist --provider anthropic
  # OpenRouter for comparison
  python caching_multiturn_test.py --gateway openrouter --provider anthropic
  # tools allowed (Nemonist then adds its hidden memory instructions), thinking off, marker on last msg only
  python caching_multiturn_test.py --tools --thinking 0 --markers last
"""
import argparse
import copy
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

GATEWAYS = {
    'nemonist': ('NEMONIST_API_KEY', 'NEMONIST_BASE_URL', 'https://nemonist.com/api/v1'),
    'openrouter': ('OPENROUTER_API_KEY', 'OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
}

# ~1k-token system prompt (above Claude's 1024-token minimum cacheable prefix together with turn 1)
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


def filler(turn, n_words, seed):
    """Deterministic, turn-specific notes text (~1.35 tokens per word)."""
    rng = random.Random(seed * 1000 + turn)
    lines = []
    for i in range(0, n_words, 15):
        lines.append('Note %d.%d: %s.' % (turn, i // 15, ' '.join(rng.choice(WORDS) for _ in range(15))))
    return '\n'.join(lines)


def mark(m):
    c = m.get('content')
    if isinstance(c, str) and c:
        m['content'] = [{'type': 'text', 'text': c, 'cache_control': {'type': 'ephemeral'}}]
    elif isinstance(c, list) and c and isinstance(c[-1], dict):
        c[-1]['cache_control'] = {'type': 'ephemeral'}


def with_markers(messages, style):
    """harness: system + last two user/tool messages (same as qa_llm._with_cache_control);
    last: last message only; none: no cache_control at all."""
    msgs = copy.deepcopy(messages)
    if style == 'none':
        return msgs
    if style == 'last':
        mark(msgs[-1])
        return msgs
    if msgs and msgs[0].get('role') == 'system':
        mark(msgs[0])
    marked = 0
    for m in reversed(msgs[1:]):
        if m.get('role') in ('user', 'tool'):
            mark(m)
            marked += 1
            if marked == 2:
                break
    return msgs


def post(base, key, body, timeout=600):
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', method='POST',
                                 data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cost_of(usage):
    if usage.get('is_byok'):
        up = (usage.get('cost_details') or {}).get('upstream_inference_cost')
        return None if up is None else up + (usage.get('cost') or 0)
    return usage.get('cost')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gateway', choices=GATEWAYS, default='nemonist')
    ap.add_argument('--model', default='anthropic/claude-sonnet-5.5')
    ap.add_argument('--provider', help='pin to this provider slug, no fallbacks (e.g. google-vertex, anthropic, '
                                       'amazon-bedrock); default: gateway routing')
    ap.add_argument('--mode', choices=['grow', 'same'], default='grow')
    ap.add_argument('--calls', type=int, default=3, help='number of calls in grow mode (default 3)')
    ap.add_argument('--turn-words', type=int, default=2200, help='notes words per call (default 2200, about 5k tokens)')
    ap.add_argument('--markers', choices=['harness', 'last', 'none'], default='harness')
    ap.add_argument('--thinking', type=int, default=1024, help='reasoning.max_tokens; 0 = thinking off')
    ap.add_argument('--answer-tokens', type=int, default=300)
    ap.add_argument('--tools', action='store_true', help='send bash/read_file/write_file tool definitions '
                    '(tool calls are answered "not executed")')
    ap.add_argument('--seed', type=int, default=None, help='notes seed; default = time, so a rerun does not '
                    'hit cache entries of an earlier run')
    ap.add_argument('--max-cost', type=float, default=0.50, help='stop if the run exceeds this many USD')
    ap.add_argument('--dry-run', action='store_true', help='print the request bodies, make no API call')
    ap.add_argument('--out', help='also append the printed report to this file')
    a = ap.parse_args()

    seed = a.seed if a.seed is not None else int(time.time())
    key_env, base_env, base_default = GATEWAYS[a.gateway]
    base = os.environ.get(base_env) or base_default
    key = os.environ.get(key_env, '')
    if not a.dry_run and not key:
        sys.exit('%s is not set (source the env file first)' % key_env)

    out = open(a.out, 'a') if a.out else None

    def say(s=''):
        print(s, flush=True)
        if out:
            out.write(s + '\n')
            out.flush()

    say('=== %s | gateway %s | model %s | provider %s | markers %s | thinking %s | tools %s | seed %d' % (
        time.strftime('%Y-%m-%d %H:%M:%S'), a.gateway, a.model, a.provider or 'default routing',
        a.markers, a.thinking or 'off', 'on' if a.tools else 'off', seed))

    messages = [{'role': 'system', 'content': SYSTEM}]
    n_calls = 2 if a.mode == 'same' else a.calls
    rows, total = [], 0.0
    for k in range(1, n_calls + 1):
        if a.mode == 'grow' or k == 1:
            # grow mode: call 1 is the system prompt + a short question (~1k tokens, as in the earlier tests);
            # every later call adds a ~5k-token block of notes
            notes = filler(k, a.turn_words, seed) if (k > 1 or a.mode == 'same') else filler(k, 15, seed)
            messages.append({'role': 'user', 'content': 'Project notes, part %d:\n%s\n\nIn one sentence: which '
                             'word appears first in part %d?' % (k, notes, k)})
        body = {'model': a.model, 'messages': with_markers(messages, a.markers)}
        if a.provider:
            body['provider'] = {'order': [a.provider], 'allow_fallbacks': False}
        if a.thinking > 0:
            body['reasoning'] = {'max_tokens': a.thinking}
            body['max_tokens'] = a.thinking + a.answer_tokens
        else:
            body['max_tokens'] = a.answer_tokens
        if a.tools:
            body['tools'] = TOOLS
        if a.dry_run:
            show = copy.deepcopy(body)
            for m in show['messages']:
                for part in (m['content'] if isinstance(m['content'], list) else [m]):
                    t = part.get('text') if 'text' in part else part.get('content')
                    if isinstance(t, str) and len(t) > 160:
                        part['text' if 'text' in part else 'content'] = t[:120] + ' ...[%d chars]' % len(t)
            say('--- call %d request body (texts shortened):' % k)
            say(json.dumps(show, indent=1))
            messages.append({'role': 'assistant', 'content': '(dry-run reply %d)' % k})
            continue

        t0 = time.time()
        try:
            resp = post(base, key, body)
        except urllib.error.HTTPError as e:
            say('call %d: HTTP %d: %s' % (k, e.code, e.read().decode()[:800]))
            break
        except Exception as e:
            say('call %d: %s: %s' % (k, type(e).__name__, e))
            break
        usage = resp.get('usage') or {}
        ptd = usage.get('prompt_tokens_details') or {}
        ctd = usage.get('completion_tokens_details') or {}
        prompt, cached, written = usage.get('prompt_tokens') or 0, ptd.get('cached_tokens') or 0, \
            ptd.get('cache_write_tokens') or 0
        c = cost_of(usage)
        total += c or 0
        msg = (resp.get('choices') or [{}])[0].get('message') or {}
        calls = msg.get('tool_calls') or []
        rows.append((prompt, cached))
        say('  call %d: prompt %6d | read from cache %6d | written to cache %6d | reasoning %s | provider %s | '
            'model %s | %.1fs | cost %s%s' % (
                k, prompt, cached, written, ctd.get('reasoning_tokens'), resp.get('provider'),
                resp.get('model'), time.time() - t0, '$%.4f' % c if c is not None else 'NOT REPORTED',
                ' | tool calls (not executed): %s' % [(t['function']['name'], t['function']['arguments'][:60])
                                                       for t in calls] if calls else ''))

        # earlier thinking is never sent back (same as the harness)
        reply = {'role': 'assistant', 'content': msg.get('content') or ''}
        if calls:
            reply['tool_calls'] = calls
        if a.mode == 'grow':
            messages.append(reply)
            for t in calls:
                messages.append({'role': 'tool', 'tool_call_id': t['id'],
                                 'content': '(not executed in this test)'})
        if total > a.max_cost:
            say('stopping: run cost $%.4f > --max-cost %.2f' % (total, a.max_cost))
            break

    if a.dry_run:
        say('dry run: no API call made')
        return
    say('total cost $%.4f' % total)
    # verdict: with working prefix caching, call k reads about the whole prompt of call k-1
    for k in range(1, len(rows)):
        prev_prompt, (_, cached) = rows[k - 1][0], rows[k]
        want = prev_prompt if a.mode == 'grow' else rows[k][0]
        share = cached / want if want else 0
        say('  call %d read %d of ~%d reusable tokens from cache (%.0f%%) -> %s' % (
            k + 1, cached, want, 100 * share, 'CACHING WORKS' if share >= 0.8 else 'CACHING BROKEN'))
    if out:
        out.close()


if __name__ == '__main__':
    main()
