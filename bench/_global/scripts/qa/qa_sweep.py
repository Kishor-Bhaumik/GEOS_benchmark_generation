"""Read an attempt's simulated points (Section 6, data conventions).

sweep_config.json : {"axes": {"<axis>": [values, ...]}, ...}   (legacy {"grid": {...}} / {"sets": {..: {"grid"}}} accepted)
sweep_table.csv   : run_id, <axes...>, <observables...>        one row per successful run
refine/ref_NN/observables.json : {"run_id": "refine/ref_NN", "params": {<axis>: v}, "observables": {<col>: v}}
Extra runs (A9) live in sweep/extra/ext_NN/observables.json with the same layout.

Points of an attempt = its own table + refine + extra, plus those of the attempt named in sweep/reuse.json.
Run ids are prefixed "a<n>:" so they are unique across attempts.
"""
import csv
import glob
import json
import os

import qa_common as C


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-300)
    return a == b


def axes_of(cfgj):
    names = list((cfgj.get('axes') or {}))
    names += list((cfgj.get('grid') or {}))
    for st in (cfgj.get('sets') or {}).values():
        names += list((st.get('grid') or {}))
    return list(dict.fromkeys(names))


def _attempt_points(sdir, n):
    rows, axes = [], []
    cfgj = C.load_json(os.path.join(sdir, 'sweep_config.json'), {}) or {}
    axes = axes_of(cfgj)
    tab = os.path.join(sdir, 'sweep_table.csv')
    if os.path.exists(tab):
        for r in csv.DictReader(open(tab)):
            d = {k: num(v) for k, v in r.items()}
            d['run_id'] = 'a%d:%s' % (n, r.get('run_id'))
            d['_kind'] = 'sweep'
            rows.append(d)
    for kind in ('refine', 'extra'):
        for p in sorted(glob.glob(os.path.join(sdir, kind, '*', 'observables.json'))):
            o = C.load_json(p, {}) or {}
            d = {k: num(v) for k, v in (o.get('params') or {}).items()}
            d.update({k: num(v) for k, v in (o.get('observables') or {}).items()})
            rid = o.get('run_id') or '%s/%s' % (kind, os.path.basename(os.path.dirname(p)))
            d['run_id'] = 'a%d:%s' % (n, rid)
            d['_kind'] = kind
            rows.append(d)
    return rows, axes


def load_points(adir):
    """(rows, axes, sources) for attempt folder adir."""
    n = int(os.path.basename(adir.rstrip('/')).split('_')[-1])
    sdir = os.path.join(adir, 'sweep')
    rows, axes = _attempt_points(sdir, n)
    sources = ['a%d' % n]
    reuse = C.load_json(os.path.join(sdir, 'reuse.json'))
    if reuse and reuse.get('from_attempt'):
        m = int(reuse['from_attempt'])
        r2, ax2 = _attempt_points(os.path.join(os.path.dirname(adir), 'attempt_%d' % m, 'sweep'), m)
        rows += r2
        axes = list(dict.fromkeys(axes + ax2))
        sources.append('a%d' % m)
    if not axes and rows:
        cols = [k for k in rows[0] if not k.startswith('_') and k != 'run_id']
        axes = cols   # unknown split: callers must name axes in the answer key
    return rows, axes, sources


def find_run(rows, run_id, own=None):
    """Row of run_id. A bare id ("run_0004", "refine/ref_03") means the attempt's own run (own = "a<n>");
    "a<m>:<id>" names a run of a reused sweep."""
    if run_id is None:
        return None
    rid = run_id if ':' in run_id or not own else '%s:%s' % (own, run_id)
    for r in rows:
        if r['run_id'] == rid:
            return r
    return None
