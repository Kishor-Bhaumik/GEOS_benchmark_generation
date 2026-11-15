"""Tool-calling loop for the controller and the tool agent (Section 3.2).

  run_session(cfg, role, model, system, user, usage_path, transcript_path, workspace=None,
              state_dir=None, deck_dir=None, tag=None) -> result dict

Tools: bash, read_file, write_file — all executed INSIDE the role's bwrap sandbox (qa_sandbox), so the
file tools see exactly what bash sees. Each bash command runs in a fresh shell; background processes end
with the command. Tool outputs longer than TOOL_OUTPUT_MAX_CHARS are cut to head + tail.
Caps: turns and wall time per role; at 90% one warning message is added; at the cap one last reply is
requested with tool_choice "none". Usage of every call is appended to usage_path (outside the sandbox).
Raises qa_llm.UsageLimit / qa_llm.ModelMismatch to the caller.
"""
import json
import os
import re
import time

import qa_common as C
import qa_llm as L
import qa_sandbox as S

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'bash',
        'description': ('Run a bash command and return its exit code, stdout and stderr. Each call runs in a '
                        'fresh shell in the working directory; background processes end when the command ends, '
                        'so wait for long jobs inside the same command. Long output is cut.'),
        'parameters': {'type': 'object', 'properties': {
            'command': {'type': 'string'},
            'timeout_s': {'type': 'integer', 'description': 'optional time limit in seconds'}},
            'required': ['command']}}},
    {'type': 'function', 'function': {
        'name': 'read_file',
        'description': 'Read a text file; returns at most `limit` lines starting at line `offset` (1-based).',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}},
            'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'write_file',
        'description': 'Write `content` to the file at `path` (creating parent folders), replacing it.',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'}, 'content': {'type': 'string'}},
            'required': ['path', 'content']}}},
]

READ_PY = r'''
import sys
p, off, lim = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(p, errors="replace") as f:
    lines = f.readlines()
sel = lines[off - 1: off - 1 + lim]
for i, l in enumerate(sel, off):
    sys.stdout.write("%6d\t%s" % (i, l if l.endswith("\n") else l + "\n"))
if off - 1 + lim < len(lines):
    sys.stdout.write("[... %d more lines ...]\n" % (len(lines) - (off - 1 + lim)))
'''
WRITE_PY = r'''
import os, sys
p = sys.argv[1]
d = os.path.dirname(os.path.abspath(p))
os.makedirs(d, exist_ok=True)
data = sys.stdin.read()
with open(p, "w") as f:
    f.write(data)
print("wrote %d bytes to %s" % (len(data.encode()), p))
'''


def cut(s, n):
    if len(s) <= n:
        return s
    h = n * 2 // 3
    t = n - h
    return s[:h] + '\n[... %d chars cut ...]\n' % (len(s) - n) + s[-t:]


class Session:
    def __init__(self, cfg, role, workspace=None, state_dir=None, deck_dir=None):
        self.cfg, self.role = cfg, role
        self.workspace, self.state_dir, self.deck_dir = workspace, state_dir, deck_dir
        self.max_out = int(cfg.get('TOOL_OUTPUT_MAX_CHARS', 12000))
        self.hidden = S.hidden_paths(cfg) if role == 'agent' else []
        self.forbidden_attempts = 0
        self.geos_cmds = 0
        self.nemonist_attempts = 0

    def argv(self, inner):
        if self.role == 'agent':
            return S.agent_argv(self.cfg, self.workspace, self.state_dir, inner)
        return S.controller_argv(self.cfg, self.deck_dir, inner)

    def note_command(self, text):
        if self.role != 'agent':
            return
        if any(h in text for h in self.hidden) or 'GEOS_Agent' in text or 'geos_sweep_bench' in text:
            self.forbidden_attempts += 1
        if re.search(r'\bgeosx?\b', text):
            self.geos_cmds += 1

    def bash(self, command, timeout_s, remaining):
        self.note_command(command)
        if self.role == 'agent' and self.state_dir:
            cap = int(self.cfg['AGENT_MAX_GEOS_RUNS'])
            if S.geos_runs(self.state_dir) >= cap and re.search(r'\bgeosx?\b', command) \
                    and '--help' not in command:
                return 'REFUSED: the GEOS run limit (%d) of this session is reached.' % cap
        t = max(10, min(int(timeout_s or remaining), int(remaining)))
        rc, out, err, to = S.run(self.argv(['bash', '-c', command]), timeout=t)
        res = 'exit code: %s%s\n--- stdout ---\n%s\n--- stderr ---\n%s' % (
            rc, ' (killed: time limit %d s)' % t if to else '', out, err)
        return cut(res, self.max_out)

    def read_file(self, path, offset=None, limit=None):
        self.note_command(path)
        off = max(1, int(offset or 1))
        lim = max(1, min(int(limit or 400), 5000))
        rc, out, err, _ = S.run(self.argv(['python3', '-c', READ_PY, path, str(off), str(lim)]), timeout=120)
        return cut(out if rc == 0 else 'ERROR: ' + err.strip()[-800:], self.max_out)

    def write_file(self, path, content):
        self.note_command(path)
        rc, out, err, _ = S.run(self.argv(['python3', '-c', WRITE_PY, path]), timeout=120, stdin=content)
        return out.strip() if rc == 0 else 'ERROR: ' + err.strip()[-800:]

    def call_tool(self, name, args, remaining):
        # Nemonist memory (scope = folder) is allowed: the generator keeps one persistent memory folder, and each
        # agent attempt gets a fresh empty /work, so memory never crosses gates or attempts. Uses are only counted.
        if '.nemonist' in json.dumps(args):
            self.nemonist_attempts += 1
        try:
            if name == 'bash':
                return self.bash(args.get('command', ''), args.get('timeout_s'), remaining)
            if name == 'read_file':
                return self.read_file(args['path'], args.get('offset'), args.get('limit'))
            if name == 'write_file':
                return self.write_file(args['path'], args.get('content', ''))
            return 'ERROR: unknown tool %s' % name
        except (KeyError, TypeError, ValueError) as e:
            return 'ERROR: bad arguments for %s: %s' % (name, e)


def _text(msg):
    c = msg.get('content')
    if isinstance(c, list):
        return ''.join(p.get('text', '') for p in c if isinstance(p, dict))
    return c or ''


NUDGE = ('Continue executing Part A of the protocol now, using your tools. Do not stop to report progress or '
         'ask for confirmation: nobody will answer. Reply without a tool call only when attempt_status.json says '
         'READY_FOR_EVAL (A10 done) or GENERATION_FAILED, or when the protocol tells you to stop.')


def run_session(cfg, role, model, system, user, usage_path, transcript_path=None, workspace=None,
                state_dir=None, deck_dir=None, tag=None, done_check=None, max_nudges=3):
    """done_check: optional callable; when the model replies without a tool call but done_check() is False,
    the loop answers with NUDGE (at most max_nudges times) instead of ending the session."""
    if role == 'agent':
        max_turns, max_wall = int(cfg['AGENT_MAX_TURNS']), float(cfg['AGENT_MAX_WALL_S'])
    else:
        max_turns, max_wall = int(cfg['CONTROLLER_MAX_TURNS']), float(cfg['CONTROLLER_MAX_WALL_S'])
    sess = Session(cfg, role, workspace, state_dir, deck_dir)
    messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': user}]
    t0 = time.time()
    turns, warned, ended = 0, False, 'final_reply'
    provider = None
    final_text = ''
    nudges = 0
    tag = dict(tag or {}, role=role)

    def log(obj):
        if transcript_path:
            C.append_jsonl(transcript_path, obj)
    log({'ts': C.now_utc(), 'event': 'start', 'role': role, 'model': model})
    while True:
        elapsed = time.time() - t0
        at_cap = turns >= max_turns - 1 or elapsed >= max_wall
        if not warned and (turns >= 0.9 * max_turns or elapsed >= 0.9 * max_wall):
            warned = True
            messages.append({'role': 'user', 'content': (
                'Limit nearly reached: give your final answer now.' if role == 'agent'
                else 'Limit nearly reached: finish now (record your status in the files) and reply without a tool call.')})
        msg, rec = L.chat(model, messages, tools=TOOLS, tool_choice='none' if at_cap else None,
                          timeout=max(60, int(min(1800, max_wall - elapsed + 60))),
                          usage_path=usage_path, tag=dict(tag, turn=turns + 1), bench_dir=cfg['BENCH_DIR'],
                          provider=provider)
        if provider is None and 'unpin' not in (rec.get('repairs') or []):
            provider = rec.get('provider')     # keep the whole session on the provider that served turn 1
        turns += 1
        calls = msg.get('tool_calls') or []
        text = _text(msg)
        out = {'role': 'assistant', 'content': text or None}
        if calls and not at_cap:
            out['tool_calls'] = calls
        # Earlier thinking is NOT sent back: through this gateway the signed thinking blocks fail verification
        # (hidden text is inserted), and removing them mid-session changes the prompt prefix so prompt caching
        # stops working. Each call still thinks afresh (reasoning budget per call).
        messages.append(out)
        log({'ts': C.now_utc(), 'event': 'assistant', 'turn': turns, 'text': text[-4000:],
             'tool_calls': [c.get('function', {}) for c in calls]})
        if not calls and not at_cap and done_check is not None and not done_check():
            if nudges < max_nudges:
                nudges += 1
                messages.append({'role': 'user', 'content': NUDGE})
                log({'ts': C.now_utc(), 'event': 'nudge', 'n': nudges})
                continue
            final_text, ended = text, 'stopped_unfinished'
            break
        if not calls or at_cap:
            final_text = text
            if at_cap:
                ended = 'cap_turns' if turns >= max_turns - 1 else 'cap_wall'
            break
        for c in calls:
            fn = c.get('function') or {}
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except json.JSONDecodeError:
                args = {}
            remaining = max(10, max_wall - (time.time() - t0))
            res = sess.call_tool(fn.get('name'), args if isinstance(args, dict) else {}, remaining)
            messages.append({'role': 'tool', 'tool_call_id': c.get('id'), 'content': res})
            log({'ts': C.now_utc(), 'event': 'tool', 'name': fn.get('name'), 'result': res[-2000:]})
    wall = time.time() - t0
    log({'ts': C.now_utc(), 'event': 'end', 'ended': ended, 'turns': turns, 'wall_s': round(wall, 1)})
    return {'final_text': final_text, 'turns': turns, 'wall_s': round(wall, 1), 'ended': ended, 'nudges': nudges,
            'forbidden_path_attempts': sess.forbidden_attempts, 'nemonist_attempts': sess.nemonist_attempts,
            'geos_runs': S.geos_runs(state_dir) if state_dir else None}
