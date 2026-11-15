"""Launcher of GEOS_SWEEP_BENCH_AGENT_v1.md for one deck (Sections 1, 3.4, 5, 12).

  python qa_run_deck.py            # configuration from the launch environment (Appendix)
  python qa_run_deck.py --check    # startup checks only (no model call)

Per attempt n = 1..MAX_ATTEMPTS: controller session(s) execute Part A -> qa_eval (Part B) -> memory (Part C).
Stops at KEPT. Writes run_config.json, results.md, deck_result.json; cleans up (C3).
Resumable: rerun at any time. Exit 75 = usage limit (rerun later), 1 = error/stop.
"""
import argparse
import os
import platform
import re
import shutil
import subprocess
import sys

import qa_agent_loop as AL
import qa_common as C
import qa_eval as E
import qa_llm as L
import qa_sandbox as S

COMPARE_KEYS = ('GEN_MODEL', 'NO_TOOL_MODEL', 'TOOL_MODEL', 'QUESTION_TYPE', 'PROTOCOL_VERSION', 'INPUT_XML')


def startup_checks(cfg):
    errs = []
    try:
        rc = subprocess.run([cfg['GEOS_BIN'], '--help'], capture_output=True, timeout=120).returncode
        if rc != 0:
            errs.append('GEOS_BIN does not run (exit %d)' % rc)
    except (OSError, subprocess.TimeoutExpired) as e:
        errs.append('GEOS_BIN does not run: %s' % e)
    for m in dict.fromkeys((cfg['GEN_MODEL'], cfg['NO_TOOL_MODEL'], cfg['TOOL_MODEL'])):
        if not C.pricing_row(cfg['BENCH_DIR'], m):
            errs.append('pricing.json has no row named exactly %s' % m)
    for b in ('NO_TOOL', 'TOOL_AGENT', 'CONTROLLER_SYSTEM'):
        try:
            C.prompt_block(cfg['PROTOCOL_FILE'], b)
        except (KeyError, OSError) as e:
            errs.append(str(e))
    if not os.path.exists(os.path.join(cfg['GEOS_ROOT'], cfg['INPUT_XML'])):
        errs.append('INPUT_XML not found under GEOS_ROOT')
    return errs


def write_run_config(cfg, ddir):
    p = os.path.join(ddir, 'run_config.json')
    old = C.load_json(p)
    if old:
        diff = [k for k in COMPARE_KEYS if str(old.get(k)) != str(cfg.get(k))]
        if diff:
            print('STOP: run_config.json of this RUN_TAG differs in %s; choose a new RUN_TAG' % diff)
            sys.exit(1)
        return old
    git = subprocess.run(['git', '-C', cfg['GEOS_ROOT'], 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    bw = subprocess.run([S.bwrap_bin(), '--version'], capture_output=True, text=True).stdout.strip()
    rc = dict(cfg, PROTOCOL_ID=C.PROTOCOL_ID, PROTOCOL_VERSION=C.PROTOCOL_VERSION,
              deck_slug=C.deck_slug(cfg['GEOS_ROOT'], cfg['INPUT_XML']), host=platform.node(),
              geos_commit=git, bwrap_version=bw, python=sys.version.split()[0],
              agent_hidden_paths=S.hidden_paths(cfg), agent_visible_untracked=S.visible_untracked(cfg),
              created_utc=C.now_utc())
    C.save_json(p, rc)
    return rc


def controller_kickoff(cfg, ddir, n):
    return ('Read %s completely and execute Part A for attempt %d of deck %s (deck folder %s, question type %s). '
            'Stop after A10.' % (cfg['PROTOCOL_FILE'], n, cfg['INPUT_XML'], ddir, cfg['QUESTION_TYPE']))


def ready_problem(adir):
    """None if the attempt is really ready for Part B, else the reason."""
    q = os.path.join(adir, 'question')
    qp = os.path.join(q, 'question.txt')
    ver = C.load_json(os.path.join(q, 'verify.json'), {}) or {}
    mcq = C.load_json(os.path.join(q, 'mcq.json'), {}) or {}
    if not os.path.exists(qp):
        return 'question.txt missing'
    sha = C.sha256_file(qp)
    if not (ver.get('frozen') and ver.get('all_passed') and ver.get('question_sha256') == sha):
        return 'question not frozen by qa_verify.py --freeze (failed: %s)' % ver.get('failed_checks')
    if mcq.get('question_sha256') != sha:
        return 'mcq.json missing or built for another text'
    return None


def run_controller(cfg, ddir, n):
    """Run controller sessions for attempt n until READY_FOR_EVAL / GENERATION_FAILED / a deck status.
    Returns 'ready' | 'generation_failed' | 'deck_done'."""
    adir = C.attempt_dir(ddir, n)
    os.makedirs(os.path.join(adir, 'controller'), exist_ok=True)
    os.makedirs(os.path.join(adir, 'eval'), exist_ok=True)
    system = C.fill(C.prompt_block(cfg['PROTOCOL_FILE'], 'CONTROLLER_SYSTEM'), BENCH_DIR=cfg['BENCH_DIR'],
                    GEN_MEMORY_DIR=C.generator_memory_dir(cfg['BENCH_DIR']))
    while True:
        st = C.load_json(os.path.join(adir, 'attempt_status.json'), {}) or {}
        if os.path.exists(os.path.join(ddir, 'deck_result.json')):
            return 'deck_done'
        if st.get('stage') in ('B1', 'B2'):
            return 'ready'
        if st.get('status') == 'READY_FOR_EVAL':
            why = ready_problem(adir)
            if not why:
                return 'ready'
            st = C.set_status(adir, 'A8', 'IN_PROGRESS', reason='READY_FOR_EVAL rejected by the launcher: ' + why)
            print('  READY_FOR_EVAL rejected: %s' % why)
        if st.get('status') == 'GENERATION_FAILED':
            return 'generation_failed'
        done = int(st.get('controller_sessions', 0))
        if done >= int(cfg['CONTROLLER_MAX_SESSIONS']):
            C.set_status(adir, st.get('stage', 'A?'), 'GENERATION_FAILED', reason='controller session cap (%d sessions)' % done)
            return 'generation_failed'
        s = done + 1
        print('attempt %d — controller session %d (%s)' % (n, s, cfg['GEN_MODEL']))
        try:
            def done():
                st_ = C.load_json(os.path.join(adir, 'attempt_status.json'), {}) or {}
                return st_.get('status') in ('READY_FOR_EVAL', 'GENERATION_FAILED') or \
                    os.path.exists(os.path.join(ddir, 'deck_result.json'))
            res = AL.run_session(cfg, 'controller', cfg['GEN_MODEL'], system, controller_kickoff(cfg, ddir, n),
                                 os.path.join(adir, 'controller', 'usage.jsonl'),
                                 os.path.join(adir, 'controller', 'transcript_%d.jsonl' % s),
                                 deck_dir=ddir, tag={'session': s}, done_check=done)
        except (L.UsageLimit, L.BudgetStop) as e:
            C.interruption(cfg['BENCH_DIR'], 'A attempt %d session %d' % (n, s), cfg['GEN_MODEL'], e.http_status, e.detail)
            print(('DAILY BUDGET reached (%s): state saved; rerun the launcher tomorrow' % e) if e.http_status == 'budget'
                  else 'USAGE LIMIT (HTTP %s): state saved; rerun the launcher later' % e.http_status)
            sys.exit(C.USAGE_LIMIT_EXIT)
        except L.ModelMismatch as e:
            print('STOP: %s' % e)
            sys.exit(1)
        except L.CallFailed as e:
            res = {'ended': 'technical_failure: %s' % str(e)[:200], 'turns': None}
        st = C.load_json(os.path.join(adir, 'attempt_status.json'), {}) or {}
        st['controller_sessions'] = s
        st.setdefault('stage', 'A?')
        st.setdefault('status', 'IN_PROGRESS')
        st.setdefault('sessions', []).append({'session': s, 'ended': res.get('ended'), 'turns': res.get('turns'),
                                              'nudges': res.get('nudges'),
                                              'nemonist_attempts': res.get('nemonist_attempts'),
                                              'last_reply': (res.get('final_text') or '')[-1500:]})
        C.save_json(os.path.join(adir, 'attempt_status.json'), st)
        print('  session %d ended: %s (%s turns); status %s %s' % (s, res.get('ended'), res.get('turns'),
                                                                   st.get('stage'), st.get('status')))


def deck_costs(ddir, n_max):
    per, tot, tok = [], {'generator_usd': 0.0, 'no_tool_usd': 0.0, 'agent_usd': 0.0}, {}
    for n in range(1, n_max + 1):
        adir = C.attempt_dir(ddir, n)
        if not os.path.isdir(adir):
            continue
        r = C.load_json(os.path.join(adir, 'attempt_result.json'))
        if r:
            g, nt, ag = r['generator']['cost_usd'] or 0, r['no_tool']['total_cost_usd'] or 0, r['agent']['total_cost_usd'] or 0
            t = r.get('tokens', {})
        else:
            gu = E.generator_usage(adir)
            g, nt, ag, t = gu['actual_cost_usd'], 0, 0, {'generator': {k: gu[k] for k in ('prompt', 'completion', 'reasoning', 'cached', 'cache_write')}}
        per.append({'attempt': n, 'generator_usd': g, 'no_tool_usd': nt, 'agent_usd': ag, 'total_usd': round(g + nt + ag, 8)})
        tot['generator_usd'] += g
        tot['no_tool_usd'] += nt
        tot['agent_usd'] += ag
        for role, d in t.items():
            acc = tok.setdefault(role, {})
            for k, v in d.items():
                acc[k] = acc.get(k, 0) + (v or 0)
    tot = {k: round(v, 8) for k, v in tot.items()}
    tot['total_usd'] = round(sum(tot.values()), 8)
    tot['per_attempt'] = per
    return tot, tok


def finish(cfg, ddir, status=None, reason=None):
    p = os.path.join(ddir, 'deck_result.json')
    old = C.load_json(p) or {}
    n_max = int(cfg['MAX_ATTEMPTS'])
    atts = []
    for n in range(1, n_max + 1):
        r = C.load_json(os.path.join(C.attempt_dir(ddir, n), 'attempt_result.json'))
        if r:
            atts.append(r)
    st = old.get('status') or status
    if not st:
        st = 'KEPT' if any(a['status'] == 'KEPT' for a in atts) else 'NOT_KEPT'
    costs, tok = deck_costs(ddir, n_max)
    sims = {'baseline': 1 if os.path.isdir(os.path.join(ddir, 'exploration', 'baseline')) else 0, 'per_attempt': []}
    for n in range(1, n_max + 1):
        sd = os.path.join(C.attempt_dir(ddir, n), 'sweep')
        if os.path.isdir(sd):
            cnt = lambda sub: len([x for x in os.listdir(os.path.join(sd, sub))]) if os.path.isdir(os.path.join(sd, sub)) else 0
            rows = 0
            tab = os.path.join(sd, 'sweep_table.csv')
            if os.path.exists(tab):
                rows = max(0, sum(1 for _ in open(tab)) - 1)
            sims['per_attempt'].append({'attempt': n, 'sweep_rows': rows, 'pilot': cnt('pilot'),
                                        'refinement': cnt('refine'), 'extra': cnt('extra')})
    kept = next((a['attempt'] for a in atts if a['status'] == 'KEPT'), None)
    dr = dict(old)
    dr.update({
        'status': st, 'reason': old.get('reason') or reason,
        'PROTOCOL_ID': C.PROTOCOL_ID, 'PROTOCOL_VERSION': C.PROTOCOL_VERSION,
        'RUN_TAG': cfg['RUN_TAG'], 'deck_slug': os.path.basename(os.path.dirname(ddir)),
        'question_type': cfg['QUESTION_TYPE'], 'attempts_run': len(atts), 'kept_attempt': kept,
        'attempts': [{'attempt': a['attempt'], 'status': a['status'], 'no_tool_failures': a['no_tool']['failures'],
                      'agent_correct': a['agent']['correct']} for a in atts],
        'simulations': sims, 'costs': costs, 'tokens': tok,
        'models': {'GEN_MODEL': cfg['GEN_MODEL'], 'NO_TOOL_MODEL': cfg['NO_TOOL_MODEL'], 'TOOL_MODEL': cfg['TOOL_MODEL']},
        'finished_utc': C.now_utc()})
    C.save_json(p, dr)
    write_results_md(ddir, dr, atts)
    cleanup(cfg, ddir)
    print('DECK %s — attempts %d, kept %s; cost generator $%.4f, no-tool $%.4f, agent $%.4f, total $%.4f' % (
        st, len(atts), kept, costs['generator_usd'], costs['no_tool_usd'], costs['agent_usd'], costs['total_usd']))
    return 0


def write_results_md(ddir, dr, atts):
    L_ = ['# %s — %s' % (dr['deck_slug'], dr['RUN_TAG']), '',
          'Status: **%s**  (question type %s; models %s)' % (dr['status'], dr['question_type'],
                                                            ', '.join('%s=%s' % kv for kv in dr['models'].items())), '',
          '| attempt | status | true / d1 / d2 | no-tool picks | agent picks | generator $ | no-tool $ (per shuffle) | agent $ (per shuffle) | total $ |',
          '|---|---|---|---|---|---|---|---|---|']
    for a in atts:
        o = a.get('options') or {}
        L_.append('| %d | %s | %s | %s | %s | %.4f | %.4f (%s) | %.4f (%s) | %.4f |' % (
            a['attempt'], a['status'], ' / '.join(str(o.get(k, '')) for k in ('true', 'd1', 'd2')) if o else '-',
            ' '.join(a['no_tool']['picks']) or '-', ' '.join(a['agent']['picks']) or '-',
            a['generator']['cost_usd'] or 0, a['no_tool']['total_cost_usd'],
            ', '.join('%.4f' % x for x in a['no_tool']['per_shuffle_cost_usd']),
            a['agent']['total_cost_usd'], ', '.join('%.4f' % x for x in a['agent']['per_shuffle_cost_usd']),
            a['total_cost_usd']))
    c = dr['costs']
    L_ += ['', 'Deck total: generator $%.4f, no-tool $%.4f, agent $%.4f, **total $%.4f**' % (
        c['generator_usd'], c['no_tool_usd'], c['agent_usd'], c['total_usd']), '',
        'Tokens (prompt / completion / reasoning / cached): ' + '; '.join(
            '%s %s/%s/%s/%s' % (r, t.get('prompt', 0), t.get('completion', 0), t.get('reasoning', 0), t.get('cached', 0))
            for r, t in dr.get('tokens', {}).items())]
    open(os.path.join(ddir, 'results.md'), 'w').write('\n'.join(L_) + '\n')


KEEP_TOP = {'run_config.json', 'deck_memory.md', 'deck_result.json', 'results.md'}
KEEP_EXPL = {'exploration/deck_summary.md', 'exploration/output_manifest.json', 'exploration/baseline/fixes.md'}
KEEP_ATT = {'design.md', 'attempt_status.json', 'attempt_result.json', 'controller/usage.jsonl'}
KEEP_SWEEP = {'sweep_config.json', 'template.xml', 'extract_table.py', 'sweep_table.csv', 'failed_runs.csv',
              'sweep_readme.md', 'reuse.json'}


def keep(rel):
    parts = rel.split('/')
    if rel in KEEP_TOP or rel in KEEP_EXPL or parts[0] == 'source':
        return True
    if parts[0].startswith('attempt_') and len(parts) > 1:
        sub = '/'.join(parts[1:])
        if sub in KEEP_ATT or parts[1] in ('question', 'eval') or \
                (parts[1] == 'controller' and parts[-1].startswith('transcript_')):
            return True
        if parts[1] == 'sweep':
            if len(parts) == 3 and parts[2] in KEEP_SWEEP:
                return True
            if len(parts) == 5 and parts[2] in ('refine', 'extra') and parts[4] == 'observables.json':
                return True
    return False


def cleanup(cfg, ddir):
    removed = 0
    for root, dirs, files in os.walk(ddir, topdown=False):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, ddir)
            if not keep(rel) or f.endswith('.hdf5') and not rel.startswith('source/'):
                os.remove(p)
                removed += 1
        for d in dirs:
            p = os.path.join(root, d)
            if d == '__pycache__':
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
    ws = os.path.join(cfg['BENCH_DIR'], '_global', 'agent_workspaces')
    pre = '%s__%s__' % (os.path.basename(os.path.dirname(ddir)), cfg['RUN_TAG'])
    if os.path.isdir(ws):
        for e in os.listdir(ws):
            if e.startswith(pre):
                shutil.rmtree(os.path.join(ws, e), ignore_errors=True)
    size = subprocess.run(['du', '-sh', ddir], capture_output=True, text=True).stdout.split()[0]
    print('cleanup: %d files removed; deck folder %s' % (removed, size))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    cfg, missing = C.config_from_env()
    if missing:
        print('STOP: missing or invalid: %s' % ', '.join(missing))
        return 1
    cfg['BENCH_DIR'] = os.path.realpath(cfg['BENCH_DIR'])
    cfg['PROTOCOL_ID'], cfg['PROTOCOL_VERSION'] = C.PROTOCOL_ID, C.PROTOCOL_VERSION
    errs = startup_checks(cfg)
    if errs:
        print('STOP: ' + '; '.join(errs))
        return 1
    ddir = C.deck_dir(cfg)
    os.makedirs(ddir, exist_ok=True)
    ok, det = S.selftest(cfg)
    if not ok:
        C.save_json(os.path.join(ddir, 'deck_result.json'),
                    {'status': 'SANDBOX_FAILED', 'reason': 'startup self-test failed: %s' % det['failed']})
        return 1
    okc, failc = S.controller_selftest(cfg, ddir)
    if not okc:
        print('STOP: the controller sandbox does not work (%s); no model call made' % failc)
        return 1
    print('deck folder: %s' % ddir)
    print('spent today: $%.4f of the $%.2f daily budget' % (C.spent_today(cfg['BENCH_DIR']), float(cfg['DAILY_BUDGET_USD'])))
    if a.check:
        print('startup checks passed (no model call made)')
        return 0
    cfg = dict(write_run_config(cfg, ddir), **{k: v for k, v in cfg.items()})
    if os.path.exists(os.path.join(ddir, 'deck_result.json')) and \
            (C.load_json(os.path.join(ddir, 'deck_result.json')) or {}).get('finished_utc'):
        print('deck already finished: %s' % C.load_json(os.path.join(ddir, 'deck_result.json')).get('status'))
        return 0
    for n in range(1, int(cfg['MAX_ATTEMPTS']) + 1):
        adir = C.attempt_dir(ddir, n)
        r = C.load_json(os.path.join(adir, 'attempt_result.json'))
        if r:
            print('attempt %d: %s (finished)' % (n, r['status']))
            if r['status'] == 'KEPT':
                return finish(cfg, ddir)
            if r['status'] == 'SANDBOX_FAILED':
                return finish(cfg, ddir, 'SANDBOX_FAILED', r.get('reason'))
            continue
        out = run_controller(cfg, ddir, n)
        if out == 'deck_done':
            return finish(cfg, ddir)
        if out == 'generation_failed':
            st = C.load_json(os.path.join(adir, 'attempt_status.json'), {}) or {}
            E.attempt_result(cfg, ddir, n, 'GENERATION_FAILED', reason=st.get('reason'),
                             key=C.load_json(os.path.join(adir, 'question', 'answer_key.json')))
            print('attempt %d: GENERATION_FAILED (%s)' % (n, st.get('reason')))
            continue
        rc = E.evaluate(cfg, ddir, n)
        if rc == 3:
            return finish(cfg, ddir, 'SANDBOX_FAILED', 'agent sandbox self-test failed during attempt %d' % n)
        if rc != 0:
            return 1
        r = C.load_json(os.path.join(adir, 'attempt_result.json'))
        if r and r['status'] == 'KEPT':
            return finish(cfg, ddir)
    return finish(cfg, ddir)


if __name__ == '__main__':
    sys.exit(main())
