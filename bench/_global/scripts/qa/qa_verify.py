"""A8 script checks of one attempt's question (GEOS_SWEEP_BENCH_AGENT_v1.md) -> question/verify.json.

  python qa_verify.py --attempt-dir <deck>/<RUN_TAG>/attempt_<n> [--freeze]

Values are recomputed from the simulated points (sweep table, refine/, extra/; qa_sweep), never taken
from the answer key alone. With --freeze and all checks passed, the SHA256 of question.txt is written
and the attempt is marked FROZEN. A frozen question whose text changed is refused.
Prints one line per check (<= ~40 lines). Exit 0 = all passed, 1 = some failed, 2 = input error.
"""
import argparse
import csv
import math
import os
import re
import sys

import qa_common as C
import qa_sweep as W

NUM_RE = re.compile(r'(?<![\w.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:\s*(?:[eE][-+]?\d+|[×x·]\s*10\^?\s*[-+−]?\d+))?(?![\w.])')
TOL_RE = re.compile(r'(within\s+[±]?\s*[\d.]+\s*(%|percent)|\btolerance\b|\baccepted\b|give (one|a single) number|'
                    r'report (one|a single)|answer format|significant figures|answer with one letter)', re.I)
OPT_RE = re.compile(r'(^|\n)\s*\(?[A-D][.)]\s+\S|none of them', re.I)
BANNED_RE = re.compile(r'(\bGEOS\b|\bXML\b|simulat|\.xml\b|\.csv\b|\.hdf5\b|\.h5\b|\.vtk\b|\.vtu\b|\.py\b|'
                       r'\brun_\d+|\bref_\d+|\bext_\d+|/[A-Za-z_]+/)', re.I)
CAMEL_RE = re.compile(r'\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b')


def sigfigs(tok):
    t = tok.replace('−', '-').strip().lstrip('+-')
    t = re.split(r'[eE×x·]', t)[0].strip()
    digits = t.replace('.', '').lstrip('0')
    if '.' not in t:
        digits = digits.rstrip('0') or digits
    return len(digits)


def to_float(tok):
    t = tok.replace('−', '-').replace(' ', '')
    t = re.sub(r'[×x·]10\^?', 'e', t)
    return float(t)


class R:
    def __init__(self):
        self.checks = {}

    def add(self, name, ok, detail=''):
        self.checks[name] = {'ok': bool(ok), 'detail': detail}

    def na(self, name, detail='not applicable'):
        self.checks[name] = {'ok': True, 'detail': detail, 'na': True}


def failed_runs(adir, sources):
    out = []
    ddir = os.path.dirname(adir)
    for s in sources:
        p = os.path.join(ddir, 'attempt_%s' % s[1:], 'sweep', 'failed_runs.csv')
        if os.path.exists(p):
            for r in csv.DictReader(open(p)):
                out.append({k: W.num(v) for k, v in r.items()})
    return out


def grid_of(adir, axes, rows):
    cfgj = C.load_json(os.path.join(adir, 'sweep', 'sweep_config.json'), {}) or {}
    g = {}
    for a in axes:
        vals = (cfgj.get('axes') or cfgj.get('grid') or {}).get(a)
        if vals is None:
            vals = [r.get(a) for r in rows if r.get('_kind') == 'sweep']
        vals = sorted({W.num(v) for v in vals if v not in (None, '')}, key=lambda v: (isinstance(v, str), v))
        g[a] = vals
    return g


def at_case(rows, fixed, extra_free=()):
    return [r for r in rows if all(W.same(r.get(a), v) for a, v in fixed.items() if a not in extra_free)]


def monotonic(pts):
    pts = sorted(pts)
    ys = [y for _, y in pts]
    d = [b - a for a, b in zip(ys, ys[1:])]
    return all(x > 0 for x in d) or all(x < 0 for x in d), len(pts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--attempt-dir', required=True)
    ap.add_argument('--freeze', action='store_true')
    ap.add_argument('--tol-percent', type=float, default=float(os.environ.get('TOLERANCE_PERCENT', 1.0)))
    a = ap.parse_args()
    adir = os.path.abspath(a.attempt_dir)
    ddir = os.path.dirname(adir)
    qdir = os.path.join(adir, 'question')
    key = C.load_json(os.path.join(qdir, 'answer_key.json'))
    qp = os.path.join(qdir, 'question.txt')
    if not key or not os.path.exists(qp):
        print('ERROR: question/question.txt and question/answer_key.json are required')
        sys.exit(2)
    text = open(qp).read().strip()
    sha = C.sha256_file(qp)
    old = C.load_json(os.path.join(qdir, 'verify.json'), {}) or {}
    if old.get('frozen') and old.get('question_sha256') != sha:
        print('ERROR: the question was frozen with SHA256 %s and its text has changed; frozen questions '
              'are never edited' % old.get('question_sha256', '')[:12])
        sys.exit(2)
    qtype = key.get('question_type')
    rows, axes, sources = W.load_points(adir)
    own = sources[0]
    r = R()
    tf = key.get('type_fields') or {}
    case = key.get('case') or {}
    free = [x for x, v in case.items() if v in ('hidden', 'searched')]
    fixed = {x: W.num(v) for x, v in case.items() if x not in free}
    tv = key.get('true_value')
    tol = key.get('tolerance_abs')
    ref = key.get('refinement') or {}

    # ---- answer key basics
    need = ['question_type', 'observable', 'units', 'true_value', 'tolerance_type', 'tolerance_value',
            'tolerance_abs', 'case', 'provenance', 'list_b_items', 'hardness_rationale']
    if qtype in C.OUTPUT_TYPES:
        need.append('answer_column')
    miss = [k for k in need if key.get(k) in (None, '')]
    r.add('answer_key_fields', not miss and qtype in C.QUESTION_TYPES and key.get('answer_type', 'numeric') == 'numeric',
          'missing: %s' % miss if miss else qtype)
    if miss or qtype not in C.QUESTION_TYPES:
        return finish(r, qdir, adir, sha, a.freeze)
    tv, tol = float(tv), float(tol)
    expected_qt = os.environ.get('QUESTION_TYPE')
    if expected_qt:
        r.add('question_type_matches_config', qtype == expected_qt, '%s vs %s' % (qtype, expected_qt))

    # ---- tolerance
    if key['tolerance_type'] == 'relative':
        exp = a.tol_percent / 100 * abs(tv)
        r.add('tolerance_abs_consistent', abs(tol - exp) <= 1e-6 * max(exp, 1e-300),
              'tolerance_abs %.6g, expected %.6g' % (tol, exp))
    else:
        col = key.get('answer_column') or tf.get('output_column')
        vals = [x.get(col) for x in rows if isinstance(x.get(col), float)]
        rng = (max(vals) - min(vals)) if vals else None
        ok = (qtype not in C.INPUT_TYPES and rng is not None and abs(tv) < 0.1 * rng
              and abs(tol - a.tol_percent / 100 * rng) <= 1e-6 * rng)
        r.add('tolerance_type_rule', ok, 'absolute: |true| %.4g, range %s' % (abs(tv), rng))

    # ---- truth recomputed
    prov = key.get('provenance') or {}
    if qtype in C.OUTPUT_TYPES:
        col = key['answer_column']
        rid = ref.get('final_run') or (prov.get('runs') or [None])[0]
        row = W.find_run(rows, rid, own) if rid else None
        val = row.get(col) if row else None
        r.add('true_value_recomputed', isinstance(val, float) and abs(val - tv) <= 1e-9 * max(abs(tv), 1e-300) + 1e-300,
              'run %s column %s = %s; key %s' % (rid, col, val, tv))
        prow = [row] if row else []
    else:
        br = ref.get('final_bracket')
        okb = isinstance(br, list) and len(br) == 2
        mid = (br[0] + br[1]) / 2 if okb else None
        r.add('true_value_recomputed', okb and abs(mid - tv) <= 1e-9 * max(abs(tv), 1e-300),
              'bracket midpoint %s; key %s' % (mid, tv))
        prow = [W.find_run(rows, x, own) for x in (ref.get('bracket_runs') or prov.get('runs') or [])]
        prow = [x for x in prow if x]
    if key.get('true_value_grid') is not None and key.get('needs_refinement') and \
            abs(float(key['true_value_grid']) - tv) <= 1e-12 * max(abs(tv), 1e-300):
        r.add('no_stale_grid_value', False, 'true_value equals true_value_grid although refinement was needed')

    # ---- failed / adjacent
    grid = grid_of(adir, axes, rows)
    fails = failed_runs(adir, sources)
    bad = []
    for p in prow:
        for f in fails:
            diffs = [x for x in axes if not W.same(p.get(x), f.get(x))]
            if not diffs:
                bad.append(p['run_id'])
            elif len(diffs) == 1:
                x = diffs[0]
                g = grid.get(x) or []
                try:
                    i, j = g.index(p.get(x)), g.index(f.get(x))
                    if abs(i - j) == 1:
                        bad.append(p['run_id'])
                except ValueError:
                    pass
    r.add('no_failed_or_adjacent_provenance', not bad and bool(prow), 'bad: %s' % bad if bad else '%d provenance run(s)' % len(prow))

    # ---- searched / hidden input
    if qtype == 'infer_then_predict':
        X = (tf.get('hidden_input') or {}).get('name')
        xv = tf.get('inferred_input_value')
        interval = (tf.get('hidden_input') or {}).get('interval')
    elif qtype in C.INPUT_TYPES:
        X, xv, interval = tf.get('searched_axis'), tv, tf.get('interval')
    else:
        X = xv = interval = None
    if X:
        lo, hi = (map(float, interval) if interval else (None, None))
        g = [v for v in grid.get(X, []) if isinstance(v, float)]
        okx = xv is not None and g and min(g) < float(xv) < max(g)
        r.add('answer_not_at_edge', okx, '%s = %s, grid %s..%s' % (X, xv, min(g) if g else None, max(g) if g else None))
        xv = float(xv) if xv is not None else None
        xtol = a.tol_percent / 100 * abs(xv) if xv else 0
        base = tf.get('baseline_value')
        near = [v for v in g if abs(v - xv) <= xtol] if xv is not None else []
        if base is not None and xv is not None and abs(float(base) - xv) <= xtol:
            near.append('baseline')
        r.add('not_guessable', xv is not None and not near and base is not None,
              'equal (within tol) to %s' % near if near else ('baseline_value missing' if base is None else 'ok'))
        if interval and xv is not None:
            w = hi - lo
            okp = abs(xv - (lo + hi) / 2) >= 0.2 * w and min(xv - lo, hi - xv) >= 0.05 * w and g and \
                min(g) <= lo and hi <= max(g)
            r.add('interval_placement', okp, 'x=%.6g in [%.6g, %.6g]' % (xv, lo, hi))
        else:
            r.add('interval_placement', False, 'interval or answer missing')
        # refinement resolution on the input
        br = ref.get('final_bracket')
        if key.get('needs_refinement', True):
            okr = isinstance(br, list) and len(br) == 2 and abs(br[1] - br[0]) <= 0.5 * xtol + 1e-300
            r.add('refinement_resolved_input', okr, 'bracket %s, half tol %.4g' % (br, 0.5 * xtol))
        # monotonic / plateau / jump on the searched relation
        ycol = tf.get('observed_column') if qtype == 'infer_then_predict' else tf.get('output_column')
        pts = [(p[X], p[ycol]) for p in at_case(rows, fixed) if isinstance(p.get(X), float)
               and isinstance(p.get(ycol), float) and lo <= p[X] <= hi] if interval and ycol else []
        if qtype != 'optimisation':
            mono, n = monotonic(pts) if pts else (False, 0)
            r.add('monotonic_in_interval', mono and n >= 3, '%s vs %s: %d points' % (ycol, X, n))
        refp = [(p[X], p[ycol]) for p in at_case(rows, fixed) if p.get('_kind') == 'refine'
                and isinstance(p.get(X), float) and isinstance(p.get(ycol), float)]
        if ycol and pts:
            yr = max(y for _, y in pts) - min(y for _, y in pts) or 1.0
            plateau = [(x1, x2) for i, (x1, y1) in enumerate(refp) for (x2, y2) in refp[i + 1:]
                       if not W.same(x1, x2) and abs(y1 - y2) <= 1e-9 * yr]
            r.add('no_plateau', not plateau, 'equal outputs at %s' % plateau[:2] if plateau else '%d refine points' % len(refp))
        hist = ref.get('history') or []
        if qtype != 'optimisation' and len(hist) >= 2:
            def ydiff(h):
                rr = [W.find_run(rows, x, own) for x in h.get('runs', [])]
                return abs(rr[0][ycol] - rr[1][ycol]) if len(rr) == 2 and all(rr) else None
            d1, d2 = ydiff(hist[-2]), ydiff(hist[-1])
            r.add('no_jump', d1 is not None and d2 is not None and d2 <= 0.9 * d1,
                  'last two bracket output differences %s -> %s' % (d1, d2))
        elif qtype != 'optimisation' and key.get('needs_refinement', True):
            r.add('no_jump', False, 'refinement.history needs at least two bisection steps')

    # ---- type-specific
    if qtype == 'infer_then_predict':
        A, B = tf.get('observed_column'), key['answer_column']
        obs = tf.get('observed_value')
        pts = [(p[A], p[B]) for p in at_case(rows, fixed) if p.get('_kind') == 'sweep'
               and isinstance(p.get(A), float) and isinstance(p.get(B), float)]
        r.add('A_and_B_differ', A and A != B, '%s vs %s' % (A, B))
        if len(pts) >= 2 and obs is not None:
            n = len(pts)
            mx = sum(x for x, _ in pts) / n
            my = sum(y for _, y in pts) / n
            sxx = sum((x - mx) ** 2 for x, _ in pts)
            slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0
            naive = my + slope * (float(obs) - mx)
            r.add('nonlinearity', abs(naive - tv) > 3 * tol, 'straight-line B = %.6g vs true %.6g (3 tol = %.4g)'
                  % (naive, tv, 3 * tol))
            eqrow = [x for x, _ in pts if abs(x - float(obs)) <= 1e-9 * max(abs(x), 1e-300)]
            r.add('observed_between_rows', not eqrow, 'observed A equals a row' if eqrow else 'ok')
        else:
            r.add('nonlinearity', False, 'too few sweep points at the case or observed_value missing')
        bruns = [W.find_run(rows, x, own) for x in (ref.get('bracket_runs') or [])]
        if len(bruns) == 2 and all(bruns) and obs is not None:
            (a0, b0), (a1, b1) = [(x[A], x[B]) for x in bruns]
            r.add('refinement_resolved_B', abs(b1 - b0) <= 0.5 * tol, '|B_hi - B_lo| = %.4g, half tol %.4g'
                  % (abs(b1 - b0), 0.5 * tol))
            e = math.floor(math.log10(abs(float(obs)))) if float(obs) else 0
            half = 0.5 * 10 ** (e - 3)
            dbda = (b1 - b0) / (a1 - a0) if a1 != a0 else float('inf')
            r.add('rounding_robustness', abs(dbda * half) < tol / 3, 'dB for half a 4th-s.f. unit of A = %.4g, tol/3 = %.4g'
                  % (abs(dbda * half), tol / 3))
        else:
            r.add('refinement_resolved_B', False, 'refinement.bracket_runs (two runs) required')
            r.add('rounding_robustness', False, 'refinement.bracket_runs (two runs) required')
        chk = tf.get('infer_predict_checks') or {}
        r.add('infer_predict_checks_recorded', all(chk.get(k) is True for k in ('uniqueness', 'nonlinearity',
              'rounding_robustness')), str({k: chk.get(k) for k in ('uniqueness', 'nonlinearity', 'rounding_robustness')}))
        if obs is not None:
            toks = [t for t in NUM_RE.findall(text)]
            hit = [t for t in toks if sigfigs(t) <= 4 and abs(to_float(t) - float(obs)) <= 1e-12 * max(abs(float(obs)), 1e-300)]
            r.add('observed_value_in_text', bool(hit) and sigfigs(C.sig4(float(obs))) == 4,
                  'observed %s %s in text' % (obs, 'found' if hit else 'NOT'))
    elif qtype == 'optimisation':
        X, ycol = tf.get('searched_axis'), tf.get('output_column')
        sign = -1.0 if tf.get('sense') == 'min' else 1.0
        pts = sorted((p[X], sign * p[ycol]) for p in at_case(rows, fixed) if isinstance(p.get(X), float)
                     and isinstance(p.get(ycol), float))
        xtol = a.tol_percent / 100 * abs(tv)
        if len(pts) >= 3:
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            k = max(range(len(ys)), key=lambda i: ys[i])
            okp, det = 0 < k < len(ys) - 1, []
            for side in (-1, 1):
                idx = range(k - 1, -1, -1) if side < 0 else range(k + 1, len(ys))
                idx = list(idx)
                if not idx:
                    okp = False
                    continue
                near = [i for i in idx if abs(xs[i] - xs[k]) <= 2 * xtol]
                if not near:
                    okp = False
                    det.append('no point within 2 tol on side %d' % side)
                    continue
                far = idx[-1]
                avg = abs(ys[k] - ys[far]) / abs(xs[k] - xs[far])
                loc = max(abs(ys[k] - ys[i]) / abs(xs[k] - xs[i]) for i in near)
                if loc > avg:
                    okp = False
                    det.append('cliff on side %d' % side)
                # walking from the interval end toward the peak the value should rise; the largest drop
                # seen on the way is the noise
                walk = idx[::-1] + [k]
                noise = max([0.0] + [ys[i] - ys[j] for i, j in zip(walk, walk[1:])])
                flat = [i for i in idx if abs(xs[i] - xs[k]) > xtol and ys[k] - ys[i] <= noise]
                if flat:
                    okp = False
                    det.append('flat on side %d' % side)
            r.add('clear_peak', okp, '; '.join(det) or 'peak at %.6g' % xs[k])
        else:
            r.add('clear_peak', False, 'fewer than 3 points at the case')
    elif qtype == 'timing':
        dt, dout = tf.get('time_step'), tf.get('output_interval')
        r.add('time_step_resolution', dt is not None and float(dt) <= 0.5 * tol, 'time step %s, half tol %.4g' % (dt, 0.5 * tol))
        r.add('output_interval_resolution', dout is not None and float(dout) <= 0.5 * tol,
              'output interval %s, half tol %.4g' % (dout, 0.5 * tol))

    # ---- text checks
    kw = (C.load_json(os.path.join(ddir, 'source', 'deck_keywords.json'), {}) or {}).get('keywords', [])
    hits = [k for k in kw if k and re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(k), text)]
    r.add('no_deck_keywords', not hits, 'found: %s' % hits[:8] if hits else '%d keywords checked' % len(kw))
    camel = sorted(set(CAMEL_RE.findall(text)) - set(key.get('allowed_camelcase') or []))
    r.add('no_camelcase', not camel, 'found: %s' % camel[:8] if camel else 'ok')
    ban = sorted(set(m.group(0) for m in BANNED_RE.finditer(text)))
    r.add('no_banned_terms', not ban, 'found: %s' % ban[:8] if ban else 'ok')
    t = TOL_RE.findall(text)
    r.add('no_tolerance_or_format_statement', not t, 'found: %s' % [x[0] for x in t][:4] if t else 'ok')
    r.add('no_options_in_text', not OPT_RE.search(text), 'ok' if not OPT_RE.search(text) else 'option-like text found')
    r.add('ends_with_question_mark', text.endswith('?'), text[-40:])
    exempt = set(str(x) for x in (key.get('exempt_numbers') or []))
    long = [tok for tok in NUM_RE.findall(text) if sigfigs(tok) > 4 and tok.strip() not in exempt]
    r.add('at_most_4_significant_figures', not long, 'found: %s' % long[:6] if long else 'ok')

    # ---- list b1
    lb = key.get('list_b_items')
    okb, det = isinstance(lb, list), []
    norm = lambda s: re.sub(r'\s+', ' ', s or '').strip().lower()
    for it in lb or []:
        h = (it.get('handling') or '').lower()
        if 'artifact' in (it.get('item') or '').lower() and h != 'not relevant':
            okb = False
            det.append('ARTIFACT affects answer: %s' % it.get('item'))
        if h == 'stated' and norm(it.get('sentence')) not in norm(text):
            okb = False
            det.append('sentence not in text: %s' % (it.get('sentence') or '')[:60])
        if h not in ('stated', 'not relevant'):
            okb = False
            det.append('bad handling %r' % h)
    r.add('list_b_items', okb, '; '.join(det) or '%d items' % len(lb or []))
    return finish(r, qdir, adir, sha, a.freeze)


def finish(r, qdir, adir, sha, freeze):
    failed = [k for k, v in r.checks.items() if not v['ok']]
    out = {'checks': r.checks, 'all_passed': not failed, 'failed_checks': failed, 'checked_utc': C.now_utc(),
           'question_sha256_checked': sha}
    old = C.load_json(os.path.join(qdir, 'verify.json'), {}) or {}
    out['rewrites'] = (old.get('rewrites', 0) + 1) if old and old.get('question_sha256_checked') not in (None, sha) else old.get('rewrites', 0)
    if old.get('frozen'):
        out.update({'frozen': True, 'question_sha256': old.get('question_sha256')})
    elif freeze and not failed:
        out.update({'frozen': True, 'question_sha256': sha})
        C.set_status(adir, 'A8', 'FROZEN', question_sha256=sha)
    C.save_json(os.path.join(qdir, 'verify.json'), out)
    for k, v in r.checks.items():
        print('%-34s %s  %s' % (k, 'n/a ' if v.get('na') else ('ok  ' if v['ok'] else 'FAIL'), str(v['detail'])[:110]))
    print('ALL PASSED%s' % (' — FROZEN %s' % sha[:12] if out.get('frozen') else '') if not failed
          else '%d FAILED: %s' % (len(failed), ', '.join(failed)))
    sys.exit(0 if not failed else 1)


if __name__ == '__main__':
    main()
