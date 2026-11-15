"""One chat-completions call through the Nemonist (OpenAI-compatible) gateway + its usage/cost record.

Section 3.1 of GEOS_SWEEP_BENCH_AGENT_v1.md.

  chat(model, messages, tools=None, ..., usage_path=..., tag={...}) -> (message_dict, record)

- The key is read from NEMONIST_API_KEY; it is never printed or written.
- actual cost: usage.cost_details.upstream_inference_cost when usage.is_byok (+ gateway_fee = usage.cost),
  else usage.cost; missing -> None (then the rate cost is used and the call is flagged cost_estimated).
- the record is appended to usage_path the moment the response arrives.
- technical failures (network, HTTP 5xx, timeout, malformed body) are retried once with backoff;
  HTTP 401/402/429 persisting after the retry raises UsageLimit (callers exit 75).
- thinking is requested as reasoning={"max_tokens": REASONING_MAX_TOKENS} (0 = off; then REASONING_EFFORT, if set,
  is sent as reasoning_effort), with max_tokens = REASONING_MAX_TOKENS + MAX_ANSWER_TOKENS.
- for anthropic/* models, cache_control marks the system prompt and the newest message (prefix caching);
  if the gateway rejects that (HTTP 400), the call is repeated once without it and caching is switched off.

Offline testing: QA_LLM_MOCK=/path/mock.py whose respond(model, messages, tools, **kw) returns a
chat-completions response dict; no network call is made then.
"""
import copy
import importlib.util
import json
import os
import socket
import time
import urllib.error
import urllib.request

import qa_common as C


class UsageLimit(Exception):
    def __init__(self, http_status, detail):
        super().__init__('usage limit (HTTP %s): %s' % (http_status, detail[:200]))
        self.http_status = http_status
        self.detail = detail


class CallFailed(Exception):
    pass


class BudgetStop(Exception):
    """Today's spend reached DAILY_BUDGET_USD: stop cleanly like a usage limit (exit 75), resume tomorrow."""
    def __init__(self, spent, budget):
        super().__init__('daily budget reached: $%.4f spent today, budget $%.2f' % (spent, budget))
        self.http_status = 'budget'
        self.detail = str(self)


class ModelMismatch(Exception):
    """The gateway served a different model than requested: stop (Section 3.1)."""


_CACHE_OK = {}
_MOCK = None


def _mock():
    global _MOCK
    p = os.environ.get('QA_LLM_MOCK')
    if not p:
        return None
    if _MOCK is None:
        spec = importlib.util.spec_from_file_location('qa_llm_mock', p)
        _MOCK = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MOCK)
    return _MOCK


def _with_cache_control(messages):
    msgs = copy.deepcopy(messages)

    def mark(m):
        c = m.get('content')
        if isinstance(c, str) and c:
            m['content'] = [{'type': 'text', 'text': c, 'cache_control': {'type': 'ephemeral'}}]
        elif isinstance(c, list) and c and isinstance(c[-1], dict):
            c[-1]['cache_control'] = {'type': 'ephemeral'}
    if msgs and msgs[0].get('role') == 'system':
        mark(msgs[0])
    # one more cache breakpoint, on the newest message only (advisor's test 2026-11-16: fewer misses on
    # Google Vertex than marking the last two user/tool messages)
    if len(msgs) > 1:
        mark(msgs[-1])
    return msgs


def _post(body, timeout):
    base = os.environ.get('NEMONIST_BASE_URL') or C.DEFAULTS['NEMONIST_BASE_URL']
    key = os.environ.get('NEMONIST_API_KEY', '')
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', method='POST',
                                 data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _model_ok(requested, reported):
    if not reported:
        return True
    if reported == requested:
        return True
    tail = reported[len(requested):] if reported.startswith(requested) else None
    return tail is not None and tail[:1] in ('-', '@', ':', '.', '_')


PROVIDER_SLUGS = {'google': 'google-vertex', 'google vertex': 'google-vertex', 'anthropic': 'anthropic',
                  'amazon bedrock': 'amazon-bedrock', 'bedrock': 'amazon-bedrock'}


def provider_slug(name):
    if not name:
        return None
    n = name.strip().lower()
    return PROVIDER_SLUGS.get(n, n.replace(' ', '-'))


def _strip_thinking(messages):
    """Remove the signed thinking blocks of earlier assistant turns IN PLACE (so later turns do not repeat
    the failure). Returns how many were removed."""
    n = 0
    for m in messages:
        if m.get('role') == 'assistant' and m.pop('reasoning_details', None) is not None:
            n += 1
    return n


def chat(model, messages, tools=None, tool_choice=None, timeout=1800, usage_path=None, tag=None,
         bench_dir=None, reasoning_effort=None, provider=None):
    """One call. Returns (message, record). Raises UsageLimit or CallFailed.

    provider: pin the call to this provider (no fallbacks), so the signed thinking blocks a session sends
    back are checked by the provider that made them. On a thinking-signature error the earlier thinking
    blocks are removed and the call is repeated once; a rejected pin is dropped."""
    body = {'model': model, 'messages': messages}
    slug = provider_slug(provider)
    if slug:
        body['provider'] = {'order': [slug], 'allow_fallbacks': False}
    if tools:
        body['tools'] = tools
        if tool_choice:
            body['tool_choice'] = tool_choice
    # Thinking: Nemonist passes it through for Claude only as reasoning.max_tokens (tested 2026-11-15;
    # reasoning_effort had no effect). max_tokens must cover the thinking budget plus the visible answer.
    budget = int(os.environ.get('REASONING_MAX_TOKENS') or C.DEFAULTS['REASONING_MAX_TOKENS'])
    if budget > 0:
        body['reasoning'] = {'max_tokens': budget}
        body['max_tokens'] = budget + int(os.environ.get('MAX_ANSWER_TOKENS') or C.DEFAULTS['MAX_ANSWER_TOKENS'])
    eff = reasoning_effort if reasoning_effort is not None else os.environ.get('REASONING_EFFORT')
    if eff and budget <= 0:
        body['reasoning_effort'] = eff
    use_cache = model.startswith('anthropic/') and _CACHE_OK.get(model, True) \
        and os.environ.get('QA_CACHE_CONTROL', '1') != '0'
    mock = _mock()
    bench = bench_dir or os.environ.get('BENCH_DIR', '')
    budget_usd = float(os.environ.get('DAILY_BUDGET_USD') or C.DEFAULTS['DAILY_BUDGET_USD'])
    if bench and budget_usd > 0:
        spent = C.spent_today(bench)
        if spent >= budget_usd:
            raise BudgetStop(spent, budget_usd)

    last_err, resp, t0 = None, None, time.time()
    fixes = []                       # 400 repairs: each may be applied once and does not use up a retry
    attempt = 0
    while attempt < 2:
        attempt += 1
        b = dict(body, messages=_with_cache_control(messages)) if use_cache else dict(body)
        t0 = time.time()
        try:
            if mock:
                resp = mock.respond(model=model, messages=messages, tools=tools, tool_choice=tool_choice)
            else:
                resp = _post(b, timeout)
            if not resp.get('choices'):
                raise CallFailed('response without choices: %s' % str(resp)[:300])
            break
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode()[:1000]
            except Exception:
                pass
            low = detail.lower()
            if e.code == 400 and 'signature' in low and 'thinking' in low and 'strip' not in fixes:
                fixes.append('strip')
                _strip_thinking(messages)
                last_err = 'HTTP 400 thinking signature: %s' % detail[:200]
                attempt -= 1
                continue
            if e.code in (400, 404) and 'provider' in body and 'provider' in low and 'unpin' not in fixes:
                fixes.append('unpin')
                body.pop('provider', None)
                last_err = 'HTTP %d with provider pin: %s' % (e.code, detail[:200])
                attempt -= 1
                continue
            if e.code == 400 and use_cache and 'cache' in low and 'nocache' not in fixes:
                fixes.append('nocache')
                _CACHE_OK[model] = False
                use_cache = False
                last_err = 'HTTP 400 with cache_control: %s' % detail[:200]
                attempt -= 1
                continue
            if e.code in (401, 402, 429):
                if attempt == 1:
                    time.sleep(30)
                    last_err = 'HTTP %d: %s' % (e.code, detail)
                    continue
                raise UsageLimit(e.code, detail)
            last_err = 'HTTP %d: %s' % (e.code, detail)
            if e.code < 500 and e.code not in (408, 409):
                raise CallFailed(last_err)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                json.JSONDecodeError, CallFailed) as e:
            last_err = '%s: %s' % (type(e).__name__, e)
        if attempt == 1:
            time.sleep(10)
    if resp is None or not resp.get('choices'):
        raise CallFailed(last_err or 'unknown failure')

    wall = time.time() - t0
    usage = resp.get('usage') or {}
    ptd = usage.get('prompt_tokens_details') or {}
    ctd = usage.get('completion_tokens_details') or {}
    cd = usage.get('cost_details') or {}
    prompt = usage.get('prompt_tokens') or 0
    completion = usage.get('completion_tokens') or 0
    cached = ptd.get('cached_tokens') or 0
    cache_write = ptd.get('cache_write_tokens') or 0
    reasoning = ctd.get('reasoning_tokens')
    gw = usage.get('cost')
    if usage.get('is_byok'):
        up = cd.get('upstream_inference_cost')
        actual = None if up is None else up + (gw or 0)
        compare = up
        gateway_fee = gw or 0
    else:
        actual = gw
        compare = gw
        gateway_fee = None
    row = C.pricing_row(bench_dir or os.environ.get('BENCH_DIR', ''), model)
    rate = C.rate_cost(row, prompt, completion, cached, cache_write)
    rec = {
        'ts': C.now_utc(), 'model_requested': model, 'model_reported': resp.get('model'),
        'provider': resp.get('provider'), 'id': resp.get('id'),
        'prompt': prompt, 'completion': completion, 'reasoning': reasoning,
        'cached': cached, 'cache_write': cache_write,
        'is_byok': usage.get('is_byok'), 'gateway_fee': gateway_fee,
        'cost_usd': actual if actual is not None else rate,
        'actual_cost_usd': actual, 'rate_cost_usd': rate,
        'cost_estimated': actual is None,
        'cost_mismatch': bool(compare and rate and abs(compare - rate) > 0.02 * max(compare, rate)),
        'wall_s': round(wall, 2), 'retried': attempt == 2, 'cache_control': use_cache,
        'provider_pinned': body.get('provider', {}).get('order', [None])[0], 'repairs': fixes,
    }
    if tag:
        rec.update(tag)
    if usage_path:
        C.append_jsonl(usage_path, rec)
    if bench:
        C.append_jsonl(C.ledger_path(bench), {'ts': rec['ts'], 'day': C.today_local(), 'model': model,
                                              'cost_usd': rec['cost_usd'], 'role': (tag or {}).get('role')})
    if not _model_ok(model, resp.get('model')):
        raise ModelMismatch('model mismatch: requested %s, reported %s' % (model, resp.get('model')))
    msg = resp['choices'][0].get('message') or {}
    return msg, rec
