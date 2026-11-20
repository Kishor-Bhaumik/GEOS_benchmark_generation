"""Part B of GEOS_SWEEP_BENCH_AGENT_v1.md: evaluate one frozen MCQ question.

  python qa_eval.py --deck-dir <BENCH_DIR>/decks/<slug>/<RUN_TAG> --attempt <n>

B1: NO_TOOL_MODEL answers the N_SHUFFLES shuffles, one fresh call each, one after the other (no tools).
    no-tool failures < NO_TOOL_FAIL_MIN -> TOO_EASY.
B2: TOOL_MODEL answers the same shuffles in the agent sandbox, one fresh session each, one after the other,
    stopping at AGENT_STOP_FAILS failures; correct >= AGENT_PASS_MIN -> KEPT, else AGENT_FAILED.
Writes eval/*.json, attempt_result.json and the deck_memory.md entry. Resumable: finished shuffles are
never rerun. Exit 0 = done, 75 = usage limit (state saved), 3 = sandbox failed, 1 = error.
"""
import argparse
import os
import re
import shutil
import sys

import qa_agent_loop as AL
import qa_common as C
import qa_llm as L
import qa_sandbox as S

LETTER_RE = re.compile(r'FINAL ANSWER\s*:?\s*\**\s*\(?([ABCD])\)?(?![A-Za-z])', re.I)
VALUE_RE = re.compile(r'COMPUTED_VALUE\s*:?\s*\**\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)')


def parse_letter(text):
    m = LETTER_RE.findall(text or '')
    return m[-1].upper() if m else None


def parse_value(text):
    m = VALUE_RE.findall(text or '')
    try:
        return float(m[-1]) if m else None
    except ValueError:
        return None


def options_text(mcq, k):
    sh = mcq['shuffles'][k - 1]['letters']
    u = mcq.get('units') or ''
    lines = []
    for x in 'ABCD':
        role = sh[x]
        if role == 'none':
            lines.append('D. None of them')
        else:
            lines.append('%s. %s%s' % (x, mcq['formatted'][role], (' ' + u) if u else ''))
    return '\n'.join(lines)


def outcome(mcq, k, letter):
    if not letter:
        return 'no_answer'
    role = mcq['shuffles'][k - 1]['letters'][letter]
    return {'true': 'correct', 'd1': 'd1', 'd2': 'd2', 'none': 'none'}[role]


def usage_stop(cfg, stage, model, e):
    C.interruption(cfg['BENCH_DIR'], stage, model, e.http_status, e.detail)
    if e.http_status == 'budget':
        print('DAILY BUDGET reached at %s (%s): state saved; rerun the launcher tomorrow' % (stage, e))
    else:
        print('USAGE LIMIT at %s (HTTP %s): state saved; rerun the launcher later' % (stage, e.http_status))
    sys.exit(C.USAGE_LIMIT_EXIT)


# ------------------------------------------------------------------ B1

def no_tool_shuffle(cfg, adir, mcq, key, question, k):
    out_p = os.path.join(adir, 'eval', 'no_tool_s%d.json' % k)
    if os.path.exists(out_p):
        return C.load_json(out_p)
    usage_p = os.path.join(adir, 'eval', 'no_tool_s%d_usage.jsonl' % k)
    prompt = C.fill(C.prompt_block(cfg['PROTOCOL_FILE'], 'NO_TOOL'), UNITS=key.get('units', ''),
                    QUESTION=question, OPTIONS=options_text(mcq, k))
    text, letter, calls, cut = '', None, [], False
    for attempt in (1, 2):
        # a try cut off by max_tokens (finish_reason "length") is repeated with a larger cap, so that the
        # no-tool model can finish its reasoning; a shuffle still cut off is `cut_off`, not a no-tool failure
        mt = int(cfg['NO_TOOL_RETRY_MAX_TOKENS']) if cut else None
        try:
            msg, rec = L.chat(cfg['NO_TOOL_MODEL'], [{'role': 'user', 'content': prompt}], tools=None,
                              timeout=int(cfg['NO_TOOL_TIMEOUT_S']), usage_path=usage_p,
                              tag={'role': 'no_tool', 'shuffle': k, 'try': attempt}, bench_dir=cfg['BENCH_DIR'],
                              max_tokens=mt)
        except (L.UsageLimit, L.BudgetStop) as e:
            usage_stop(cfg, 'B1 shuffle %d' % k, cfg['NO_TOOL_MODEL'], e)
        except L.CallFailed as e:
            calls.append({'try': attempt, 'error': str(e)[:300]})
            continue
        text = AL._text(msg)
        letter = parse_letter(text)
        cut = rec.get('finish_reason') == 'length'
        calls.append({'try': attempt, 'letter': letter, 'finish_reason': rec.get('finish_reason'),
                      'max_tokens': mt})
        if letter:
            break
    recs = C.read_jsonl(usage_p)
    oc = outcome(mcq, k, letter)
    if not letter and cut:
        oc = 'cut_off'                   # never finished its answer: not counted as a no-tool failure (B1)
    res = {'shuffle': k, 'question_sha256': mcq['question_sha256'], 'prompt_sha256': C.sha256_text(prompt),
           'answer_text': text, 'computed_value': parse_value(text), 'letter': letter,
           'outcome': oc, 'tries': calls, 'usage': C.sum_usage(recs),
           'cut_off': any(r.get('finish_reason') == 'length' for r in recs),
           'model': cfg['NO_TOOL_MODEL']}
    C.save_json(out_p, res)
    print('  no-tool shuffle %d: %-9s (letter %s)  $%.4f' % (k, res['outcome'], letter, res['usage']['actual_cost_usd']))
    return res


# ------------------------------------------------------------------ B2

def agent_shuffle(cfg, adir, mcq, key, question, k, slug):
    out_p = os.path.join(adir, 'eval', 'agent_s%d.json' % k)
    if os.path.exists(out_p):
        return C.load_json(out_p)
    usage_p = os.path.join(adir, 'eval', 'agent_s%d_usage.jsonl' % k)
    n = int(os.path.basename(adir).split('_')[-1])
    root = os.path.join(cfg['BENCH_DIR'], '_global', 'agent_workspaces')
    name = '%s__%s__a%d__s%d' % (slug, cfg['RUN_TAG'], n, k)
    prompt = C.fill(C.prompt_block(cfg['PROTOCOL_FILE'], 'TOOL_AGENT'), GEOS_ROOT=os.path.realpath(cfg['GEOS_ROOT']),
                    MAX_GEOS_RUNS=cfg['AGENT_MAX_GEOS_RUNS'], MAX_TURNS=cfg['AGENT_MAX_TURNS'],
                    WALL_HOURS=('%.3g' % (float(cfg['AGENT_MAX_WALL_S']) / 3600)), CORES=cfg['AGENT_CORES'],
                    UNITS=key.get('units', ''), QUESTION=question, OPTIONS=options_text(mcq, k))
    tries, result, geos = [], None, 0
    for attempt in (1, 2):
        ok, det = S.selftest(cfg, verbose=False)
        if not ok:
            return {'sandbox_failed': True, 'detail': det}
        ws, st = os.path.join(root, name), os.path.join(root, name + '.state')
        for p in (ws, st):
            shutil.rmtree(p, ignore_errors=True)
        os.makedirs(ws)
        S.make_agent_state(cfg, st)
        try:
            result = AL.run_session(cfg, 'agent', cfg['TOOL_MODEL'], None, prompt, usage_p, None,
                                    workspace=ws, state_dir=st, tag={'shuffle': k, 'try': attempt})
            tries.append({'try': attempt, 'ended': result['ended']})
            break
        except (L.UsageLimit, L.BudgetStop) as e:
            usage_stop(cfg, 'B2 shuffle %d' % k, cfg['TOOL_MODEL'], e)
        except L.CallFailed as e:
            geos = S.geos_runs(st)
            tries.append({'try': attempt, 'error': str(e)[:300], 'geos_runs': geos})
            result = None
            if geos > 0:
                break
        finally:
            for p in (ws, st):
                shutil.rmtree(p, ignore_errors=True)
    text = (result or {}).get('final_text', '')
    letter = parse_letter(text)
    recs = C.read_jsonl(usage_p)
    geos_runs = (result or {}).get('geos_runs', geos) or 0
    geos_inv = (result or {}).get('geos_invocations', 0) or 0
    used_geos = geos_runs > 0 or geos_inv > 0
    oc = outcome(mcq, k, letter)
    if oc == 'correct' and not used_geos and str(cfg.get('AGENT_REQUIRE_GEOS', 'on')).lower() not in ('0', 'off', 'no'):
        oc = 'correct_no_geos'           # right letter without running GEOS: not counted (Section 10, B2)
    res = {'shuffle': k, 'question_sha256': mcq['question_sha256'], 'prompt_sha256': C.sha256_text(prompt),
           'final_message': text, 'computed_value': parse_value(text), 'letter': letter,
           'outcome': oc, 'tries': tries,
           'turns': (result or {}).get('turns'), 'ended': (result or {}).get('ended', 'technical_failure'),
           'geos_runs': geos_runs, 'geos_invocations': geos_inv, 'used_geos': used_geos,
           'geos_run_line': (re.findall(r'GEOS_RUN\s*:\s*(.+)', text) or [None])[-1],
           'cut_off': any(r.get('finish_reason') == 'length' for r in recs),
           'forbidden_path_attempts': (result or {}).get('forbidden_path_attempts', 0),
           'nemonist_attempts': (result or {}).get('nemonist_attempts', 0),
           'wall_s': (result or {}).get('wall_s'), 'usage': C.sum_usage(recs), 'model': cfg['TOOL_MODEL']}
    C.save_json(out_p, res)
    print('  agent shuffle %d: %-9s (letter %s) turns %s, GEOS runs %s (commands %s), %.0f s, $%.4f' % (
        k, res['outcome'], letter, res['turns'], res['geos_runs'], geos_inv, res['wall_s'] or 0,
        res['usage']['actual_cost_usd']))
    return res


# ------------------------------------------------------------------ records

def generator_usage(adir):
    recs = C.read_jsonl(os.path.join(adir, 'controller', 'usage.jsonl'))
    u = C.sum_usage(recs)
    u['sessions'] = len({r.get('session') for r in recs if r.get('session') is not None}) or (1 if recs else 0)
    return u


def write_memory_entry(ddir, n, lines):
    p = os.path.join(ddir, 'deck_memory.md')
    txt = open(p).read() if os.path.exists(p) else '# Deck memory\n'
    head = '## Attempt %d' % n
    entry = head + '\n' + '\n'.join(lines[:24]) + '\n'
    parts = re.split(r'(?m)^(?=## Attempt \d+\s*$)', txt)
    parts = [x for x in parts if not x.startswith(head + '\n') and x.strip() != head]
    parts.append(entry)
    nums = lambda x: int(re.match(r'## Attempt (\d+)', x).group(1)) if x.startswith('## Attempt') else -1
    pre = [x for x in parts if nums(x) < 0]
    att = sorted([x for x in parts if nums(x) >= 0], key=nums)
    with open(p, 'w') as f:
        f.write(''.join(x if x.endswith('\n') else x + '\n' for x in pre + att))


def memory_lines(adir, status, key=None, mcq=None, nt=None, ag=None, reason=None):
    L_ = ['- status: %s%s' % (status, ' — %s' % reason if reason else '')]
    if key:
        tf = key.get('type_fields') or {}
        L_.append('- type: %s; observable: %s; units: %s' % (key.get('question_type'), key.get('observable'), key.get('units')))
        L_.append('- case: %s' % key.get('case'))
        if tf.get('hidden_input'):
            L_.append('- hidden input: %s in %s; inferred %s; observed A (%s) = %s' % (
                tf['hidden_input'].get('name'), tf['hidden_input'].get('interval'), tf.get('inferred_input_value'),
                tf.get('observed_column'), tf.get('observed_value')))
        L_.append('- answer: %s = %s %s' % (key.get('answer_column') or tf.get('searched_axis'), key.get('true_value'), key.get('units', '')))
    if mcq:
        pv = mcq.get('distractor_provenance', {})
        L_.append('- options: true %s; d1 %s (%s); d2 %s (%s); band %s-%s%%' % (
            mcq['formatted']['true'], mcq['formatted']['d1'], pv.get('d1', {}).get('source'),
            mcq['formatted']['d2'], pv.get('d2', {}).get('source'), mcq['band']['d_min_pct'], mcq['band']['d_max_pct']))
        L_.append('- sweep: %s' % ', '.join(mcq.get('sweep_sources', [])))
    reuse = C.load_json(os.path.join(adir, 'sweep', 'reuse.json'))
    if reuse:
        L_.append('- sweep reused from attempt %s: %s' % (reuse.get('from_attempt'), reuse.get('reason', '')[:120]))
    if nt:
        L_.append('- no-tool picks: %s; computed values: %s' % ([x['outcome'] for x in nt], [x.get('computed_value') for x in nt]))
    if ag:
        L_.append('- agent picks: %s; computed values: %s' % ([x['outcome'] for x in ag], [x.get('computed_value') for x in ag]))
        L_.append('- agent turns: %s; GEOS runs: %s; ended: %s' % ([x.get('turns') for x in ag], [x.get('geos_runs') for x in ag],
                                                                   [x.get('ended') for x in ag]))
    return L_


def attempt_result(cfg, ddir, n, status, reason=None, key=None, mcq=None, nt=None, ag=None):
    adir = C.attempt_dir(ddir, n)
    gen = generator_usage(adir)
    nt, ag = nt or [], ag or []
    nt_cost = [x['usage']['actual_cost_usd'] for x in nt]
    ag_cost = [x['usage']['actual_cost_usd'] for x in ag]
    tok = {}
    for role, us in (('generator', [gen]), ('no_tool', [x['usage'] for x in nt]), ('agent', [x['usage'] for x in ag])):
        tok[role] = {k: sum(u.get(k, 0) or 0 for u in us) for k in ('prompt', 'completion', 'reasoning', 'cached', 'cache_write')}
    res = {
        'attempt': n, 'status': status, 'reason': reason,
        'question_type': (key or {}).get('question_type'), 'question_sha256': (mcq or {}).get('question_sha256'),
        'true_value': (key or {}).get('true_value'), 'units': (key or {}).get('units'),
        'options': (mcq or {}).get('formatted'), 'band': (mcq or {}).get('band'),
        'no_tool': {'picks': [x['outcome'] for x in nt], 'failures': sum(1 for x in nt if x['outcome'] not in ('correct', 'cut_off')),
                    'per_shuffle_cost_usd': nt_cost, 'total_cost_usd': round(sum(nt_cost), 8)},
        'agent': {'picks': [x['outcome'] for x in ag] + ['not_run'] * ((cfg['N_SHUFFLES'] - len(ag)) if ag else 0),
                  'correct': sum(1 for x in ag if x['outcome'] == 'correct'),
                  'per_shuffle_cost_usd': ag_cost, 'total_cost_usd': round(sum(ag_cost), 8),
                  'per_shuffle_wall_s': [x.get('wall_s') for x in ag], 'per_shuffle_geos_runs': [x.get('geos_runs') for x in ag],
                  'per_shuffle_turns': [x.get('turns') for x in ag]},
        'generator': {'cost_usd': gen['actual_cost_usd'], 'sessions': gen['sessions'], 'turns': gen['calls'],
                      'wall_s': gen['wall_s']},
        'tokens': tok,
        'cost_mismatch_calls': gen['cost_mismatch_calls'] + sum(x['usage']['cost_mismatch_calls'] for x in nt + ag),
        'cost_estimated_calls': gen['cost_estimated_calls'] + sum(x['usage']['cost_estimated_calls'] for x in nt + ag),
        'total_cost_usd': round(gen['actual_cost_usd'] + sum(nt_cost) + sum(ag_cost), 8),
        'models': {'GEN_MODEL': cfg['GEN_MODEL'], 'NO_TOOL_MODEL': cfg['NO_TOOL_MODEL'], 'TOOL_MODEL': cfg['TOOL_MODEL']},
        'finished_utc': C.now_utc(),
    }
    C.save_json(os.path.join(adir, 'attempt_result.json'), res)
    write_memory_entry(ddir, n, memory_lines(adir, status, key, mcq, nt, ag, reason))
    C.set_status(adir, 'B3', status)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--deck-dir', required=True)
    ap.add_argument('--attempt', type=int, required=True)
    a = ap.parse_args()
    ddir = os.path.abspath(a.deck_dir)
    cfg = C.load_json(os.path.join(ddir, 'run_config.json'))
    if not cfg:
        sys.exit('run_config.json missing (start through qa_run_deck.py)')
    sys.exit(evaluate(cfg, ddir, a.attempt))


def evaluate(cfg, ddir, n):
    adir = C.attempt_dir(ddir, n)
    if os.path.exists(os.path.join(adir, 'attempt_result.json')):
        print('attempt %d already evaluated' % n)
        return 0
    st = C.load_json(os.path.join(adir, 'attempt_status.json'), {}) or {}
    if st.get('status') not in ('READY_FOR_EVAL', 'IN_PROGRESS') or (st.get('status') == 'IN_PROGRESS' and st.get('stage') not in ('B1', 'B2')):
        print('attempt %d is not ready for evaluation (%s %s)' % (n, st.get('stage'), st.get('status')))
        return 1
    qdir = os.path.join(adir, 'question')
    key = C.load_json(os.path.join(qdir, 'answer_key.json'))
    ver = C.load_json(os.path.join(qdir, 'verify.json'), {})
    mcq = C.load_json(os.path.join(qdir, 'mcq.json'))
    qp = os.path.join(qdir, 'question.txt')
    sha = C.sha256_file(qp)
    if not (key and mcq and ver.get('frozen') and ver.get('question_sha256') == sha == mcq.get('question_sha256')):
        print('question/mcq hashes do not match the frozen question: not evaluated')
        return 1
    if not mcq.get('frozen'):
        mcq['frozen'] = True
        C.save_json(os.path.join(qdir, 'mcq.json'), mcq)
    question = open(qp).read().strip()
    slug = os.path.basename(os.path.dirname(ddir))
    os.makedirs(os.path.join(adir, 'eval'), exist_ok=True)
    N = int(cfg['N_SHUFFLES'])

    print('attempt %d — B1 no-tool (%s), %d shuffles' % (n, cfg['NO_TOOL_MODEL'], N))
    C.set_status(adir, 'B1', 'IN_PROGRESS')
    try:
        nt = [no_tool_shuffle(cfg, adir, mcq, key, question, k) for k in range(1, N + 1)]
    except L.ModelMismatch as e:
        print('STOP: %s' % e)
        return 1
    fails = sum(1 for x in nt if x['outcome'] not in ('correct', 'cut_off'))
    if fails < int(cfg['NO_TOOL_FAIL_MIN']):
        r = attempt_result(cfg, ddir, n, 'TOO_EASY', key=key, mcq=mcq, nt=nt)
        print('TOO_EASY: no-tool wrong %d/%d (< %d); generator $%.4f, no-tool $%.4f, total $%.4f' % (
            fails, N, cfg['NO_TOOL_FAIL_MIN'], r['generator']['cost_usd'], r['no_tool']['total_cost_usd'], r['total_cost_usd']))
        return 0

    print('attempt %d — B2 agent (%s); no-tool wrong %d/%d' % (n, cfg['TOOL_MODEL'], fails, N))
    C.set_status(adir, 'B2', 'IN_PROGRESS')
    ag = []
    for k in range(1, N + 1):
        if sum(1 for x in ag if x['outcome'] != 'correct') >= int(cfg['AGENT_STOP_FAILS']):
            break
        try:
            res = agent_shuffle(cfg, adir, mcq, key, question, k, slug)
        except L.ModelMismatch as e:
            print('STOP: %s' % e)
            return 1
        if res.get('sandbox_failed'):
            attempt_result(cfg, ddir, n, 'SANDBOX_FAILED', reason='self-test failed: %s' % res['detail'].get('failed'),
                           key=key, mcq=mcq, nt=nt, ag=ag)
            print('SANDBOX_FAILED before agent shuffle %d: %s' % (k, res['detail'].get('failed')))
            return 3
        ag.append(res)
    correct = sum(1 for x in ag if x['outcome'] == 'correct')
    status = 'KEPT' if correct >= int(cfg['AGENT_PASS_MIN']) else 'AGENT_FAILED'
    r = attempt_result(cfg, ddir, n, status, key=key, mcq=mcq, nt=nt, ag=ag)
    print('%s: agent correct %d/%d run; generator $%.4f, no-tool $%.4f, agent $%.4f, total $%.4f' % (
        status, correct, len(ag), r['generator']['cost_usd'], r['no_tool']['total_cost_usd'],
        r['agent']['total_cost_usd'], r['total_cost_usd']))
    return 0


if __name__ == '__main__':
    main()
