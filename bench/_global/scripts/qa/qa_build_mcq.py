"""Build the MCQ of one attempt (Section 9): distractors from the sweep, options, shuffles -> question/mcq.json.

  python qa_build_mcq.py --attempt-dir <deck>/<RUN_TAG>/attempt_<n> --d-min-pct 3 --d-max-pct 12 --reason "..." \
        --placement above|below|straddle
        [--n-shuffles 4] [--max-pct 25]

Prints the options and the distractors' provenance, never the letter keys.
Exit 0 = built; 2 = fewer than two simulated candidates (the controller may add extra runs and rerun;
add --allow-random to use the seeded random fallback); 3 = NO_VALID_OPTIONS; 1 = input error.
"""
import argparse
import os
import random
import sys

import qa_common as C
import qa_sweep as W


def die(msg, code=1):
    print('ERROR: ' + msg)
    sys.exit(code)


def candidates(key, rows):
    qtype = key['question_type']
    case = key.get('case') or {}
    free = [a for a, v in case.items() if v in ('hidden', 'searched')]
    fixed = {a: W.num(v) for a, v in case.items() if a not in free}
    out = []
    if qtype in C.OUTPUT_TYPES:
        col = key['answer_column']
        for r in rows:
            v = r.get(col)
            if not isinstance(v, float):
                continue
            diff = [a for a in fixed if not W.same(r.get(a), fixed[a])]
            if len(diff) <= 1:
                out.append((v, {'source_runs': [r['run_id']], 'column': col, 'changed_axes': diff,
                                'case': {a: r.get(a) for a in list(fixed) + free}}))
        return out
    tf = key.get('type_fields') or {}
    X = tf['searched_axis']
    ycol = tf['output_column']
    lo, hi = tf['interval']
    groups = {}
    for r in rows:
        if not (isinstance(r.get(X), float) and isinstance(r.get(ycol), float)):
            continue
        diff = [a for a in fixed if not W.same(r.get(a), fixed[a])]
        if len(diff) == 1:
            groups.setdefault(tuple((a, r.get(a)) for a in fixed), []).append(r)
    for g, rs in groups.items():
        rs = sorted(rs, key=lambda r: r[X])
        diff = [a for a, v in g if not W.same(v, fixed[a])]
        if qtype in ('inverse', 'threshold'):
            target = float(tf['target_value'])
            for r0, r1 in zip(rs, rs[1:]):
                y0, y1 = r0[ycol] - target, r1[ycol] - target
                if y0 * y1 <= 0 and y0 != y1:
                    x = r0[X] + (r1[X] - r0[X]) * (-y0) / (y1 - y0)
                    if lo < x < hi:
                        out.append((x, {'source_runs': [r0['run_id'], r1['run_id']], 'column': ycol,
                                        'searched_axis': X, 'changed_axes': diff, 'case': dict(g),
                                        'method': 'linear interpolation of the crossing'}))
        else:
            sign = -1.0 if tf.get('sense') == 'min' else 1.0
            ys = [sign * r[ycol] for r in rs]
            k = max(range(len(ys)), key=lambda i: ys[i])
            if 0 < k < len(ys) - 1:
                (x0, y0), (x1, y1), (x2, y2) = [(rs[i][X], ys[i]) for i in (k - 1, k, k + 1)]
                den = (x0 - x1) * (x0 - x2) * (x1 - x2)
                a = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / den if den else 0
                b = (x2 * x2 * (y0 - y1) + x1 * x1 * (y2 - y0) + x0 * x0 * (y1 - y2)) / den if den else 0
                x = -b / (2 * a) if a < 0 else x1
                if lo < x < hi:
                    out.append((x, {'source_runs': [rs[i]['run_id'] for i in (k - 1, k, k + 1)],
                                    'column': ycol, 'searched_axis': X, 'changed_axes': diff,
                                    'case': dict(g), 'method': 'parabolic extremum'}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--attempt-dir', required=True)
    ap.add_argument('--d-min-pct', type=float, required=True)
    ap.add_argument('--d-max-pct', type=float, required=True)
    ap.add_argument('--reason', required=True)
    ap.add_argument('--placement', required=True, choices=('above', 'below', 'straddle'),
                    help='where the two distractors lie relative to the true value: both above, both below, '
                         'or one on each side (the controller chooses; Section 9.3)')
    ap.add_argument('--n-shuffles', type=int, default=int(os.environ.get('N_SHUFFLES', 4)))
    ap.add_argument('--max-pct', type=float, default=float(os.environ.get('DISTRACTOR_MAX_PCT', 25)))
    ap.add_argument('--allow-random', action='store_true')
    a = ap.parse_args()

    adir = os.path.abspath(a.attempt_dir)
    qdir = os.path.join(adir, 'question')
    key = C.load_json(os.path.join(qdir, 'answer_key.json'))
    ver = C.load_json(os.path.join(qdir, 'verify.json'))
    qpath = os.path.join(qdir, 'question.txt')
    if not key or not ver or not os.path.exists(qpath):
        die('question/answer_key.json, verify.json and question.txt are required (freeze first)')
    sha = C.sha256_file(qpath)
    if not ver.get('all_passed') or ver.get('question_sha256') != sha:
        die('the question is not frozen: verify.json must have all_passed and the SHA256 of question.txt')
    if os.path.exists(os.path.join(qdir, 'mcq.json')):
        old = C.load_json(os.path.join(qdir, 'mcq.json'), {})
        if old.get('question_sha256') == sha and old.get('frozen'):
            die('mcq.json is already final for this question (evaluation may have started)')
    tv = float(key['true_value'])
    ttype = key.get('tolerance_type', 'relative')
    tol = float(key['tolerance_abs'])
    if a.d_max_pct > a.max_pct or a.d_min_pct >= a.d_max_pct:
        die('band must satisfy D_MIN_PCT < D_MAX_PCT <= %g' % a.max_pct)
    rows, axes, sources = W.load_points(adir)
    if not rows:
        die('no simulated points found for this attempt')

    # band in absolute units
    if ttype == 'absolute':
        col = key['answer_column'] if key['question_type'] in C.OUTPUT_TYPES else key['type_fields']['output_column']
        vals = [r[col] for r in rows if isinstance(r.get(col), float)]
        scale = (max(vals) - min(vals)) if vals else abs(tv)
        band_basis = 'full sweep range of %s' % col
    else:
        scale = abs(tv)
        band_basis = '|true value|'
    lo_abs, hi_abs = a.d_min_pct / 100 * scale, a.d_max_pct / 100 * scale
    if lo_abs <= tol:
        die('D_MIN_PCT too small: %.4g%% of %s = %.4g is not above the tolerance %.4g'
            % (a.d_min_pct, band_basis, lo_abs, tol))
    fmt = lambda v: C.fmt_option(v, ttype, tol)
    ftrue = fmt(tv)
    tf = key.get('type_fields') or {}

    def valid(v):
        d = abs(v - tv)
        if not (lo_abs <= d <= hi_abs) or d <= tol:
            return False
        if ttype != 'absolute' and tv != 0 and v * tv <= 0:
            return False
        if key['question_type'] in C.INPUT_TYPES:
            lo, hi = tf['interval']
            if not (lo < v < hi):
                return False
        fv = fmt(v)
        return fv != ftrue and abs(float(fv) - tv) > tol

    cands = [(v, d) for v, d in candidates(key, rows) if valid(v)]
    # unique formatted values
    seen, uniq = set(), []
    for v, d in sorted(cands, key=lambda x: (len(x[1]['changed_axes']), abs(x[0] - tv))):
        f = fmt(v)
        if f not in seen:
            seen.add(f)
            uniq.append((v, d))
    # the placement the controller chose: both above, both below, or one on each side of the true value
    side_ok = {'above': lambda v: v > tv, 'below': lambda v: v < tv, 'straddle': lambda v: True}[a.placement]
    uniq = [(v, d) for v, d in uniq if side_ok(v)]
    best, best_s = None, None
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            (v1, d1), (v2, d2) = uniq[i], uniq[j]
            if abs(float(fmt(v1)) - float(fmt(v2))) <= tol:
                continue
            if a.placement == 'straddle' and (v1 - tv) * (v2 - tv) >= 0:
                continue
            s = (len(d1['changed_axes']) + len(d2['changed_axes']),
                 0 if d1.get('case') != d2.get('case') else 1)
            if best_s is None or s < best_s:
                best, best_s = (uniq[i], uniq[j]), s
    picked = list(best) if best else (uniq[:1] if uniq else [])
    n_sim = len(picked)
    if n_sim < 2 and not a.allow_random:
        print('only %d valid simulated candidate(s) in the band with placement %s (of %d simulated points); '
              'add extra runs of neighbouring cases under sweep/extra/ and rerun, choose another placement, '
              'or rerun with --allow-random' % (n_sim, a.placement, len(rows)))
        sys.exit(2)
    seed_base = int(sha[:8], 16)
    prov = [dict(d, source='sweep', value=v, percent_from_true=round(abs(v - tv) / abs(tv) * 100, 3) if tv else None)
            for v, d in picked]
    vals = [v for v, _ in picked]
    tries = 0
    while len(vals) < 2:
        tries += 1
        if tries > 100:
            print('NO_VALID_OPTIONS: no valid random value in the band after 100 tries')
            sys.exit(3)
        rng = random.Random(seed_base + 1000 + tries)
        if a.placement == 'above':
            side = 1
        elif a.placement == 'below':
            side = -1
        else:
            side = rng.choice((-1, 1)) if not vals else (-1 if vals[0] > tv else 1)
        pct = rng.uniform(a.d_min_pct, a.d_max_pct)
        v = tv + side * pct / 100 * scale
        if key['question_type'] == 'timing' and tf.get('time_step'):
            dt = float(tf['time_step'])
            v = round(v / dt) * dt
        v = float(fmt(v))
        if valid(v) and all(abs(float(fmt(v)) - float(fmt(w))) > tol for w in vals):
            vals.append(v)
            prov.append({'source': 'random', 'value': v, 'side': 'above' if side > 0 else 'below',
                         'percent': round(pct, 4), 'seed': seed_base + 1000 + tries})
    options = {'true': ftrue, 'd1': fmt(vals[0]), 'd2': fmt(vals[1])}
    shuffles, used = [], set()
    for k in range(1, a.n_shuffles + 1):
        rng = random.Random(seed_base + k)
        # every letter order of A-C (true letter, then the distractors' order); from shuffle 4 on only orders
        # not used yet, so no two shuffles are the same (repeats only after all 6 orders were used)
        orders = [(tl, ds) for tl in 'ABC' for ds in (('d1', 'd2'), ('d2', 'd1'))]
        if k <= 3:
            cand = [o for o in orders if o[0] == 'ABC'[k - 1]]
        else:
            cand = [o for o in orders if o not in used] or orders
        tl, ds = cand[rng.randrange(len(cand))]
        used.add((tl, ds))
        rest = [x for x in 'ABC' if x != tl]
        letters = {tl: 'true', rest[0]: ds[0], rest[1]: ds[1], 'D': 'none'}
        shuffles.append({'k': k, 'seed': seed_base + k, 'letters': {x: letters[x] for x in 'ABCD'}})
    mcq = {'question_sha256': sha, 'true_value': tv, 'units': key.get('units', ''),
           'options_values': {'true': tv, 'd1': vals[0], 'd2': vals[1]}, 'formatted': options,
           'band': {'d_min_pct': a.d_min_pct, 'd_max_pct': a.d_max_pct, 'basis': band_basis,
                    'reason': a.reason, 'placement': a.placement},
           'tolerance_abs': tol, 'tolerance_type': ttype,
           'distractor_provenance': {'d1': prov[0], 'd2': prov[1]},
           'n_simulated_candidates': len(uniq), 'sweep_sources': sources,
           'shuffles': shuffles, 'frozen': False, 'built_utc': C.now_utc()}
    C.save_json(os.path.join(qdir, 'mcq.json'), mcq)
    print('MCQ built for %s (%s)' % (key['question_type'], key.get('units', '')))
    print('  true : %s' % options['true'])
    for d in ('d1', 'd2'):
        p = mcq['distractor_provenance'][d]
        if p['source'] == 'sweep':
            print('  %s   : %s  sweep %s, changed %s, %.2f%% from true' % (
                d, options[d], ','.join(p['source_runs']), p['changed_axes'] or 'none', p['percent_from_true'] or 0))
        else:
            print('  %s   : %s  random fallback (%s, %.2f%%)' % (d, options[d], p['side'], p['percent']))
    print('  D    : None of them;  %d shuffles written (letter keys not shown)' % a.n_shuffles)


if __name__ == '__main__':
    main()
