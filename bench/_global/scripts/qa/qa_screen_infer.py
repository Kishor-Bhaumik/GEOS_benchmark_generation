"""Screen the sweep table for good infer_then_predict questions (Section 8.1) in ONE command.

  python qa_screen_infer.py --attempt-dir <attempt> [--hidden K] [--tol-percent 1.0] [--top 15] [--min-points 4] [--columns a,b,...]

Reads only <attempt>/sweep/sweep_config.json ("axes") and <attempt>/sweep/sweep_table.csv, so it works for any
deck that follows the data conventions of A6. For every hidden axis h, every case (fixed values of the other
axes) and every ordered pair of observable columns (A, B), it estimates the Section 8.1 checks from the grid:

  uniq    A strictly monotonic in h over the case's grid points (one input value reproduces A)
  bspan   relative range of B over the grid (B must move, in % of |B|)
  lin×    nonlinearity: the straight-line fit of B against A (all grid points of the case) misses the
          piecewise-linear B(A) of the grid by this many B tolerances, at the best place inside the allowed
          zone (needs > 3)
  round   rounding robustness: |dB/dA| x half a unit of A's 4th significant figure, in B tolerances
          (needs < 1/3)
  (columns that are numerical noise in a case, below 1e-6 of the column's largest |value|, are skipped, and
  so are cases where B changes sign or comes within 5% of its largest |value| from zero)
  h*      the hidden value where lin× is largest, inside the allowed zone (>= 20% of the interval width from
          the midpoint, >= 5% from the ends, interval = the case's grid span) and not on a grid value

These are screening estimates from the grid only: the real checks are made later on the refined case (A7/A8).
Only pairs that pass uniq, lin× > 3 and round < 1/3 are listed, best first; a summary counts the others.
"""
import argparse
import csv
import itertools
import json
import math
import os
import sys


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def half_unit_4sf(a):
    if a == 0:
        return 0.0
    return 0.5 * 10 ** (math.floor(math.log10(abs(a))) - 3)


def interp(xs, ys, x):
    for i in range(len(xs) - 1):
        lo, hi = xs[i], xs[i + 1]
        if min(lo, hi) <= x <= max(lo, hi):
            t = 0.0 if hi == lo else (x - lo) / (hi - lo)
            return ys[i] + t * (ys[i + 1] - ys[i])
    return None


def screen_case(h_vals, A, B, tol_pct, scale_a=None, scale_b=None):
    """Return None if the pair fails uniqueness (or A / B is numerical noise), else a dict of the estimates.
    scale_a / scale_b: the column's largest |value| over the whole table; values below 1e-6 of it are noise."""
    n = len(h_vals)
    for col, scale in ((A, scale_a), (B, scale_b)):
        if scale and max(abs(v) for v in col) < 1e-6 * scale:
            return None
    if min(B) * max(B) <= 0 or min(abs(v) for v in B) < 0.05 * max(abs(v) for v in B):
        return None                       # B changes sign or nears zero: a relative tolerance is meaningless
    dA = [A[i + 1] - A[i] for i in range(n - 1)]
    if not (all(d > 0 for d in dA) or all(d < 0 for d in dA)):
        return None
    bmid = sum(abs(b) for b in B) / n
    if bmid == 0:
        return None
    # straight line B = p*A + q through all grid points of the case
    ma, mb = sum(A) / n, sum(B) / n
    saa = sum((a - ma) ** 2 for a in A)
    p = sum((a - ma) * (b - mb) for a, b in zip(A, B)) / saa if saa else 0.0
    q = mb - p * ma
    lo, hi = min(h_vals), max(h_vals)
    width = hi - lo
    mid = (lo + hi) / 2
    best = None
    steps = 400
    for k in range(1, steps):
        h = lo + width * k / steps
        if abs(h - mid) < 0.2 * width or h - lo < 0.05 * width or hi - h < 0.05 * width:
            continue
        if any(abs(h - g) < 1e-9 * max(1.0, abs(g)) for g in h_vals):
            continue
        a = interp(h_vals, A, h)
        b = interp(h_vals, B, h)
        if a is None or b is None or abs(b) < 0.05 * max(abs(v) for v in B):
            continue                      # B near zero: a relative tolerance there is meaningless
        tol_b = tol_pct / 100.0 * max(abs(b), 1e-300)
        miss = abs(p * a + q - b) / tol_b
        # local slope dB/dA from the grid interval holding h
        j = max(i for i in range(n - 1) if min(h_vals[i], h_vals[i + 1]) <= h)
        slope = (B[j + 1] - B[j]) / (A[j + 1] - A[j])
        rnd = abs(slope) * half_unit_4sf(a) / tol_b
        if best is None or miss > best['lin']:
            best = {'lin': miss, 'round': rnd, 'h': h, 'a': a, 'b': b}
    if best is None:
        return None
    best['bspan'] = 100.0 * (max(B) - min(B)) / bmid
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--attempt-dir', required=True)
    ap.add_argument('--hidden', default='', help='only this hidden axis (default: every axis with >= 3 values)')
    ap.add_argument('--columns', default='', help='only these observable columns, comma-separated')
    ap.add_argument('--tol-percent', type=float, default=float(os.environ.get('TOLERANCE_PERCENT', 1.0)))
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--min-points', type=int, default=4,
                    help='grid points a case needs on the hidden axis (fewer make the estimates unreliable)')
    a = ap.parse_args()

    sw = os.path.join(a.attempt_dir, 'sweep')
    try:
        axes = list((json.load(open(os.path.join(sw, 'sweep_config.json'))).get('axes') or {}).keys())
        rows = list(csv.DictReader(open(os.path.join(sw, 'sweep_table.csv'))))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit('cannot read sweep_config.json / sweep_table.csv: %s' % e)
    if not rows:
        sys.exit('sweep_table.csv has no rows')
    axes = [x for x in axes if x in rows[0]]
    obs = [c for c in rows[0] if c != 'run_id' and c not in axes and all(num(r[c]) is not None for r in rows)]
    if a.columns:
        want = [c.strip() for c in a.columns.split(',') if c.strip()]
        obs = [c for c in obs if c in want]
    scale = {c: max(abs(num(r[c])) for r in rows) for c in obs}
    hidden = [a.hidden] if a.hidden else [x for x in axes if len({num(r[x]) for r in rows}) >= a.min_points]
    print('axes %s | %d rows | %d numeric observable columns | tolerance %.3g%% | hidden candidates %s'
          % (axes, len(rows), len(obs), a.tol_percent, hidden))

    found, n_pairs, fails = [], 0, {'uniq': 0, 'lin': 0, 'round': 0}
    for h in hidden:
        others = [x for x in axes if x != h]
        cases = {}
        for r in rows:
            cases.setdefault(tuple(num(r[x]) for x in others), []).append(r)
        for key, rs in cases.items():
            rs = sorted(rs, key=lambda r: num(r[h]))
            hv = [num(r[h]) for r in rs]
            if len(set(hv)) < a.min_points or len(set(hv)) != len(hv):
                continue
            for ca, cb in itertools.permutations(obs, 2):
                n_pairs += 1
                A = [num(r[ca]) for r in rs]
                B = [num(r[cb]) for r in rs]
                res = screen_case(hv, A, B, a.tol_percent, scale[ca], scale[cb])
                if res is None:
                    fails['uniq'] += 1
                    continue
                if res['lin'] <= 3:
                    fails['lin'] += 1
                    continue
                if res['round'] >= 1 / 3:
                    fails['round'] += 1
                    continue
                found.append(dict(res, hidden=h, case=dict(zip(others, key)), A=ca, B=cb,
                                  interval=[min(hv), max(hv)]))
    found.sort(key=lambda d: -d['lin'])
    print('pairs screened %d | fail uniqueness %d | fail nonlinearity (lin<=3) %d | fail rounding %d | pass %d'
          % (n_pairs, fails['uniq'], fails['lin'], fails['round'], len(found)))
    if not found:
        print('no (A, B) pair passes on this grid: add sweep points or other observables')
        return
    print('%-8s %-28s %-14s %-14s %7s %7s %7s %12s' % ('hidden', 'case', 'A', 'B', 'lin×', 'round', 'bspan%', 'h*'))
    for d in found[:a.top]:
        case = ','.join('%s=%.4g' % kv for kv in d['case'].items()) or '-'
        print('%-8s %-28s %-14s %-14s %7.1f %7.3f %7.1f %12.5g' % (
            d['hidden'], case[:28], d['A'][:14], d['B'][:14], d['lin'], d['round'], d['bspan'], d['h']))
    if len(found) > a.top:
        print('... %d more (use --top N, --hidden or --columns to narrow)' % (len(found) - a.top))
    print('next: pick one pair, then run the real Section 8.1 checks on its refined case (A7/A8)')


if __name__ == '__main__':
    main()
