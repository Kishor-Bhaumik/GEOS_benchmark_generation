"""bubblewrap sandboxes for the controller and the tool agent (Section 3.3) + the agent self-test.

  python qa_sandbox.py selftest            # agent-sandbox self-test with the launch environment
  python qa_sandbox.py hidden              # list the GEOS_ROOT paths hidden from the agent

Agent view: system read-only; $HOME hidden except GEOS_ROOT, conda base and AGENT_RO_BINDS (read-only);
BENCH_DIR, GEOS_ROOT/GEOS_Agent and every git-untracked/ignored GEOS_ROOT path hidden, except
AGENT_VISIBLE_UNTRACKED (default: the build root holding bin/ and lib/ of GEOS_BIN); its workspace at
/work (the only writable place); private /tmp; no API key; network per AGENT_NET; CPUs via taskset.
Controller view: system read-only; BENCH_DIR writable except the harness scripts, pricing.json and the
attempts' controller/ and eval/ folders; ~/.config/nemonist hidden; private /tmp.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

import qa_common as C

AGENT_WORK = '/work'
AGENT_BIN = '/opt/qa/bin'
AGENT_STATE = '/opt/qa/state'
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))


def bwrap_bin():
    b = shutil.which('bwrap') or os.path.join(sys.prefix, 'bin', 'bwrap')
    if not os.path.exists(b):
        raise RuntimeError('bwrap not found (install bubblewrap in the geos env)')
    return b


def conda_base():
    p = sys.prefix
    while p != '/' and os.path.basename(os.path.dirname(p)) == 'envs':
        p = os.path.dirname(os.path.dirname(p))
    return p


def build_root(cfg):
    """The build folder that holds bin/ (and lib/) of GEOS_BIN."""
    b = os.path.realpath(cfg['GEOS_BIN'])
    d = os.path.dirname(b)
    return os.path.dirname(d) if os.path.basename(d) == 'bin' else d


def visible_untracked(cfg):
    v = [p for p in cfg.get('AGENT_VISIBLE_UNTRACKED', '').replace(',', ':').split(':') if p]
    if not v:
        v = [build_root(cfg)]
    return [os.path.realpath(p if os.path.isabs(p) else os.path.join(cfg['GEOS_ROOT'], p)) for p in v]


def _under(p, root):
    p, root = os.path.realpath(p), os.path.realpath(root)
    return p == root or p.startswith(root.rstrip('/') + '/')


def hidden_paths(cfg):
    root = os.path.realpath(cfg['GEOS_ROOT'])
    out = subprocess.run(['git', '-C', root, 'status', '--porcelain', '--ignored', '-z'],
                         capture_output=True, text=True, check=True).stdout
    paths = []
    for ent in out.split('\0'):
        if len(ent) > 3 and ent[:2] in ('??', '!!'):
            paths.append(os.path.join(root, ent[3:].rstrip('/')))
    paths += [os.path.join(root, 'GEOS_Agent'), os.path.realpath(cfg['BENCH_DIR'])]
    vis = visible_untracked(cfg)
    keep = []
    for p in dict.fromkeys(os.path.realpath(x) for x in paths):
        if any(_under(p, v) for v in vis):          # p is (inside) a visible path
            continue
        if not os.path.lexists(p):
            continue
        keep.append(p)
    # drop paths already covered by a hidden parent
    keep.sort(key=len)
    final = []
    for p in keep:
        if not any(_under(p, q) for q in final):
            final.append(p)
    return final


def _empty_file():
    p = os.path.join(tempfile.gettempdir(), 'qa_empty_file')
    if not os.path.exists(p):
        open(p, 'w').close()
    return p


def make_agent_state(cfg, state_dir):
    """Create bin/geosx wrapper (counts launches, refuses beyond the cap) and the counter."""
    b = os.path.join(state_dir, 'bin')
    s = os.path.join(state_dir, 'state')
    os.makedirs(b, exist_ok=True)
    os.makedirs(s, exist_ok=True)
    cap = int(cfg['AGENT_MAX_GEOS_RUNS'])
    real = os.path.realpath(cfg['GEOS_BIN'])
    w = os.path.join(b, 'geosx')
    with open(w, 'w') as f:
        f.write('''#!/bin/bash
# GEOS launch counter (soft cap of AGENT_MAX_GEOS_RUNS); counts rank 0 / serial launches only.
r="${OMPI_COMM_WORLD_RANK:-${PMI_RANK:-${PMIX_RANK:-0}}}"
case " $* " in *" --help "*|*" -h "*) exec %(real)s "$@";; esac
if [ "$r" = "0" ]; then
  exec 9>>%(st)s/geos_runs.lock
  flock 9
  n=$(cat %(st)s/geos_runs 2>/dev/null || echo 0)
  if [ "$n" -ge %(cap)d ]; then
    echo "GEOS run limit (%(cap)d) reached: no further GEOS runs are allowed in this session." >&2
    exit 97
  fi
  echo $((n+1)) > %(st)s/geos_runs
  flock -u 9
fi
exec %(real)s "$@"
''' % {'real': real, 'st': AGENT_STATE, 'cap': cap})
    os.chmod(w, 0o755)
    open(os.path.join(s, 'geos_runs'), 'w').write('0')
    return b, s


def geos_runs(state_dir):
    try:
        return int(open(os.path.join(state_dir, 'state', 'geos_runs')).read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _env_args(env):
    a = ['--clearenv']
    for k, v in env.items():
        a += ['--setenv', k, str(v)]
    return a


def _root_binds():
    """New root = empty tmpfs (so /work and /opt/qa can be created) + every host top-level entry read-only."""
    a = []
    for e in sorted(os.listdir('/')):
        if e in ('proc', 'dev', 'tmp', 'work', 'lost+found'):
            continue
        p = '/' + e
        if os.path.islink(p):
            a += ['--symlink', os.readlink(p), p]
        elif os.path.isdir(p):
            a += ['--ro-bind', p, p]
    return a


def agent_argv(cfg, workspace, state_dir, inner):
    """argv running `inner` (list) inside the agent sandbox."""
    home = os.path.expanduser('~')
    geos_root = os.path.realpath(cfg['GEOS_ROOT'])
    a = [bwrap_bin(), '--die-with-parent', '--unshare-pid', '--unshare-ipc'] + _root_binds() + \
        ['--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp', '--tmpfs', home]
    if cfg.get('AGENT_NET', 'on') == 'off':
        a.append('--unshare-net')
    ro = [geos_root, conda_base()] + [p for p in cfg.get('AGENT_RO_BINDS', '').replace(',', ':').split(':') if p]
    for p in dict.fromkeys(os.path.realpath(x) for x in ro):
        if os.path.exists(p):
            a += ['--ro-bind', p, p]
    bench = os.path.realpath(cfg['BENCH_DIR'])
    vis = visible_untracked(cfg)
    hidden = hidden_paths(cfg)
    if not any(_under(bench, h) for h in hidden):
        hidden.append(bench)
    empty = _empty_file()
    for h in hidden:
        if os.path.isdir(h) and not os.path.islink(h):
            a += ['--tmpfs', h]
        elif os.path.lexists(h):
            a += ['--ro-bind', empty, h]
    for v in vis:                                   # re-expose visible paths under a hidden parent
        if os.path.exists(v) and any(_under(v, h) for h in hidden):
            a += ['--ro-bind', v, v]
    a += ['--bind', workspace, AGENT_WORK]
    if os.path.isdir('/opt'):
        a += ['--tmpfs', '/opt']     # host /opt hidden behind a tmpfs that holds /opt/qa
    a += ['--ro-bind', os.path.join(state_dir, 'bin'), AGENT_BIN,
          '--bind', os.path.join(state_dir, 'state'), AGENT_STATE,
          '--chdir', AGENT_WORK]
    env = {'PATH': '%s:%s/bin:/usr/local/bin:/usr/bin:/bin' % (AGENT_BIN, sys.prefix),
           'HOME': AGENT_WORK, 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'OMP_NUM_THREADS': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'CONDA_PREFIX': sys.prefix,
           'OMPI_MCA_rmaps_base_oversubscribe': '1'}
    a += _env_args(env)
    cores = int(cfg.get('AGENT_CORES') or 0)
    ncpu = os.cpu_count() or 1
    pre = ['taskset', '-c', '0-%d' % (min(cores, ncpu) - 1)] if cores and shutil.which('taskset') else []
    return a + ['--'] + pre + inner


def controller_argv(cfg, ddir, inner):
    home = os.path.expanduser('~')
    bench = os.path.realpath(cfg['BENCH_DIR'])
    a = [bwrap_bin(), '--die-with-parent', '--unshare-pid', '--unshare-ipc',
         '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp']
    key_dir = os.path.join(home, '.config', 'nemonist')
    if os.path.isdir(key_dir):
        a += ['--tmpfs', key_dir]
    a += ['--bind', bench, bench]
    protect = [HARNESS_DIR, os.path.join(bench, '_global', 'pricing.json')]
    if os.path.isdir(ddir):
        for e in sorted(os.listdir(ddir)):
            if e.startswith('attempt_'):
                for sub in ('controller', 'eval'):
                    p = os.path.join(ddir, e, sub)
                    os.makedirs(p, exist_ok=True)
                    protect.append(p)
    for p in protect:
        if not os.path.exists(p):
            continue
        real = os.path.realpath(p)          # bwrap cannot mount onto a symlink: protect its target instead
        if _under(real, bench):
            a += ['--ro-bind', real, real]
        # a target outside BENCH_DIR is already read-only (the whole system is mounted read-only)
    gm = C.generator_memory_dir(bench)       # cwd = the generator's persistent memory folder (Nemonist scope = folder)
    os.makedirs(gm, exist_ok=True)
    a += ['--chdir', gm]
    env = {'PATH': '%s/bin:/usr/local/bin:/usr/bin:/bin' % sys.prefix, 'HOME': home,
           'TMPDIR': '/tmp', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'OMP_NUM_THREADS': '1',
           'PYTHONDONTWRITEBYTECODE': '1', 'CONDA_PREFIX': sys.prefix,
           'OMPI_MCA_rmaps_base_oversubscribe': '1'}
    for k in ('GEOS_ROOT', 'BENCH_DIR', 'GEOS_BIN', 'INPUT_XML', 'RUN_TAG', 'PROTOCOL_FILE',
              'PARALLEL_JOBS') + tuple(C.DEFAULTS):
        if cfg.get(k) not in (None, ''):
            env[k] = cfg[k]
    a += _env_args(env)
    return a + ['--'] + inner


def run(argv, timeout, stdin=None):
    """Run argv in its own process group; kill the group on timeout. Returns (rc, out, err, timed_out)."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(input=stdin.encode() if stdin is not None else None, timeout=timeout)
        return p.returncode, out.decode(errors='replace'), err.decode(errors='replace'), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return -9, out.decode(errors='replace'), err.decode(errors='replace'), True


def selftest(cfg, verbose=True):
    """Section 3.3 self-test of the agent sandbox. Returns (ok, details)."""
    tmp = tempfile.mkdtemp(prefix='qa_selftest_')
    ws = os.path.join(tmp, 'ws')
    os.makedirs(ws)
    make_agent_state(cfg, tmp)
    hidden = hidden_paths(cfg)
    bench = os.path.realpath(cfg['BENCH_DIR'])
    if bench not in hidden:
        hidden.append(bench)
    checks = []
    script = ['set +e']
    script.append('geosx --help >/dev/null 2>&1; echo "CHK geosx_runs $([ $? -eq 0 ] && echo ok || echo FAIL)"')
    for i, h in enumerate(hidden):
        script.append('p=%s; if [ -d "$p" ]; then [ -z "$(ls -A "$p" 2>/dev/null)" ] && r=ok || r=FAIL; '
                      'elif [ -e "$p" ]; then [ ! -s "$p" ] && r=ok || r=FAIL; else r=ok; fi; echo "CHK hidden_%d $r"'
                      % (json.dumps(h), i))
    script.append('[ -z "$NEMONIST_API_KEY" ] && echo "CHK no_api_key ok" || echo "CHK no_api_key FAIL"')
    script.append('[ -e %s ] && echo "CHK key_file_hidden FAIL" || echo "CHK key_file_hidden ok"'
                  % json.dumps(os.path.join(os.path.expanduser('~'), '.config', 'nemonist', 'env')))
    script.append('touch %s/.qa_write_test 2>/dev/null && echo "CHK geos_root_readonly FAIL" || echo "CHK geos_root_readonly ok"'
                  % json.dumps(os.path.realpath(cfg['GEOS_ROOT'])))
    script.append('touch /work/.w && echo "CHK work_writable ok" || echo "CHK work_writable FAIL"')
    script.append('[ -r %s ] && echo "CHK input_decks_visible ok" || echo "CHK input_decks_visible FAIL"'
                  % json.dumps(os.path.join(os.path.realpath(cfg['GEOS_ROOT']), cfg['INPUT_XML'])))
    rc, out, err, to = run(agent_argv(cfg, ws, tmp, ['bash', '-c', '\n'.join(script)]), timeout=120)
    res = {}
    for line in out.splitlines():
        if line.startswith('CHK '):
            _, name, r = line.split(' ', 2)
            res[name] = r.strip()
    names = ['geosx_runs', 'no_api_key', 'key_file_hidden', 'geos_root_readonly', 'work_writable',
             'input_decks_visible'] + ['hidden_%d' % i for i in range(len(hidden))]
    failed = [n for n in names if res.get(n) != 'ok']
    if os.path.exists(os.path.join(os.path.realpath(cfg['GEOS_ROOT']), '.qa_write_test')):
        failed.append('geos_root_readonly(host)')
    shutil.rmtree(tmp, ignore_errors=True)
    det = {'ok': not failed, 'failed': [(n if not n.startswith('hidden_') else 'hidden:' + hidden[int(n[7:])])
                                        for n in failed],
           'n_hidden': len(hidden), 'stderr_tail': err[-500:] if failed else ''}
    if verbose:
        print('sandbox self-test: %s (%d hidden paths)%s' % ('PASS' if det['ok'] else 'FAIL', len(hidden),
              '' if det['ok'] else '; failed: ' + ', '.join(det['failed'])))
    return det['ok'], det


def controller_selftest(cfg, ddir, verbose=True):
    """The controller sandbox starts, the deck folder is writable, pricing.json and the harness are not."""
    bench = os.path.realpath(cfg['BENCH_DIR'])
    pr = os.path.join(bench, '_global', 'pricing.json')
    script = '; '.join([
        'echo "CHK starts ok"',
        'touch %s/.qa_w && rm %s/.qa_w && echo "CHK deck_writable ok" || echo "CHK deck_writable FAIL"'
        % (json.dumps(ddir), json.dumps(ddir)),
        '(echo >> %s) 2>/dev/null && echo "CHK pricing_readonly FAIL" || echo "CHK pricing_readonly ok"' % json.dumps(pr),
        'touch %s/.qa_w 2>/dev/null && echo "CHK harness_readonly FAIL" || echo "CHK harness_readonly ok"'
        % json.dumps(HARNESS_DIR),
        '[ -z "$NEMONIST_API_KEY" ] && echo "CHK no_api_key ok" || echo "CHK no_api_key FAIL"',
        'python3 -c "import numpy" && echo "CHK python ok" || echo "CHK python FAIL"'])
    rc, out, err, _ = run(controller_argv(cfg, ddir, ['bash', '-c', script]), timeout=120)
    res = dict(l.split(' ', 2)[1:] for l in out.splitlines() if l.startswith('CHK '))
    names = ['starts', 'deck_writable', 'pricing_readonly', 'harness_readonly', 'no_api_key', 'python']
    failed = [n for n in names if res.get(n, '').strip() != 'ok']
    for p in (os.path.join(HARNESS_DIR, '.qa_w'),):
        if os.path.exists(p):
            os.remove(p)
            failed.append('harness_readonly(host)')
    if verbose:
        print('controller sandbox self-test: %s%s' % ('PASS' if not failed else 'FAIL',
              '' if not failed else '; failed: %s; %s' % (', '.join(failed), err.strip()[-300:])))
    return not failed, failed


if __name__ == '__main__':
    cfg, missing = C.config_from_env()
    need = [m for m in missing if m in ('BENCH_DIR', 'GEOS_ROOT', 'GEOS_BIN', 'INPUT_XML')]
    if need:
        sys.exit('missing: ' + ', '.join(need))
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'selftest'
    if cmd == 'hidden':
        for p in hidden_paths(cfg):
            print(p)
    else:
        ok, _ = selftest(cfg)
        sys.exit(0 if ok else 1)
