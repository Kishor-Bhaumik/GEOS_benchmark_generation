"""Shared helpers of the AGENT v1 harness (GEOS_SWEEP_BENCH_AGENT_v1.md).

Configuration, deck paths, JSON I/O, prompt blocks read from the protocol file, pricing.
Never prints or stores NEMONIST_API_KEY.
"""
import hashlib
import json
import os
import re
import time

PROTOCOL_ID = 'AGENT'
PROTOCOL_VERSION = 1
QUESTION_TYPES = ('infer_then_predict', 'inverse', 'threshold', 'optimisation', 'timing', 'magnitude')
OUTPUT_TYPES = ('infer_then_predict', 'timing', 'magnitude')
INPUT_TYPES = ('inverse', 'threshold', 'optimisation')
USAGE_LIMIT_EXIT = 75

# Section 1 defaults (overridable from the environment).
DEFAULTS = {
    'QUESTION_TYPE': 'infer_then_predict',
    'NEMONIST_BASE_URL': 'https://nemonist.com/api/v1',
    'MAX_ATTEMPTS': 3, 'TOLERANCE_PERCENT': 1.0,
    'N_SHUFFLES': 4, 'NO_TOOL_FAIL_MIN': 3, 'AGENT_PASS_MIN': 3,
    'MAX_SIMULATIONS_PER_ATTEMPT': 200, 'REFINEMENT_RESERVE_RUNS': 30, 'MAX_REFINEMENT_RUNS': 12,
    'MAX_FAILED_RUN_PERCENT': 5, 'DISTRACTOR_MAX_PCT': 25,
    'CONTROLLER_MAX_TURNS': 400, 'CONTROLLER_MAX_WALL_S': 21600, 'CONTROLLER_MAX_SESSIONS': 2,
    'NO_TOOL_TIMEOUT_S': 1800, 'AGENT_MAX_WALL_S': 7200, 'AGENT_MAX_TURNS': 300,
    'AGENT_MAX_GEOS_RUNS': 60, 'AGENT_CORES': 16, 'AGENT_NET': 'on',
    'TOOL_OUTPUT_MAX_CHARS': 6000,
    'REASONING_MAX_TOKENS': 16000, 'MAX_ANSWER_TOKENS': 16000,
    'DAILY_BUDGET_USD': 8.0,
}
REQUIRED = ('RUN_TAG', 'BENCH_DIR', 'GEOS_ROOT', 'INPUT_XML', 'GEOS_BIN', 'PROTOCOL_FILE',
            'PARALLEL_JOBS', 'MODEL')
OPTIONAL_EMPTY = ('REASONING_EFFORT', 'AGENT_RO_BINDS', 'AGENT_VISIBLE_UNTRACKED')


def config_from_env():
    """Section 1 values from the environment. Returns (cfg, missing)."""
    cfg, missing = {}, []
    for k in REQUIRED:
        v = os.environ.get(k, '')
        if not v:
            missing.append(k)
        cfg[k] = v
    for k in OPTIONAL_EMPTY:
        cfg[k] = os.environ.get(k, '')
    for k in ('GEN_MODEL', 'NO_TOOL_MODEL', 'TOOL_MODEL'):
        cfg[k] = os.environ.get(k) or cfg['MODEL']
    for k, d in DEFAULTS.items():
        v = os.environ.get(k)
        if v in (None, ''):
            cfg[k] = d
        elif isinstance(d, bool):
            cfg[k] = v.lower() in ('1', 'true', 'on', 'yes')
        elif isinstance(d, int):
            cfg[k] = int(v)
        elif isinstance(d, float):
            cfg[k] = float(v)
        else:
            cfg[k] = v
    if cfg.get('PARALLEL_JOBS'):
        cfg['PARALLEL_JOBS'] = int(cfg['PARALLEL_JOBS'])
    cfg['AGENT_STOP_FAILS'] = cfg['N_SHUFFLES'] - cfg['AGENT_PASS_MIN'] + 1
    if not os.environ.get('NEMONIST_API_KEY'):
        missing.append('NEMONIST_API_KEY')
    if cfg['QUESTION_TYPE'] not in QUESTION_TYPES:
        missing.append('QUESTION_TYPE (must be one of %s)' % ', '.join(QUESTION_TYPES))
    return cfg, missing


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def deck_slug(geos_root, input_xml):
    p = os.path.join(geos_root, input_xml)
    stem = os.path.splitext(os.path.basename(p))[0]
    return '%s__%s' % (stem, sha256_file(p)[:8])


def deck_dir(cfg):
    return os.path.join(cfg['BENCH_DIR'], 'decks', deck_slug(cfg['GEOS_ROOT'], cfg['INPUT_XML']), cfg['RUN_TAG'])


def attempt_dir(ddir, n):
    return os.path.join(ddir, 'attempt_%d' % n)


def load_json(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(p, obj):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2)
        f.write('\n')
    os.replace(tmp, p)


def append_jsonl(p, obj):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, 'a') as f:
        f.write(json.dumps(obj) + '\n')
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(p):
    out = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return out


def now_utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def set_status(adir, stage, status, **extra):
    p = os.path.join(adir, 'attempt_status.json')
    d = load_json(p, {}) or {}
    d.update({'stage': stage, 'status': status, 'timestamp_utc': now_utc()})
    d.update(extra)
    save_json(p, d)
    return d


def prompt_block(protocol_file, name):
    """Text of the ```text fence under the heading '### Prompt: <name>' of the protocol file."""
    txt = open(protocol_file).read()
    m = re.search(r'^### Prompt: %s\s*$' % re.escape(name), txt, re.M)
    if not m:
        raise KeyError('prompt block %s not found in %s' % (name, protocol_file))
    f = re.search(r'```text\n(.*?)\n```', txt[m.end():], re.S)
    if not f:
        raise KeyError('prompt block %s has no ```text fence' % name)
    return f.group(1)


def fill(template, **kw):
    out = template
    for k, v in kw.items():
        out = out.replace('{%s}' % k, str(v))
    return out


def interruption(bench_dir, stage, model, http_status, detail=''):
    p = os.path.join(bench_dir, '_global', 'controller_logs', 'interruptions.json')
    d = load_json(p, []) or []
    d.append({'kind': 'USAGE_LIMIT_REACHED', 'stage': stage, 'model': model,
              'http_status': http_status, 'detail': detail[:300], 'timestamp_utc': now_utc()})
    save_json(p, d)


# ---------------------------------------------------------------- costs

def pricing_row(bench_dir, model):
    rows = load_json(os.path.join(bench_dir, '_global', 'pricing.json'), {}) or {}
    return rows.get(model)


def rate_cost(row, prompt, completion, cached, cache_write):
    """Cost from pricing.json rates (USD), Section 11. None if no row."""
    if not row:
        return None
    r = row
    thr = row.get('long_context_threshold_prompt_tokens')
    if thr:
        basis = row.get('long_context_threshold_basis', 'input')
        count = prompt if basis == 'input+cache' else max(prompt - cached - cache_write, 0)
        if count > thr and row.get('long_context'):
            r = dict(row, **row['long_context'])
    uncached = max(prompt - cached - cache_write, 0)
    return (uncached * r['input'] + cached * r.get('cache_read', r['input'])
            + cache_write * r.get('cache_write', r['input']) + completion * r['output']) / 1e6


def sum_usage(records):
    """Sum a list of qa_llm usage records."""
    tot = {'calls': 0, 'prompt': 0, 'completion': 0, 'reasoning': 0, 'cached': 0, 'cache_write': 0,
           'actual_cost_usd': 0.0, 'rate_cost_usd': 0.0, 'cost_estimated_calls': 0,
           'cost_mismatch_calls': 0, 'wall_s': 0.0}
    for r in records:
        tot['calls'] += 1
        for k in ('prompt', 'completion', 'reasoning', 'cached', 'cache_write'):
            tot[k] += r.get(k) or 0
        c = r.get('cost_usd')
        if c is not None:
            tot['actual_cost_usd'] += c
        tot['rate_cost_usd'] += r.get('rate_cost_usd') or 0.0
        tot['cost_estimated_calls'] += 1 if r.get('cost_estimated') else 0
        tot['cost_mismatch_calls'] += 1 if r.get('cost_mismatch') else 0
        tot['wall_s'] += r.get('wall_s') or 0.0
    tot['actual_cost_usd'] = round(tot['actual_cost_usd'], 8)
    tot['rate_cost_usd'] = round(tot['rate_cost_usd'], 8)
    tot['wall_s'] = round(tot['wall_s'], 1)
    return tot


# ---------------------------------------------------------------- numbers

def sig4(v):
    """v to 4 significant figures, keeping trailing zeros (e.g. 1.200, 0.03400, 2.874e+06)."""
    import math
    if v == 0:
        return '0.000'
    e = int(math.floor(math.log10(abs(v))))
    if -4 <= e < 4:
        return '%.*f' % (max(0, 3 - e), v)
    return '%.3e' % v


def fmt_option(v, tolerance_type, tolerance_abs):
    """Option format of Section 9.1: 4 s.f. (relative), or decimals resolving tol/10 (absolute)."""
    import math
    if tolerance_type == 'absolute' and tolerance_abs:
        dec = max(0, int(math.ceil(-math.log10(tolerance_abs / 10.0))))
        return '%.*f' % (dec, v)
    return sig4(v)


# ---------------------------------------------------------------- daily budget

def ledger_path(bench_dir):
    return os.path.join(bench_dir, '_global', 'spend_ledger.jsonl')


def today_local():
    return time.strftime('%Y-%m-%d', time.localtime())


def spent_today(bench_dir):
    """USD spent today (server local date) by every harness call, from the shared spend ledger."""
    day = today_local()
    return round(sum(r.get('cost_usd') or 0 for r in read_jsonl(ledger_path(bench_dir)) if r.get('day') == day), 6)


def generator_memory_dir(bench_dir):
    """The generator's persistent Nemonist memory folder, shared by all decks and runs (never visible to gates)."""
    return os.path.join(bench_dir, '_global', 'generator_memory')
