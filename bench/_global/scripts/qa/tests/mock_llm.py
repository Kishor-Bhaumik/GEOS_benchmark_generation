"""Offline mock of the gateway for the AGENT harness test. Plays controller, no-tool and agent."""
import json, os, re

def _usage(p, c):
    return {'prompt_tokens': p, 'completion_tokens': c, 'total_tokens': p + c, 'cost': 0, 'is_byok': True,
            'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
            'cost_details': {'upstream_inference_cost': round(p * 4e-6 + c * 20e-6, 8)},
            'completion_tokens_details': {'reasoning_tokens': c // 4}}

def _resp(model, text=None, calls=None, p=1000, c=200):
    m = {'role': 'assistant', 'content': text}
    if calls:
        m['tool_calls'] = [{'id': 'call_%d' % i, 'type': 'function',
                            'function': {'name': n, 'arguments': json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {'id': 'mock', 'model': model, 'provider': 'mock', 'choices': [{'message': m}], 'usage': _usage(p, c)}

def _letter_for(text, want_true):
    true = os.environ['MOCK_TRUE']
    opts = re.findall(r'^([ABC])\. (\S+)', text, re.M)
    for L, v in opts:
        if (v == true) == want_true:
            return L
    return 'D'

def respond(model, messages, tools, tool_choice=None):
    n_asst = sum(1 for m in messages if m['role'] == 'assistant')
    if not tools and os.environ.get('MOCK_NT') == '429':
        import urllib.error, io
        raise urllib.error.HTTPError('http://x', 429, 'Too Many Requests', {}, io.BytesIO(b'rate limit'))
    if not tools:                                   # no-tool solver
        L = _letter_for(messages[-1]['content'], os.environ.get('MOCK_NT', 'wrong') == 'right')
        return _resp(model, 'reasoning...\nCOMPUTED_VALUE: 1.0 um\nFINAL ANSWER: %s' % L, p=800, c=1500)
    sysmsg = messages[0]['content'] if messages[0]['role'] == 'system' else ''
    if 'controller of a GEOS benchmark' in sysmsg and os.environ.get('MOCK_CTRL') == 'lazy':
        return _resp(model, 'I stopped without finishing.')
    if 'controller of a GEOS benchmark' in sysmsg:  # controller
        kick = messages[1]['content']
        att = int(re.search(r'Part A for attempt (\d+)', kick).group(1))
        deck = re.search(r'\(deck folder (\S+),', kick).group(1)
        ad = '%s/attempt_%d' % (deck, att)
        Q = os.environ['MOCK_QA']
        if n_asst == 0:
            cmd = ('set -e; mkdir -p %(d)s/source; echo \'{"keywords":["cementRock"]}\' > %(d)s/source/deck_keywords.json; '
                   'cp -r %(fx)s/sweep %(fx)s/question %(ad)s/; echo design > %(ad)s/design.md; '
                   'python %(Q)s/qa_verify.py --attempt-dir %(ad)s --freeze | tail -1; '
                   'python %(Q)s/qa_build_mcq.py --attempt-dir %(ad)s --d-min-pct 3 --d-max-pct 15 --reason mock | head -3; '
                   '(touch $BENCH_DIR/_global/pricing.json 2>/dev/null && echo PRICING_WRITABLE) || echo pricing_protected; '
                   '(touch %(ad)s/controller/x 2>/dev/null && echo CONTROLLER_DIR_WRITABLE) || echo controller_dir_protected; '
                   '(touch $GEOS_ROOT/x 2>/dev/null && echo GEOS_WRITABLE) || echo geos_root_protected; '
                   'ls ~/.config/nemonist 2>/dev/null | wc -l'
                   ) % {'d': deck, 'fx': os.environ['MOCK_FIXTURE'], 'ad': ad, 'Q': Q}
            return _resp(model, None, [('bash', {'command': cmd})], p=5000, c=300)
        if n_asst == 1:
            os.environ['MOCK_CONTROLLER_TOOL_OUT'] = messages[-1]['content']
            return _resp(model, None, [('write_file', {'path': ad + '/attempt_status.json',
                         'content': json.dumps({'stage': 'A10', 'status': 'READY_FOR_EVAL'})})])
        return _resp(model, 'Part A done: question frozen, MCQ built. TOOL OUTPUT WAS:\n' +
                     [m for m in messages if m['role'] == 'tool'][0]['content'][-1500:])
    # agent
    if n_asst == 0:
        cmd = ('geosx --help | head -1; echo hidden_bench=$(ls -A %(bench)s | wc -l); '
               'echo hidden_agentdir=$(ls -A %(root)s/GEOS_Agent 2>/dev/null | wc -l); '
               'echo hidden_questions=$(ls -A %(root)s/questions 2>/dev/null | wc -l); '
               'echo test_bench=$(ls -A %(mb)s 2>/dev/null | wc -l); echo key=[$NEMONIST_API_KEY]; pwd; '
               'ls %(root)s/inputFiles | head -2; nproc') % {
            'root': os.environ.get('GEOS_ROOT', os.path.expanduser('~/codes/GEOS')),
            'bench': os.environ.get('REAL_BENCH_DIR', os.path.expanduser('~/codes/GEOS/geos_sweep_bench')),
            'mb': os.environ['MOCK_BENCH']}
        return _resp(model, None, [('bash', {'command': cmd})], p=3000, c=200)
    if n_asst == 1:
        return _resp(model, None, [('write_file', {'path': '/work/notes.txt', 'content': 'hello\n'}),
                                   ('read_file', {'path': '/work/notes.txt'})])
    tool_out = [m['content'] for m in messages if m['role'] == 'tool']
    user = [m['content'] for m in messages if m['role'] == 'user'][0]
    L = _letter_for(user, os.environ.get('MOCK_AG', 'right') == 'right')
    return _resp(model, 'TOOL OUTPUTS:\n%s\nCOMPUTED_VALUE: 12.3 um\nFINAL ANSWER: %s' % ('\n'.join(tool_out)[-1200:], L))
