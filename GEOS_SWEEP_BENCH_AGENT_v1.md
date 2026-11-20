# GEOS Sweep Benchmark — One Hard MCQ Question, No-Tool vs Tool-Agent (AGENT v1)

## 0. Purpose

This file is the execution contract for one deck. It is executed partly by a controller
model (Part A) and partly by scripts (Parts B and C). The controller executes Part A; it
does not summarise or explain this file.

For one GEOS deck, the experiment tries to produce ONE kept question:

| Step | Who | What |
|---|---|---|
| A | controller (`GEN_MODEL`, with tools) | explore the deck, run a sweep, write ONE question of type `QUESTION_TYPE`, refine its true answer, verify, freeze, build the 4-option MCQ |
| B1 | script → `NO_TOOL_MODEL` (no tools) | answers the MCQ `N_SHUFFLES` times, one fresh call per shuffle, one after the other |
| B2 | script → `TOOL_MODEL` (tool agent, sandboxed GEOS access) | only if the no-tool model was wrong at least `NO_TOOL_FAIL_MIN` times: answers the SAME shuffles, one fresh session per shuffle, one after the other; stops early once keeping is impossible |
| C | script | KEPT if the agent was right at least `AGENT_PASS_MIN` times; otherwise the attempt is dropped, a memory entry is appended, and a new attempt starts (at most `MAX_ATTEMPTS` per deck) |

A kept question is therefore one that a no-tool model gets wrong and a tool agent with
GEOS gets right: hard without simulation, solvable with it.

The MCQ options are:

| Option | Source |
|---|---|
| true answer | the simulation true value (refined) |
| distractor 1, distractor 2 | the same quantity in neighbouring simulated cases of the sweep, inside a distance band the controller chooses (Section 9.3); a seeded random value in that band only if the sweep has no suitable value |
| D | always "None of them"; never correct (a trap only) |

Every rule is written for ANY GEOS deck and any physics. Flow or CO2 details are examples.

---

## 1. Configuration

All counts and limits are variables; refer to them by name. Values marked `$` come from
the launch environment (Appendix); the others are defaults that may be overridden there.

```text
PROTOCOL_ID                 = AGENT
PROTOCOL_VERSION            = 1
RUN_TAG                     = $RUN_TAG          # change for every new experiment on the same deck
BENCH_DIR                   = $BENCH_DIR        # work folder; all outputs go here
GEOS_ROOT                   = $GEOS_ROOT        # the GEOS repository (read-only)
INPUT_XML                   = $INPUT_XML        # deck path relative to GEOS_ROOT
GEOS_BIN                    = $GEOS_BIN         # the geosx binary
PROTOCOL_FILE               = $PROTOCOL_FILE    # absolute path of THIS file
PARALLEL_JOBS               = $PARALLEL_JOBS    # sweep parallelism, <= nproc

NEMONIST_BASE_URL           = $NEMONIST_BASE_URL   # default https://nemonist.com/api/v1
NEMONIST_API_KEY            = (environment only)   # never printed, never written to any file
MODEL                       = $MODEL            # e.g. anthropic/claude-opus-5.5
GEN_MODEL                   = $GEN_MODEL        # default = MODEL  (controller / generator)
NO_TOOL_MODEL               = $NO_TOOL_MODEL    # default = MODEL  (no-tool solver)
TOOL_MODEL                  = $TOOL_MODEL       # default = MODEL  (tool agent)
REASONING_MAX_TOKENS        = 16000             # thinking budget per call, sent as reasoning={"max_tokens": N}; 0 = off
MAX_ANSWER_TOKENS           = 16000             # visible answer budget; each call's max_tokens = REASONING_MAX_TOKENS + this
REASONING_EFFORT            = $REASONING_EFFORT # optional; sent as reasoning_effort ONLY when REASONING_MAX_TOKENS = 0

QUESTION_TYPE               = $QUESTION_TYPE    # default infer_then_predict; one of the types in Section 8
MAX_ATTEMPTS                = 3                 # questions tried per deck before giving up
TOLERANCE_PERCENT           = 1.0               # internal accuracy of the true answer (never shown in the question)

N_SHUFFLES                  = 4
NO_TOOL_FAIL_MIN            = 3                 # no-tool wrong at least this often -> run the agent
AGENT_PASS_MIN              = 3                 # agent right at least this often -> KEPT
AGENT_STOP_FAILS            = N_SHUFFLES - AGENT_PASS_MIN + 1   # = 2 with the defaults: stop the agent here

MAX_SIMULATIONS_PER_ATTEMPT = 200               # pilots + sweep + refinement launches of one attempt
REFINEMENT_RESERVE_RUNS     = 30                # kept free from pilots + sweep for refinement
MAX_REFINEMENT_RUNS         = 12                # refinement launches for the one question
MAX_FAILED_RUN_PERCENT      = 5
DISTRACTOR_MAX_PCT          = 25                # upper limit of the band the controller may choose

CONTROLLER_MAX_TURNS        = 400               # model turns per controller session
CONTROLLER_MAX_WALL_S       = 21600             # wall time per controller session
CONTROLLER_MAX_SESSIONS     = 2                 # controller sessions per attempt (1 + one resume after a cap)
NO_TOOL_TIMEOUT_S           = 1800              # one no-tool call
AGENT_MAX_WALL_S            = 7200              # one agent session (one shuffle)
AGENT_MAX_TURNS             = 300               # model turns per agent session
AGENT_MAX_GEOS_RUNS         = 60                # GEOS launches per agent session
AGENT_CORES                 = 16                # CPU cores pinned to one agent session
AGENT_NET                   = on                # on | off: network inside the agent sandbox
AGENT_REQUIRE_GEOS          = on                # on: a right letter counts only with at least one GEOS run (B2)
NO_TOOL_RETRY_MAX_TOKENS    = 40000             # max_tokens of the retry of a no-tool reply cut off by max_tokens (B1)
AGENT_RO_BINDS              = $AGENT_RO_BINDS   # extra read-only paths GEOS needs (e.g. the thirdPartyLibs install)
AGENT_VISIBLE_UNTRACKED     = $AGENT_VISIBLE_UNTRACKED  # untracked/ignored GEOS_ROOT paths the agent may see; default = the build root that holds bin/ and lib/ of GEOS_BIN (e.g. build-conda-geos-release/)
TOOL_OUTPUT_MAX_CHARS       = 6000              # longer tool outputs are cut (head + tail) before the model sees them
DAILY_BUDGET_USD            = 8                 # no new model call once today's spend (server date, all runs) reaches this
```

The launcher (`qa_run_deck.py`, Section 3.4) checks at startup, before any model call:
every `$` variable is set (`REASONING_EFFORT`, `AGENT_RO_BINDS` and
`AGENT_VISIBLE_UNTRACKED` may be empty; the `*_MODEL` variables default to `MODEL`);
`NEMONIST_API_KEY` is non-empty (never printed); `QUESTION_TYPE` is one of the Section 8
types; `GEOS_BIN` runs; `pricing.json` has a row for each of `GEN_MODEL`,
`NO_TOOL_MODEL` and `TOOL_MODEL`; and the agent sandbox passes its self-test (3.3). If a
check fails it stops and reports which (a failed self-test writes `deck_result.json` with
`SANDBOX_FAILED`). Model names are used exactly as given and are written to files only as
read from these variables.

---

## 2. Non-negotiable rules

1. **Everything is written inside `BENCH_DIR`.** The GEOS repository, the original deck
   and every file it includes are read-only. The controller works on copies.
2. All GEOS and Python work runs in the `geos` conda environment
   (`PYTHONDONTWRITEBYTECODE=1`). Never use system Python or `sudo`. A missing package
   is not installed by the controller: stop and report it.
3. **Every GEOS run gets its own new output directory.** Never reuse or overwrite one.
4. **All simulations of an attempt use the same solver and time-stepping settings.**
   Runs differ only in the swept inputs (and, where needed, output-recording
   frequency). Never change solver tolerances or time-step controls for single runs.
5. **The simulations are ground truth.** The true answer is traceable to specific runs,
   and so is every sweep distractor. The only value not taken from a run is the
   recorded seeded random distractor of Section 9.3, used only when no simulated value
   fits. Never estimate, extrapolate or invent any other value; unknown values are
   `null`.
6. **The question is frozen** (text + SHA256) before any evaluated model sees it, and
   never edited afterwards. The options and the per-shuffle letter keys are fixed
   before the first no-tool call.
7. **The no-tool model** receives only its fixed prompt, the question and one
   shuffle's options. Its calls carry no `tools` parameter, so it cannot call a tool.
8. **The tool agent** runs only inside the sandbox of Section 3.3, in which
   `BENCH_DIR`, `GEOS_Agent/`, every other untracked or ignored path of `GEOS_ROOT`
   (except `AGENT_VISIBLE_UNTRACKED`) and the API key do not exist. It never receives a path
   into `BENCH_DIR`, the answer key, the sweep, the memory or any other attempt.
9. **Sessions never share context.** Every no-tool call and every agent session is
   fresh. Shuffles are run one after the other, never in parallel.
10. Never fabricate results, costs or token counts. Never edit an option, an answer
    letter or a model answer by hand.
11. **Reuse scripts; one command per stage.** Before writing a script, look in
    `BENCH_DIR/_global/scripts/qa/` (harness, read-only) and `BENCH_DIR/_global/scripts/`.
    A new script must be generic (not tied to one deck), saved in
    `BENCH_DIR/_global/scripts/`, and reused; deck-specific scripts (e.g.
    `extract_table.py`) live in the attempt's `sweep/`.
    Mechanical stages (build + launch sweep, extract table, refine, verify, freeze,
    build MCQ) each run as ONE command.
12. **Keep controller context small.** Never print a whole file, log, CSV or HDF5 dump.
    Every command prints at most ~50 lines. Wait for long jobs inside one command
    (a loop with `sleep`), not with repeated tool calls: every turn costs API money.
13. **Keep evaluation records small.** Agent workspaces and full agent transcripts are
    deleted after each session; only the records of Section 10 and the C3 keep-list
    (Section 12) are kept.

---

## 3. Harness, API, sandbox and costs

All mechanics live in `BENCH_DIR/_global/scripts/qa/`. The controller never edits
these harness scripts (`qa_llm.py`, `qa_agent_loop.py`, `qa_sandbox.py`,
`qa_build_mcq.py`, `qa_verify.py`, `qa_eval.py`, `qa_run_deck.py`, `qa_common.py`,
`qa_sweep.py`); they are mounted
read-only in its sandbox. If one is broken, the controller stops and reports it.

### 3.1 API calls (`qa_llm.py`)

Every model call is an OpenAI-compatible `POST {NEMONIST_BASE_URL}/chat/completions`
with `Authorization: Bearer $NEMONIST_API_KEY`, the configured model id, and the thinking
request `reasoning: {"max_tokens": REASONING_MAX_TOKENS}` with `max_tokens =
REASONING_MAX_TOKENS + MAX_ANSWER_TOKENS` (tested: Nemonist passes thinking through for
Claude only in this form; `reasoning_effort` had no effect). For every call the script records,
from the response:

- `model` reported (must equal the requested id, or the requested id followed by a
  date or version suffix; otherwise stop and report);
- `usage.prompt_tokens`, `usage.completion_tokens`,
  `usage.completion_tokens_details.reasoning_tokens`,
  `usage.prompt_tokens_details.cached_tokens` and
  `usage.prompt_tokens_details.cache_write_tokens`;
- **actual cost** = `usage.cost_details.upstream_inference_cost` when `usage.is_byok`
  is true (the provider bills the upstream cost; any gateway fee in `usage.cost` is
  recorded separately as `gateway_fee` and added), else `usage.cost`. A missing value
  is `null`, never 0;
- **rate cost** = the same tokens priced with the row of `BENCH_DIR/_global/pricing.json`
  whose key is exactly the model id (Section 11), as a cross-check;
- wall time and the provider reported.

Reasoning tokens are billed as output and are already inside `completion_tokens`; they
are reported separately, never added a second time. If the upstream (or, without BYOK, gateway) cost and
the rate cost differ by more than 2% (the BYOK `gateway_fee` is left out of this
comparison), the call is flagged `cost_mismatch` (both values kept; actual cost is the
one summed; when the actual cost is `null`, the rate cost is summed and the call is
flagged `cost_estimated`). Every call is appended to its usage file the moment its
response arrives, so an interrupted session keeps its spent cost. **Daily budget.** Every call is also appended to `BENCH_DIR/_global/spend_ledger.jsonl`
(date, model, cost); before each call the script sums today's entries and, at
`DAILY_BUDGET_USD` or above, stops exactly like a usage-limit stop (exit 75, state saved,
resumable the next day). A technical failure (network, HTTP 5xx, timeout) is retried once with
backoff; this is the only technical retry layer (Part B retries only for a missing
answer letter, Section 10). HTTP 401/402/429 that persists after the retry is a **usage-limit stop**: the
script saves state, appends `{stage, model, http_status, timestamp}` to
`BENCH_DIR/_global/controller_logs/interruptions.json`, and exits with code 75.

Earlier thinking is never sent back in later turns (each call thinks afresh): through this
gateway the signed thinking blocks fail verification, and removing them mid-session would
change the prompt prefix and defeat caching. For models whose id starts with `anthropic/`,
the harness marks the system prompt and the last two user/tool messages with
`cache_control` so long sessions reuse the cached prefix;
`cached_tokens > 0` in later turns confirms it works.

### 3.2 Tool loop (`qa_agent_loop.py`)

One loop serves both the controller (role `controller`) and the tool agent (role
`agent`). Each turn sends the conversation with three tools:

| Tool | Arguments | Effect |
|---|---|---|
| `bash` | `command`, optional `timeout_s` | runs the command with `bash -lc` inside the role's sandbox, in the role's working directory; returns exit code, stdout and stderr |
| `read_file` | `path`, optional `offset`, `limit` | returns at most `limit` lines |
| `write_file` | `path`, `content` | writes the file (only where the sandbox allows writing) |

**Nemonist memory (scope = folder).** The gateway gives the model a memory whose scope is
the folder holding its `.nemonist/` files. The generator (controller) keeps ONE persistent
memory folder, `BENCH_DIR/_global/generator_memory/` (its working directory), so it carries
memory across questions and decks. Each agent attempt runs in a fresh, empty `/work`
deleted afterwards, so its memory never reaches another attempt or gate, and the
generator's folder is hidden from it (inside `BENCH_DIR`). The no-tool gate has no tools,
hence no memory. Tool calls that touch `.nemonist` are counted as `nemonist_attempts`.

Every tool output longer than `TOOL_OUTPUT_MAX_CHARS` is cut to its head and tail with
a `[... N chars cut ...]` marker. The loop ends when the model replies without a tool
call, or at the role's cap. For the controller, a reply without a tool call while the
attempt is not finished (no `READY_FOR_EVAL`, `GENERATION_FAILED` or deck status) is
answered with a fixed "continue Part A, nobody will answer" message, at most 3 times per
session; after that the session ends as `stopped_unfinished`. The caps are (controller: `CONTROLLER_MAX_TURNS`, `CONTROLLER_MAX_WALL_S`;
agent: `AGENT_MAX_TURNS`, `AGENT_MAX_WALL_S`). When 90% of a cap is used, the loop adds
one user message: "Limit nearly reached: finish now" (for the agent: "give your final
answer now"); at the cap it allows one last reply without tools. Every call's usage and
cost is appended to a usage file OUTSIDE the role's sandbox: the controller's to
`attempt_<n>/controller/usage.jsonl` (all sessions of the attempt append to the same
file), the agent's to `attempt_<n>/eval/agent_s<k>_usage.jsonl`. A session never resumes
another's conversation.

### 3.3 Sandboxes (`qa_sandbox.py`, bubblewrap `bwrap` from the `geos` env)

Both roles run their `bash` commands under `bwrap --die-with-parent --unshare-pid
--clearenv` with only `PATH`, `HOME`, `LANG`, `OMP_NUM_THREADS=1` and the conda
variables set. `NEMONIST_API_KEY` is never passed into a sandbox.

| Path | controller | agent |
|---|---|---|
| `/` (system) | read-only | read-only |
| `$HOME` | read-only, except `~/.config/nemonist` hidden | hidden (empty tmpfs), except the binds below |
| `GEOS_ROOT` | read-only | read-only, with `GEOS_Agent/` and every untracked or ignored path hidden (below) |
| conda base, `AGENT_RO_BINDS` | read-only | read-only |
| `BENCH_DIR` | **read-write**, except `_global/scripts/qa/`, `_global/pricing.json` and every `attempt_<n>/controller/` and `attempt_<n>/eval/` (read-only) | **hidden** |
| own workspace | working directory `BENCH_DIR/_global/generator_memory/` (persistent memory folder); writes the deck folder by absolute path | an empty host folder `BENCH_DIR/_global/agent_workspaces/<deck_slug>__<RUN_TAG>__a<n>__s<k>/`, mounted read-write at the neutral path `/work`, the only writable place |
| `/tmp` | private tmpfs | private tmpfs (GEOS/MPI need it) |
| network | on | `AGENT_NET` |
| CPUs | all | `taskset` to `AGENT_CORES` cores |

**Hidden GEOS_ROOT paths (agent).** Besides `BENCH_DIR` (normally
`GEOS_ROOT/geos_sweep_bench`) and `GEOS_Agent/`, the sandbox builder hides every path
that `git -C GEOS_ROOT status --porcelain --ignored` lists as untracked (`??`) or
ignored (`!!`) — earlier question files, simulation outputs, notes, `*_runtime.txt`
files — except the paths in `AGENT_VISIBLE_UNTRACKED` (default: the whole build root
that holds `bin/` and `lib/` of `GEOS_BIN`, e.g. `build-conda-geos-release/`, since
`geosx` needs its `lib/`). The agent thus sees the tracked GEOS repository (source, docs,
`inputFiles/`) plus the build; generated docs that are git-ignored (e.g.
`src/docs/sphinx/datastructure/`) are hidden too unless added to
`AGENT_VISIBLE_UNTRACKED`. The hidden list is written to `run_config.json`.

**Sandbox self-test** (by the launcher at startup, Section 1, and before each agent
session): `geosx --help` runs; `BENCH_DIR`, `GEOS_Agent/` and every hidden path list as
empty; `$NEMONIST_API_KEY` is empty; writing to `GEOS_ROOT` fails; writing to `/work`
succeeds. A failure at startup stops the deck with `SANDBOX_FAILED`; a failure before a
session stops the deck the same way, after recording the attempt as `SANDBOX_FAILED`.

GEOS launches of the agent are counted by a `geosx` wrapper placed first on the agent's
`PATH` (it forwards to `GEOS_BIN`) and by scanning its `bash` commands for the binary
name. This is a soft cap (the real binary stays reachable inside the build directory):
beyond `AGENT_MAX_GEOS_RUNS` the harness refuses further commands that launch GEOS and
tells the agent so. Any agent command naming a hidden path is logged as
`forbidden_path_attempts` (the content was not visible; this is information, not a
failure).

### 3.4 Scripts and their order

```text
qa_run_deck.py           launcher: config checks, then per attempt n = 1..MAX_ATTEMPTS:
                         controller session (Part A) -> qa_eval.py (Part B) -> Part C
qa_agent_loop.py         tool loop for controller and agent (3.2)
qa_llm.py                one API call + usage/cost record (3.1)
qa_sandbox.py            bwrap command builder + self-test (3.3)
qa_build_mcq.py          distractor selection + options + shuffles (A9)
qa_eval.py               no-tool shuffles, agent shuffles, scoring, costs, memory entry (Part B, C)
qa_verify.py             script checks of A8 (generic; read-only for the controller)
qa_sweep.py              reads an attempt's simulated points (data conventions of A6)
qa_screen_infer.py       screens the sweep table for infer_then_predict (A, B) pairs (8.1), one command
qa_common.py             shared helpers: configuration, prompt blocks, pricing, option format
```

Who writes what: the controller writes everything of Part A, and `deck_result.json`
only for `DECK_UNSUITABLE` / `DECK_BASELINE_FAILED`. `qa_eval.py` writes `eval/`,
`attempt_result.json` and the memory entry of an evaluated attempt. The launcher writes
`run_config.json`, the `attempt_result.json` and memory entry of a `GENERATION_FAILED`
or `SANDBOX_FAILED` attempt, `results.md`, and `deck_result.json` in every other case;
when the controller already wrote `deck_result.json`, the launcher only adds the costs
and keeps the status.

### 3.5 Helper scripts: commands to use (do not read their code)

These are the commands that work. Use them as given; read a script's code only when its
command fails, and then only the part named in the error. Shorthands: `S=$BENCH_DIR/_global/scripts`,
`DK=` the deck folder, `A=$DK/attempt_<n>`, `GEOS=$GEOS_BIN`. Always `export PYTHONDONTWRITEBYTECODE=1`.

```text
A0 copy the deck (setup_deck.py builds its own tree under --D, so use a temp folder):
  T=$BENCH_DIR/_tmp_setup_$RUN_TAG; mkdir -p $T
  python $S/setup_deck.py --D $T --geos-root $GEOS_ROOT --xml $INPUT_XML --run-tag $RUN_TAG --geos-bin $GEOS | head -20
  cp -r $T/decks/<deck_slug>/$RUN_TAG/source $DK/source && rm -rf $T
  -> source/ (deck + included files), source_manifest.json, deck_keywords.json

A2 baseline (one GEOS run; input.xml must be in the run folder, output/ must not exist):
  mkdir -p $DK/exploration/baseline; cp $DK/source/*.xml $DK/exploration/baseline/
  cd $DK/exploration/baseline; cp <deck>.xml input.xml
  python $S/run_geos.py --geos $GEOS --run-dir $PWD --run-id baseline --write-command-txt | tail -5
  -> run.json (status, first_error), output/, stdout.log, stderr.log

A3 output inventory and mesh check:
  python $S/inventory_outputs.py $DK/exploration/baseline/output $DK/exploration/output_manifest.json | head -20
  python $S/check_mesh_description.py --baseline-output $DK/exploration/baseline/output --description mesh_desc.json | tail -8
  mesh_desc.json = {"bodies": [{"name": "<mesh body>", ...}]}; body kinds: box (x, nx, y, ny, z, nz),
  explicit_nodes, box_plus_polylines, piecewise_box, split_notch_plate, gll_box, inclined_cartesian_wellbore
  (radius_nodes, theta_deg, z, map_from_radius, outer_half_width, inner_radius, trajectory: two xyz points),
  radial_cartesian_blend. The fields of each kind are in the script's top docstring: `sed -n 1,60p $S/check_mesh_description.py`.

A5 sweep (one command; resumable; every launch counted in launch_ledger.json):
  $A/sweep/template.xml   the deck with {{NAME}} placeholders (every occurrence replaced)
  $A/sweep/sweep_config.json:
    {"geos": "<GEOS_BIN>", "template": "template.xml", "parallel_jobs": <PARALLEL_JOBS>,
     "budget": {"max_launches": <cap>, "ledger": "launch_ledger.json"},
     "derived": {"NAME": "<python expression of params, math available>"},
     "axes": {"<axis>": [grid values], ...},
     "sets": {"pilot": {"dir": "pilot", "prefix": "pil_", "points": [{"run_id": "pil_01", "params": {...}}]},
              "sweep": {"dir": "runs", "prefix": "run_", "start": 1, "grid": {"<axis>": [...], ...}}},
     "failed_runs_csv": "failed_runs.csv"}
  cd $A/sweep; python $S/sweep_runner.py --config sweep_config.json --set pilot --jobs $PARALLEL_JOBS | tail -5
  cd $A/sweep; python $S/sweep_runner.py --config sweep_config.json --set sweep --jobs $PARALLEL_JOBS | tail -5
  -> <set dir>/<run_id>/{input.xml, run.json (params, status), output/}

A5 table: write a deck-specific $A/sweep/extract_table.py with two modes:
  --table <sweep dir> --csv sweep_table.csv   one row per successful run: run_id, the axes, the observables
  --observables <run dir> [--delete-fields]   writes <run dir>/observables.json =
      {"run_id": "refine/ref_NN", "params": {...}, "observables": {<column>: value}} (used by refinement)
  (the generic $S/extract_table.py --spec ... reads TimeHistory files; use it when the deck writes them)

A6 choose the question (infer_then_predict): screen every hidden axis / case / (A, B) pair in one command
  instead of trying pairs one by one:
  cd $S/qa; python qa_screen_infer.py --attempt-dir $A [--hidden <axis>] [--columns a,b,..] | head -25
  -> passing pairs, best first: lin× (straight-line miss in B tolerances, needs > 3), round (needs < 1/3),
     bspan% (how much B moves), h* (a good hidden value). Pick one, then record the real 8.1 checks.

A7 refinement (bisection on one axis; output types and infer_then_predict):
  write $A/sweep/refine_question.json = [{"qid": "", "needs_refinement": true,
     "tolerance_type": "relative", "tolerance_value": <TOLERANCE_PERCENT>,
     "search": {"kind": "infer_predict", "axis": "<hidden axis>", "target_expr": "O['<A column>']",
                "b_expr": "O['<B column>']", "target_value": <observed A>, "fixed_params": {<other axes>}},
     "bracket": {"lo": .., "hi": .., "f_lo": <A at lo>, "f_hi": <A at hi>, "b_lo": .., "b_hi": ..,
                 "run_lo": "<run_id>", "run_hi": "<run_id>"}}]
  (for inverse / threshold: drop "kind"/"b_expr"; target_expr is the searched output)
  cd $A/sweep; python $S/refine_bisect.py --config sweep_config.json --questions refine_question.json \
      --extract-script $PWD/extract_table.py --tol-percent <TOLERANCE_PERCENT> --jobs 1 --out refine_results.json | tail -5
  -> refine/ref_NN/observables.json; refine_results.json[""] = final bracket, true value, history

A4 (attempt n > 1 after TOO_EASY) read how the no-tool model answered attempt m = n - 1
  (unquoted E, so that $DK is expanded; write m as a number):
  python - <<E
  import json, glob
  for f in sorted(glob.glob('$DK/attempt_<m>/eval/no_tool_s*.json')):
      d = json.load(open(f)); t = d['answer_text']
      print('=== %s letter %s computed %s\n%s\n...\n%s' % (f[-16:], d.get('letter'), d.get('computed_value'), t[:1200], t[-600:]))
  E

A4 (attempt n > 1 after AGENT_FAILED) read the agent's final messages of attempt m = n - 1:
  python - <<E
  import json, glob
  for f in sorted(glob.glob('$DK/attempt_<m>/eval/agent_s*.json')):
      d = json.load(open(f)); t = d.get('final_message') or ''
      print('=== %s outcome %s computed %s GEOS runs %s\n%s' % (f[-14:], d.get('outcome'), d.get('computed_value'), d.get('geos_runs'), t[:2500]))
  E

A8 verify and freeze, A9 MCQ (harness scripts, Sections 6 and 9):
  cd $S/qa; QUESTION_TYPE=$QUESTION_TYPE TOLERANCE_PERCENT=$TOLERANCE_PERCENT python qa_verify.py --attempt-dir $A | cut -c1-200
  ... fix what failed, then the same command with --freeze
  cd $S/qa; python qa_build_mcq.py --attempt-dir $A --d-min-pct <..> --d-max-pct <..> --placement above|below|straddle --reason "<one sentence>"
```

---

## 4. Workspace layout

```text
BENCH_DIR/
├── _global/
│   ├── pricing.json
│   ├── scripts/qa/                 harness + reusable QA scripts
│   ├── scripts/                    older reusable scripts (check_mesh_description.py, ...)
│   ├── parsers/                    reusable output readers + parser_registry.json (shared with earlier protocols)
│   ├── controller_logs/            interruptions.json, launcher logs
│   └── agent_workspaces/           one EMPTY folder per agent session (deleted after it)
│
└── decks/<deck_slug>/<RUN_TAG>/
    ├── run_config.json             all Section 1 values (no key), host, GEOS commit, bwrap version, hidden paths
    ├── deck_memory.md              one entry per finished attempt (Part C); KEPT, never deleted
    ├── source/                     deck + included decks + data files, source_manifest.json, deck_keywords.json
    ├── exploration/                baseline/ (input.xml, run.json, logs, output/, fixes.md if fixed),
    │                               deck_summary.md, output_manifest.json
    ├── attempt_<n>/
    │   ├── controller/             usage.jsonl, transcript_<session>.jsonl (all controller sessions of this attempt)
    │   ├── design.md
    │   ├── sweep/                  sweep_config.json, template.xml, extract_table.py, runs/, refine/, pilot/,
    │   │                           sweep_table.csv, failed_runs.csv, sweep_readme.md, reuse.json (A4),
    │   │                           extra/ext_NN/ (neighbouring-case runs for distractors, A9)
    │   ├── question/
    │   │   ├── question.txt        exact frozen text
    │   │   ├── answer_key.json     true value + provenance + refinement (never shown to an evaluated model)
    │   │   ├── verify.json         A8 results
    │   │   └── mcq.json            options, distractor provenance, band, per-shuffle letter keys
    │   ├── eval/
    │   │   ├── no_tool_s<k>.json   prompt hash, answer text, letter, outcome, usage, cost (all calls of the shuffle)
    │   │   ├── no_tool_s<k>_usage.jsonl one line per no-tool API call
    │   │   ├── agent_s<k>.json     final message, letter, outcome, turns, GEOS runs, wall time, usage, cost
    │   │   └── agent_s<k>_usage.jsonl   one line per agent API call (written outside the sandbox)
    │   ├── attempt_status.json     stage + status (Section 10)
    │   └── attempt_result.json     outcome, picks, costs
    ├── results.md                  readable table of all attempts
    └── deck_result.json            KEPT | NOT_KEPT | DECK_UNSUITABLE | DECK_BASELINE_FAILED | SANDBOX_FAILED
```

`deck_slug` = XML filename stem + `__` + first 8 hex characters of its SHA256. Never read
results of another `RUN_TAG`.

---

## 5. Workflow overview

```text
qa_run_deck.py
 ├─ attempt n (n = 1 .. MAX_ATTEMPTS)
 │   ├─ Part A  controller session (fresh), executes A0–A10 of this file, then stops
 │   │          A1–A3 run only in attempt 1; later attempts reuse source/ and exploration/
 │   ├─ Part B  qa_eval.py
 │   │          B1 no-tool: N_SHUFFLES calls, one after the other
 │   │          no-tool wrong < NO_TOOL_FAIL_MIN           -> attempt TOO_EASY
 │   │          B2 agent: same shuffles, one session each, one after the other,
 │   │             stop at AGENT_STOP_FAILS failures
 │   │          agent right >= AGENT_PASS_MIN              -> attempt KEPT
 │   │          otherwise                                  -> attempt AGENT_FAILED
 │   └─ Part C  append the memory entry; KEPT -> deck KEPT, stop;
 │              else next attempt, or deck NOT_KEPT after MAX_ATTEMPTS
 └─ results.md, deck_result.json
```

An attempt whose controller cannot produce a valid frozen question ends
`GENERATION_FAILED` and also counts as an attempt. If a controller session ends at a cap
(3.2) before A10, the launcher starts a new controller session that resumes the attempt
(A0.3); after `CONTROLLER_MAX_SESSIONS` sessions without `READY_FOR_EVAL`, the launcher
marks the attempt `GENERATION_FAILED` (reason: session cap). A session that ends without
a cap and without a terminal status (A10, `GENERATION_FAILED`, or a deck status) is
treated the same way. A session ended by a usage-limit stop (exit code 75) does not
count toward `CONTROLLER_MAX_SESSIONS`; the launcher stops and the next launch resumes.

---

## 6. Part A — Generate one question (controller)

The controller session receives this kickoff message and nothing else:
`Read <PROTOCOL_FILE> completely and execute Part A for attempt <n> of deck <INPUT_XML>
(deck folder <deck folder>, question type <QUESTION_TYPE>). Stop after A10.`

### A0 — Setup
1. Read Section 1 values from `run_config.json` (written by the launcher after its
   startup checks). If a value the controller needs is missing, write
   `attempt_status.json` with `GENERATION_FAILED` and the reason, and stop.
2. Read `deck_memory.md` if it exists (attempts n > 1).
3. Apply resume (Section 12): continue at the first unfinished step of this attempt.

### A1 — Explore (attempt 1 only)
1. Read the whole target XML and, recursively, every included XML and every data file
   it references (tables, functions, meshes). Copy all of them into `source/`, keeping
   relative layout; GEOS finds many data files by bare filename, so each must sit next
   to every deck copy that uses it.
2. Write `source/source_manifest.json`: every file, original path, SHA256, purpose, and
   whether it holds **numeric table values** or only **model settings**.
3. Understand the physics and solver stack, mesh, constitutive models, parameters,
   initial and boundary conditions, sources, events, time stepping and outputs, and any
   behaviour defined by GEOS source rather than the XML (GEOS source may be read).
   Note the valid range of every table or model input.
4. Build `source/deck_keywords.json` = `{"keywords": [...], "excluded_common_words":
   [...]}`: every XML element name, attribute name and name-like attribute value
   (model, solver, region, function names) of the deck and its includes, minus single
   plain words (letters only, all lower case or only an initial capital, e.g. "fault",
   "Domain"). Keep CamelCase, tokens with a digit, underscore, dot or inner capital, and
   acronyms.
5. **Suitability.** The deck is UNSUITABLE when its physics-defining content cannot be
   written into a question in plain scientific words (e.g. behaviour only from an
   opaque precomputed table of unknown origin, or answers that depend at the tolerance
   level on a file mesh that cannot be described in a few sentences). Then write
   `deck_result.json` with `DECK_UNSUITABLE` and the reason, and stop. A file mesh that
   can be described (element type, counts, spacing, grading) is not a reason.

### A2 — Baseline (attempt 1 only)
Run the unchanged working copy once into `exploration/baseline/output`, saving the
command, logs and `run.json` (times, wall time, exit code, input SHA256). If it fails,
read the error from the TOP of the log, fix the working copy (never the original),
record every change in `exploration/baseline/fixes.md`, and rerun in a new directory.
If it cannot be fixed, write `deck_result.json` with `DECK_BASELINE_FAILED` and stop.
Never rerun a successful baseline.

### A3 — Inspect (attempt 1 only)
1. Inventory every output file (path, format, size, purpose, parser) into
   `exploration/output_manifest.json`; summarise contents without dumping arrays. Look
   for a parser in `_global/parsers/` first; a new one must be generic and registered
   in `parser_registry.json`.
2. Write `exploration/deck_summary.md`: physical system, key parameters, solver and
   time structure, mesh, outputs, baseline runtime, observed ranges, **where and when
   things actually happen**, promising nonlinearities, and three numbered lists:
   - **(a) Physics shortcuts that fail** — simplified reasoning a scientist could apply
     to the stated inputs that gives the wrong number here. Questions are built on these.
   - **(b1) Input-level items** (numbered 1, 2, …) — what a user sets or must know to
     read the output: scaling conventions of inputs, which variable an output stores,
     output timing, solver parameters that set a physical quantity, parameters a user
     could set that the deck leaves at default, the problem type actually solved,
     discrepancies between the stated and the actual initial state, and behaviour that
     contradicts the stated physical model (marked **ARTIFACT**). For each non-artifact
     item, the plain-words sentence a question would use.
   - **(b2) Internal behaviour** (numbered B2-1, B2-2, …) — how the solver implements a
     stated model (internal equations, hard-coded constants, implementation rules).
     Never stated in a question; part of what must be predicted. Mesh geometry is never
     b2: node positions are problem input (b1), or the deck is unsuitable for questions
     that depend on them.
3. If a question will depend on a generated or file mesh, write its node-position
   description and test it with `_global/scripts/check_mesh_description.py` (reads the
   baseline node positions; every node must match within 1% of the smallest element
   size). Record the description and result in `deck_summary.md`.

### A4 — Design (every attempt)
Using `deck_summary.md` and `deck_memory.md`, write `attempt_<n>/design.md`:
- the planned question: `QUESTION_TYPE`, observable (physical field), location and
  time, which list (a) shortcut it targets and why a no-tool model should miss it;
- for attempts n > 1: what the memory shows went wrong (too easy: which option the
  no-tool model picked and why the shortcut worked; agent failed: what the agent
  computed and why it missed) and what is changed. When the previous attempt was
  `TOO_EASY`, read the no-tool answers themselves (command in Section 3.5): note the
  method each one used (formula, estimate, guess among the options, picking the middle
  value) and design the new question so that this method fails. When it was
  `AGENT_FAILED`, read the agent's final messages (Section 3.5): what it set up, which
  inputs it said were missing or assumed, which solver it used, what it computed. A
  consistent wrong value usually means the question left an input out or allowed another
  model/solver choice: state that input or choice explicitly in the new question. The `COMPUTED_VALUE` in
  the memory is often just the chosen option's value, not the model's own result. A new attempt must change the
  question substantially (different observable, case, hidden input or structural
  option), not only reword it;
- **sweep reuse decision.** The controller decides whether an earlier attempt's sweep
  serves this question. If yes, write `attempt_<n>/sweep/reuse.json` =
  `{"from_attempt": m, "reason": ""}`; that sweep's table and runs are read in place
  (never copied) and only new runs (pilots, extra sweep points, refinement) are
  launched under `attempt_<n>/sweep/`. If no, design a new sweep;
- sweep axes, ranges and grid, chosen so that (i) the question's case and its
  refinement are covered, (ii) neighbouring cases (one other axis changed, or the
  hidden input at other grid values) provide distractor candidates in the band of
  Section 9.3, and (iii) the type checks of Section 8 can be made on the table. Include
  a structural axis (a change of scenario type, e.g. fixed-pressure vs fixed-rate
  boundary, drained vs undrained, monotonic vs cyclic) where the deck supports one;
- **mesh size.** If the deck's mesh is generated internally (element counts set in the XML),
  refine it in `template.xml` so that solving the question with a hand-written solver is
  impractical (for example a few thousand elements or more), while one GEOS run of the
  case still finishes in at most 3 minutes (check the runtime with a pilot). The refined
  mesh is stated in the question like any mesh (element type, counts, spacing or
  grading); test its node-position description (A3) on a pilot run's output. A mesh read
  from a file is kept as it is. Record the chosen counts, the pilot runtime and the reason
  in `design.md`;
- checks before running: no planned observable gets its effect from an ARTIFACT item;
  every input stays inside every table's and model's valid range; something happens at
  the asked place and time; for a time-type answer, the time step near the event is at
  most half the tolerance (set a finer fixed step in the template if needed, for every
  run of the attempt);
- the run count: pilots + sweep ≤ `MAX_SIMULATIONS_PER_ATTEMPT − REFINEMENT_RESERVE_RUNS`.

**Staircase inputs.** An input that moves a geometric feature across a fixed mesh
(fracture length or position, well or source location, layer boundary) changes results
only when it crosses a cell edge. Never use it as the continuous axis of an inverse,
threshold, optimisation or infer-then-predict question.

### A5 — Pilot, sweep, table
1. **Pilot:** 2–3 runs at the most extreme corners of the new grid points, covering
   every structural option, in `sweep/pilot/`. Fix the template and rerun pilots in new
   directories until they pass. Measure their wall time to size the sweep.
2. **Sweep:** `template.xml` with placeholders (if a placeholder appears twice on one
   line, substitute with `s/.../.../g`); one `runs/run_NNNN/input.xml` per run with its
   data files alongside; run with `OMP_NUM_THREADS=1` and `xargs -P PARALLEL_JOBS` as
   ONE command. Each run writes one `run.json` (run id, swept values, command, times,
   wall time, exit code, input SHA256, output dir). Time-varying inputs use a
   `TableFunction` with `interpolation="lower"`. Record only the numbers the design
   needs, at the times it needs them; no full-field or restart output. Failed runs go
   to `failed_runs.csv` with the first error line; above `MAX_FAILED_RUN_PERCENT`, stop,
   fix the template and rerun the sweep in fresh directories.
3. **Table:** `extract_table.py` reads each successful run's own output (check path
   and timestamps) and writes `sweep_table.csv` (swept values, observables, run id).
   Then delete the run's logs and every `*.hdf5`. Write a compact `sweep_readme.md`:
   what was varied and held fixed, column meanings and extraction, runs attempted /
   succeeded / failed, wall time, the rebuild command.
4. Total launches of the attempt (pilots + sweep + refinement, failed included) never
   exceed `MAX_SIMULATIONS_PER_ATTEMPT`. Runs of a reused sweep do not count.

### A6 — Write the question
The controller writes the question itself (no separate generation call), obeying
Section 7 and the `QUESTION_TYPE` rules of Section 8. It inspects `sweep_table.csv` only
through short Python commands (column list, ranges, filtered rows, brackets), never by
reading the whole file. It writes `answer_key.json`:

```json
{
  "question_type": "", "observable": "", "units": "",
  "true_value_grid": null, "true_value": null,
  "tolerance_type": "relative | absolute", "tolerance_value": null,
  "tolerance_abs": null,
  "needs_refinement": true,
  "bracket_from_table": null,
  "provenance": { "table": "", "runs": [], "columns": [], "selection_rule": "" },
  "case": { "<axis>": "<value>" },
  "type_fields": {},
  "list_b_items": [ { "item": "", "handling": "stated | not relevant", "sentence": "" } ],
  "hardness_rationale": "",
  "naive_prediction": null
}
```

`case` gives the question's value on every swept axis (the hidden input marked
`"hidden"`); `tolerance_abs` is the tolerance as an absolute amount at the true value;
`type_fields` holds the Section 8 fields of the type.

**Completeness self-check.** Reread the question as if knowing only its text and check
that it states: 1 problem type (steady/transient, static/dynamic); 2 geometry and
dimensions; 3 mesh and time stepping; 4 every parameter value the stated models need,
including defaults a user could set; 5 the name of each physical model chosen and any
user-chosen history dependence; 6 initial conditions; 7 boundary conditions; 8 sources
and loads with their amplitude conventions; 9 the load or event sequence; 10 the
requested quantity (variable, location, time, sign, units, averaging). Add what is
missing, obeying Section 7.

**Data conventions** (read by `qa_sweep.py`, `qa_verify.py`, `qa_build_mcq.py`):
- `sweep/sweep_config.json` = `{"axes": {"<axis>": [grid values]}, ...}`; `sweep/sweep_table.csv`
  has a `run_id` column, one column per axis and one per observable, one row per successful
  run; `sweep/failed_runs.csv` has `run_id`, the axis values and `error`.
- Each refinement run `sweep/refine/ref_NN/` and extra run `sweep/extra/ext_NN/` keeps
  `observables.json` = `{"run_id": "refine/ref_NN", "params": {<axis>: value},
  "observables": {<column>: value}}`.
- Run ids in the answer key are bare (`run_0004`, `refine/ref_03`) for the attempt's own
  runs and `a<m>:<id>` for runs of the sweep reused from attempt m.
- `answer_key.json` also holds: `answer_column` (output-type answers: the column of the
  answer); `refinement` = `{"final_bracket": [lo, hi], "bracket_width", "bracket_runs":
  [run at lo, run at hi], "final_run": <run giving the true value, output types>,
  "history": [{"bracket": [lo, hi], "runs": [run_lo, run_hi]}, ...] (one entry per
  bisection step, starting with the table bracket), "runs": [...]}`; `exempt_numbers`
  (numbers copied from deck tables, exempt from the 4-significant-figure check); and
  `allowed_camelcase` (proper names only, e.g. a person's name; never a deck token).
- `type_fields` per type: `infer_then_predict` — `hidden_input`, `observed_output`,
  `observed_column` (A), `observed_value`, `inferred_input_value`, `baseline_value`,
  `infer_predict_checks`; `inverse`/`threshold` — `searched_axis`, `interval`,
  `output_column`, `target_value`, `baseline_value`; `optimisation` — `searched_axis`,
  `interval`, `output_column`, `sense` (`max`|`min`), `baseline_value`; `timing` —
  `time_step`, `output_interval`. `case` marks the hidden axis `"hidden"` and the searched
  axis `"searched"`.

### A7 — Refine
When `needs_refinement` is true, refine as Section 8 prescribes for the type, in
`sweep/refine/ref_NN/` (same template, solver settings and run rules as the sweep,
counted in the attempt budget, failures in `failed_runs.csv`). Each refinement run keeps
an `observables.json` with the numbers taken from it. Record in `answer_key.json`: the
final bracket and width, the refinement run ids, the true value. If a refinement run
fails, or the question would need more than `MAX_REFINEMENT_RUNS` launches, or the
attempt budget would be exceeded, rewrite the question (another case) and record the
discarded launches as `refinement_runs_discarded`.

### A8 — Verify and freeze
**Script checks** (`qa_verify.py`, results in `question/verify.json`):

```text
[ ] true value recomputed from sweep_table.csv (unrefined) or from the refinement record
    (refined) matches answer_key.json; no stale grid value remains
[ ] no provenance run failed or is adjacent (along any axis) to a failed run
[ ] true answer is not at an edge of the swept range
[ ] refinement: final bracket width / output interval <= half the tolerance
[ ] the Section 8 checks of QUESTION_TYPE pass, with their numbers recorded
[ ] tolerance type follows Section 7 rule 4
[ ] question text contains no entry of deck_keywords.json, no CamelCase class-style token,
    no "GEOS", "XML", "simulat", file names, paths or run ids
[ ] question text contains no tolerance statement, no answer-format instruction, no options
    and ends with "?"
[ ] observed values in the text have at most 4 significant figures (input tables copied
    from the deck are exempt) and match answer_key.json
[ ] list_b_items present; no ARTIFACT item affects the answer; every affecting item is
    `stated` and its sentence appears in the text
```

**Controller review** (what a script cannot judge): every Section 7 rule; the requested
quantity is precise; no hint or leakage; something happens at the asked place and time;
no hand shortcut (linear model, superposition, symmetry, steady state, textbook closed
form) solves it; physics consistency (the answer does not rest on behaviour the stated
model cannot produce — that would be an ARTIFACT; b2 behaviour is fine); every statement
about the initial or boundary state agrees with the baseline at t = 0.

If a check fails, fix the question (or choose another case) and rerun both checks on the
new text. To freeze, run `qa_verify.py --attempt-dir <attempt> --freeze` (ONE command): when
every script check passes it writes the SHA256 of `question.txt` into `verify.json` and marks
the attempt `FROZEN` in `attempt_status.json`; afterwards it refuses any change of the text. If no valid question can be produced within
the attempt budget, mark the attempt `GENERATION_FAILED` with the reason and stop.

### A9 — Build the MCQ
1. Choose the distractor band `[D_MIN_PCT, D_MAX_PCT]` and the placement (`above`,
   `below` or `straddle`, Section 9.3) and write them, with a one-sentence reason, into
   `design.md`.
2. Run `qa_build_mcq.py --attempt-dir <attempt> --d-min-pct <..> --d-max-pct <..>
   --placement <..> --reason "<..>"` (ONE command). It selects the two distractors, formats the options, builds the
   `N_SHUFFLES` letter keys, writes `mcq.json` (band included) and prints the options
   and the distractors' provenance, never the letter keys. If it reports that fewer than
   two simulated candidates fit (exit code 2), the controller may first launch a few
   extra runs of neighbouring cases (one swept axis changed, within the attempt budget)
   under `sweep/extra/ext_NN/` (same rules as refinement runs) and rerun; only then does
   it rerun with `--allow-random` to use the random fallback. If it reports
   `NO_VALID_OPTIONS`, the controller may choose another band once and rerun; still
   failing → `GENERATION_FAILED`.

### A10 — Hand-off
Write `attempt_status.json` = `{"stage": "A10", "status": "READY_FOR_EVAL"}` and stop.
The controller never runs Part B, never calls `NO_TOOL_MODEL` or `TOOL_MODEL`, and never
prints the letter keys.

---

## 7. Question content rules

A question MUST:
1. **State every input needed to define and run the case**: geometry and mesh (element
   type, counts, spacing or grading; for a generated or file mesh, the tested
   node-position description of A3), all material and fluid/solid properties with
   units, initial and boundary conditions, sources and loads with schedules, time-step
   size and end time, and the precisely defined requested quantity (what, where, when,
   units, sign convention, averaging, tie rule).
   - A data file with **numeric table values**: include the values.
   - A data file with only **model settings**: describe the model in plain scientific
     words (e.g. "gas density from the Span–Wagner equation of state") with its settings.
   - A search range is stated as a **continuous interval** (minimum and maximum only).
2. Ask about a place and time where something actually happens.
3. Have its true answer away from the edges of the swept range, and rely on no failed
   run or run adjacent to one.
4. **Tolerance** (internal; never written in the question): relative,
   `TOLERANCE_PERCENT`, by default. Absolute only for a quantity that can be zero or
   change sign whose true value is below 10% of its full range across the sweep; then
   `TOLERANCE_PERCENT` of that full range. A strictly positive quantity spanning orders
   of magnitude (permeability, time, rate constant) is always relative; so is every
   input-type answer (threshold, inverse, optimisation).
5. Give observed values from simulation output to 4 significant figures, as a
   measurement would be reported.
6. **State every list (b1) item that affects the answer** in plain scientific words, or
   not ask the question: problem type, which variable is requested (e.g. "effective
   stress, total stress plus pore pressure"), scaling conventions, the model chosen and
   every parameter value including defaults a user could set, the true initial state.
   Never ask a question whose answer comes from an ARTIFACT.
7. **Input-level test.** State what a scientist would set up for the simulator, in plain
   scientific words: geometry, mesh, time step and end time, every material and model
   parameter value, the name of each physical model chosen (e.g. "Brooks–Corey relative
   permeability with exponents 2 and 3", "Coulomb friction with coefficient 0.6"),
   initial and boundary conditions, sources and schedules, and the exact requested
   quantity. Do **not** state the equations the solver uses inside those models, nor
   implementation rules or hard-coded constants (list b2). The mesh is always input-level.
8. **No guessable answer.** An input-type answer (inverse, threshold, optimisation) or
   an inferred hidden input never equals a sweep grid value or the baseline value of
   that input; the observed value or limit is chosen between grid points and refined.
9. Read like a normal PhD-level scientific problem and end with "?".

A question MUST NOT:
- state or hint at the governing equations, the discretisation or time-integration
  scheme, solver methods or tolerances, or how the requested quantity is computed (the
  mesh, time step, problem type, constitutive model with its parameters and the exact
  definition of the requested variable are problem inputs, not hints);
- contain any GEOS keyword or internal name (deck keywords, CamelCase class names);
- mention GEOS, XML, simulators or simulation software, files, paths, hashes, output
  formats or run ids, or say or imply that a simulation or search is needed;
- state a tolerance or an answer format, list candidate values, or offer options (the
  options are added by `qa_build_mcq.py`, never written into `question.txt`);
- contain or leak the answer;
- **describe the solution**: no signs, trends, magnitudes, regimes, orderings or which
  effect dominates. If a sign is ambiguous only because of the result, redefine the
  quantity (e.g. ask for its magnitude);
- depend on hidden files.

**Implementation-determined choices.** A purely numerical choice the question does not
state (discretisation, solvers, upwinding, quadrature), the internal equations of a
stated model and its hard-coded constants are determined by GEOS; the simulation result
is the truth. This does not cover list (b1) items, which must be stated (rule 6).

---

## 8. Question types

`QUESTION_TYPE` selects one block below. Only that type's rules apply to the attempt.

### 8.1 `infer_then_predict` (default)
One continuous input is hidden and stated only as an interval. The question gives the
observed value of output A (4 significant figures) and asks for a DIFFERENT output B of
the same case. A and B are different physical quantities, or the same quantity at a
clearly different place or time (not B = A at a neighbouring cell).

`type_fields` = `{"hidden_input": {"name": <axis name in sweep_config.json>,
"interval": [min, max]}, "observed_output": "<A: what, where, when>",
"observed_value": <as written, 4 s.f.>, "inferred_input_value": null,
"infer_predict_checks": {...}}`.

Checks on the sweep table (all other inputs at the question's case), each recorded with
its numbers; if one fails, choose another case or another A/B pair. Screen the candidates
first with `qa_screen_infer.py` (Section 3.5), which estimates uniqueness, nonlinearity and
rounding for every pair from the grid in one command:
- **Uniqueness:** A is monotonic in the hidden input across the stated interval, so one
  input value reproduces the observed A;
- **Nonlinearity:** fit B against A by a straight line across the sweep points of the
  hidden input; its prediction at the observed A misses the true B by more than 3 × B's
  tolerance (recorded as `naive_prediction`);
- **Rounding robustness:** moving the observed A by half a unit of its 4th significant
  figure, either way, changes the true B by less than 1/3 of B's tolerance;
- **Not guessable:** the inferred input lies strictly between grid values and differs
  from the baseline value; the observed A lies between the A values of two
  neighbouring rows, never equal to one;
- **Interval placement:** the inferred input is at least 20% of the interval width from
  the interval midpoint and at least 5% from either end.

**Refinement:** bisect the hidden input inside its table bracket until the bracket width
is at most half the tolerance on the hidden input AND B at the two bracket ends differs
by at most half of B's tolerance. The inferred input is the bracket midpoint; run that
case once and take B from it as the true value. **No jump:** the last bisection step
must shrink the A difference across the bracket to at most 90% of the previous one, and
no two refinement points at different inputs give the same A (within 1e-9 of A's range).
Example: "The friction coefficient μ, the same on both interfaces, is not known; it lies
between 0.11 and 0.29. The ovalization of the casing bore in the final state is measured
as 9.993 mm. What is the cement–rock slip at θ = 45° in the final state, in micrometres?"

### 8.2 `inverse` / `threshold`
Inverse: given an observed output, which value of a continuous input produced it.
Threshold: the highest or lowest input that keeps an output below or above a limit.
The output must change monotonically and clearly with the input over the stated search
interval, which lies inside the swept range and contains exactly one answer, placed as
in the interval-placement check of 8.1. Refinement: bisection inside the table bracket
until its width is at most half the tolerance; the true value is the bracket midpoint;
the no-jump check of 8.1 applies to the output.

### 8.3 `optimisation`
Which input value maximises (or minimises) an output inside a stated interval. The sweep
must show a smooth rise and fall around the extremum. Refinement: keep three points
whose middle one is best, run one simulation at the middle of the wider half each step,
stop when the outer two are at most half the tolerance apart; the true value is the
midpoint of the final bracket. **Clear peak:** on each side, within two tolerance widths
of the peak, the curve is no steeper than that side's average slope (no cliff); no point
farther than one tolerance width from the peak lies within the noise of the peak value
(not flat). Interval placement as in 8.1.

### 8.4 `timing`
When an event first happens (arrival, crossing, peak, onset). The time step near the
event is at most half the tolerance, and the recorded output interval at most half the
tolerance (rerun with finer output recording only if needed). Between samples, linear
interpolation is allowed and recorded in the provenance.

### 8.5 `magnitude`
How large a quantity is at a stated place and time. Usually no refinement; the case must
still not be solvable by a hand shortcut.

---

## 9. MCQ construction (`qa_build_mcq.py`)

### 9.1 Format
All three numeric options use one format: 4 significant figures for a relative
tolerance; for an absolute tolerance, enough decimals to resolve one tenth of it. If
rounding makes two options equal, or a distractor within `tolerance_abs` of the true
value, that candidate is rejected. Option D is always `None of them`.

### 9.2 Shuffles
Shuffle k = 1 … `N_SHUFFLES`: the true answer takes letter A, B, C in shuffles 1, 2, 3
and the two distractors fill the other two of A–C in seeded random order. From shuffle 4
on, the letter order (true letter + distractor order, 6 possible) is a seeded random choice
among the orders not used by an earlier shuffle, so no two shuffles are identical (repeats
only after all 6 orders are used). Seed = `int(<first 8 hex digits of the question
SHA256>, 16) + k` (integer addition). The letter key per shuffle is stored in `mcq.json` and never shown to any model or
printed by the controller.

### 9.3 Distractors from the sweep
The controller sets the band `[D_MIN_PCT, D_MAX_PCT]` per question (A9): a distance from
the true value, in percent of |true value|, close enough to be a credible trap and far
enough to be unambiguous. Limits: `100 × tolerance_abs / |true| < D_MIN_PCT < D_MAX_PCT ≤
DISTRACTOR_MAX_PCT`. For an absolute tolerance near zero the band is in units of the
quantity's full sweep range instead, and is recorded so.

Candidates (all real simulation outputs or values interpolated from them):
- **output-type answers** (`infer_then_predict`, `timing`, `magnitude`): the answer's
  column in every sweep and refinement row that differs from the question's case in at
  most one swept axis; for `infer_then_predict` the hidden input is free, so B at other
  hidden-input values (a wrongly inferred input) are candidates;
- **input-type answers** (`inverse`, `threshold`, `optimisation`): the searched input at
  which a neighbouring case (one other swept axis changed) reproduces the question's
  observed value or limit, by linear interpolation along the searched axis
  (optimisation: its interpolated extremum), kept only inside the stated search interval.

A candidate is valid if it lies inside the band, has the true answer's sign and units,
is more than `tolerance_abs` from the true value and, once formatted, differs from the
other distractor by more than `tolerance_abs`. **Placement** (the controller's choice,
`--placement`): `above` (both distractors above the true value), `below` (both below) or
`straddle` (one on each side). Choose it per question so that the true value is not
simply the middle option, which a guessing model would pick; write the choice and its
reason in `design.md`. The script then picks two valid candidates with that placement,
preferring fewest changed axes, then distinct source cases; a random fallback keeps the
placement. Each distractor's source runs, column and changed axis are stored in
`mcq.json`.

**Fallback (seeded random).** For each missing distractor, draw a seeded value inside
the band on a random side, written in the option format, snapped to the stated time
step for a timing answer, inside the search interval for an input-type answer, and
valid as above (redraw otherwise, at most 100 tries; seed = `int(<first 8 hex digits
of the question SHA256>, 16) + 1000 + try number`). Record `source: "random"`, side, percentage and seed. This is
the only distractor not taken from a run (Section 2 rule 5). No model call is ever made
for a distractor. If no two valid
options exist, report `NO_VALID_OPTIONS`.

`mcq.json` = `{"question_sha256", "true_value", "options_values": {"true", "d1", "d2"},
"formatted": {...}, "band": {...}, "distractor_provenance": {"d1": {...}, "d2": {...}},
"shuffles": [{"k": 1, "letters": {"A": "true|d1|d2", "B": ..., "C": ..., "D": "none"}}, ...]}`.

---

## 10. Part B — Evaluation (`qa_eval.py`)

Starts only when `attempt_status.json` is `READY_FOR_EVAL` and the SHA256 of
`question.txt` equals the one in `verify.json` and `mcq.json`.

### B1 — No-tool shuffles
For k = 1 … `N_SHUFFLES`, one after the other: ONE fresh chat call to `NO_TOOL_MODEL`
with Prompt NO_TOOL (Section 13), the question and shuffle k's options, no `tools`
parameter, timeout `NO_TOOL_TIMEOUT_S`. The letter is read from the last
`FINAL ANSWER:` line. Outcome per shuffle: `correct` (true letter), `d1`, `d2`, `none`
(D), or `no_answer`. A reply without a parsable letter is retried once with a new
identical fresh call (technical failures are retried only inside `qa_llm.py`, 3.1); if
the retry, or a call that still fails technically, gives no letter, the outcome is
`no_answer`. A reply cut off by `max_tokens` (`finish_reason` "length", usually inside the
thinking) is retried with the larger cap `NO_TOOL_RETRY_MAX_TOKENS`; if the retry is cut
off too, the outcome is `cut_off`. Every outcome other than `correct` and `cut_off` is a
no-tool **failure** (a model that never finished its answer has not failed the question).
Written to `eval/no_tool_s<k>.json`: question SHA256, shuffle k, answer text,
`COMPUTED_VALUE` line if present, letter, outcome, and usage, actual and rate cost and
wall time summed over ALL calls made for the shuffle (retries included, each also listed).

**Gate:** no-tool failures < `NO_TOOL_FAIL_MIN` → attempt `TOO_EASY`, skip B2.

### B2 — Agent shuffles
For k = 1 … `N_SHUFFLES`, one after the other: the sandbox self-test (3.3), then ONE
fresh agent session (`qa_agent_loop.py --role agent`) with `TOOL_MODEL`, a new empty
workspace at `/work`, Prompt TOOL_AGENT, the question and shuffle k's options; caps
`AGENT_MAX_WALL_S`, `AGENT_MAX_TURNS`, `AGENT_MAX_GEOS_RUNS` (all three stated in the
prompt). The letter is read from the last `FINAL ANSWER:` line of its final message.
Outcomes as in B1; a session that ends at a cap without a letter is `no_answer`. A
session that fails for a technical reason before its first GEOS run is retried once in a
new empty workspace; a technical failure after that, or of the retry, is `no_answer`.
**GEOS is required** (`AGENT_REQUIRE_GEOS = on`): the right letter counts only when the
session actually ran GEOS, i.e. the counting wrapper recorded at least one GEOS launch or
one of its commands ran `geosx ... -i <input>`. A right letter without a GEOS run is
`correct_no_geos` (for example an answer from a hand-written solver). Every outcome other
than `correct` is an agent **failure**. Written to
`eval/agent_s<k>.json`: final message, `COMPUTED_VALUE` if present, letter, outcome,
turns, GEOS runs, GEOS commands (`geos_invocations`), `used_geos`, `cut_off` (a call
ended with `finish_reason` "length"), `forbidden_path_attempts`, wall time, and usage, actual and rate cost
summed over ALL sessions of the shuffle (from `agent_s<k>_usage.jsonl`). Then the
workspace and the full transcript are deleted.

Stop B2 as soon as agent failures reach `AGENT_STOP_FAILS`; the remaining shuffles are
recorded as `not_run`.

**Decision:** agent correct ≥ `AGENT_PASS_MIN` → attempt `KEPT`; otherwise
`AGENT_FAILED`.

### B3 — Records
`attempt_result.json`:

```json
{
  "attempt": 1, "status": "KEPT | TOO_EASY | AGENT_FAILED | GENERATION_FAILED | SANDBOX_FAILED",
  "reason": null,
  "question_type": "", "question_sha256": "", "true_value": null, "units": "",
  "options": { "true": "", "d1": "", "d2": "" }, "band": {},
  "no_tool": { "picks": ["d1", "d2", "correct", "none"], "failures": 3,
               "per_shuffle_cost_usd": [], "total_cost_usd": null },
  "agent":   { "picks": ["correct", "correct", "d1", "correct"], "correct": 3,
               "per_shuffle_cost_usd": [], "total_cost_usd": null,
               "per_shuffle_wall_s": [], "per_shuffle_geos_runs": [] },
  "generator": { "cost_usd": null, "sessions": null, "turns": null, "wall_s": null },
  "tokens": { "<role>": { "prompt": 0, "completion": 0, "reasoning": 0, "cached": 0 } },
  "cost_mismatch_calls": 0,
  "total_cost_usd": null,
  "models": { "GEN_MODEL": "", "NO_TOOL_MODEL": "", "TOOL_MODEL": "" }
}
```

`attempt_status.json` stages and statuses: `A0`…`A10` with `IN_PROGRESS`, `FROZEN`,
`READY_FOR_EVAL`, `GENERATION_FAILED`; then `B1`/`B2` with `IN_PROGRESS`; finally the
attempt status of `attempt_result.json`.

---

## 11. Costs

Costs are summed from the **actual cost** of each call (3.1); the rate cost is a
cross-check. `pricing.json` rows are keyed by the exact model id:
`{"input", "output", "cache_read", "cache_write"}` in USD per 1M tokens (optional
`long_context_threshold_prompt_tokens`, `long_context_threshold_basis`, `long_context`
as in earlier protocols). Rate cost = `(prompt − cached − cache_write) × input +
cached × cache_read + cache_write × cache_write_rate + completion × output`, per 1M.
Rows are experiment configuration; never change them. A missing row stops the launcher
at its startup checks (Section 1), before any model call.

Reported for every attempt and summed for the deck:

| Bucket | Content |
|---|---|
| generator | every call of every controller session of the attempt (attempt 1 includes exploration and baseline work) |
| no_tool | each shuffle's calls (retries included), and the total of the shuffles run |
| agent | each shuffle's session(s) (all turns, retries and interrupted sessions included), and the total of the sessions run |
| total | generator + no_tool + agent |

Each bucket also reports prompt, completion, reasoning and cached tokens. GEOS compute is
not API cost; it is reported as run counts and wall time.

---

## 12. Part C — Attempt loop, memory, deck result, resume

### C1 — Memory entry
After each attempt, `qa_eval.py` (or the launcher, for `GENERATION_FAILED` and
`SANDBOX_FAILED`) writes one entry headed `## Attempt <n>` to `deck_memory.md`, replacing
an existing entry with that heading (so a resume never duplicates it); compact, ≤ 25 lines:
attempt n, status, `QUESTION_TYPE`, observable, case, hidden input and interval (if
any), the A/B pair, true value and units, the three options with their sources, sweep
used (own or reused from attempt m, axes), no-tool picks per shuffle and the
`COMPUTED_VALUE`s it gave, agent picks, `COMPUTED_VALUE`s, turns and GEOS runs per
session, and for `GENERATION_FAILED` the reason. The next attempt's controller adds, at
the start of its own `design.md`, its reading of this entry (A4). `deck_memory.md` is
never deleted.

### C2 — Next attempt or finish
- `KEPT` → deck `KEPT`; no further attempts.
- `SANDBOX_FAILED` → deck `SANDBOX_FAILED`; no further attempts. After fixing the
  sandbox, the user starts a new `RUN_TAG`; the failed attempt is never re-evaluated.
- Otherwise, if n < `MAX_ATTEMPTS` → attempt n + 1 (fresh controller session).
- Otherwise → deck `NOT_KEPT`.

Then the launcher writes `results.md` (one row per attempt: type, observable, status,
no-tool picks, agent picks, costs) and `deck_result.json`:

```json
{
  "status": "KEPT | NOT_KEPT | DECK_UNSUITABLE | DECK_BASELINE_FAILED | SANDBOX_FAILED",
  "reason": null, "PROTOCOL_ID": "AGENT", "PROTOCOL_VERSION": 1,
  "RUN_TAG": "", "deck_slug": "", "question_type": "",
  "attempts_run": 0, "kept_attempt": null,
  "attempts": [ { "attempt": 1, "status": "", "no_tool_failures": 0, "agent_correct": 0 } ],
  "simulations": { "baseline": 1, "per_attempt": [] },
  "costs": { "generator_usd": null, "no_tool_usd": null, "agent_usd": null, "total_usd": null,
             "per_attempt": [] },
  "tokens": {},
  "models": { "GEN_MODEL": "", "NO_TOOL_MODEL": "", "TOOL_MODEL": "" }
}
```

### C3 — Cleanup (after the deck result)
Keep: `run_config.json`, `deck_memory.md`, `deck_result.json`, `results.md`, `source/`,
`exploration/deck_summary.md`, `exploration/output_manifest.json`,
`exploration/baseline/fixes.md` (if any), and per attempt: `design.md`,
`attempt_status.json` (with each controller session's last reply), `attempt_result.json`,
`controller/usage.jsonl`, `controller/transcript_<session>.jsonl`, `question/`,
`eval/`, and in `sweep/`: `sweep_config.json`, `template.xml`, `extract_table.py`,
`sweep_table.csv`, `failed_runs.csv`, `sweep_readme.md`, `reuse.json`, and each
refinement and extra run's `observables.json`. Delete everything else (per-run inputs, logs,
`output/` folders, `*.hdf5`, agent workspaces and transcripts, `__pycache__`),
then empty folders. Print the deck folder size.

### C4 — Resume
`qa_run_deck.py` can be rerun at any time and continues where it stopped:
- `deck_result.json` exists → do nothing;
- an attempt with an `attempt_result.json` is finished (whatever its status, including
  `GENERATION_FAILED`); never rerun it;
- an attempt `READY_FOR_EVAL` or in `B1`/`B2` → resume Part B; finished shuffles
  (`no_tool_s<k>.json`, `agent_s<k>.json`) are never rerun. An interrupted shuffle
  (usage file but no result file) is started again as a fresh call or session, after
  its leftover workspace is emptied; the cost already spent stays in its usage records
  and is included in that shuffle's cost;
- an attempt in `A*` → a new controller session continues it from the first unfinished
  step (A0.3): a successful baseline, a completed sweep table (`sweep_readme.md`
  confirms), a finished refinement and a frozen question are never redone;
- `run_config.json` with different models, `QUESTION_TYPE` or protocol version → stop;
  the user must choose a new `RUN_TAG`;
- exit code 75 (usage limit) → stop cleanly; the next launch resumes.

Other technical errors: diagnose, repair within the rules, record, continue. Never weaken
a rule, edit a frozen question or fabricate a result to get past an error.

---

## 13. Prompts (the scripts read these blocks verbatim; keep the headings and fences)

Placeholders in braces are filled by the scripts.

### Prompt: NO_TOOL

```text
You are the no-tool solver in a scientific reasoning evaluation. Answer using ONLY your internal knowledge and the information written in this prompt. You have no tools: do not try to read files, run programs or search the web. Do not refuse because tools are unavailable.

Below is one multiple-choice question. It gives the physical setup, the parameter values and the models chosen; it does not write out every equation inside those models. Use your own scientific knowledge for anything it leaves out.

First work out your own answer from the physics, showing the key steps (the physical mechanism, any estimate or formula you use). Then compare your result with the options. Exactly one of the options is the correct answer. Options A, B and C are numerical values; option D, "None of them", means that none of A, B and C is the correct value. Choose the single option you judge to be correct.

End your response with these two lines:
COMPUTED_VALUE: <your own computed number> {UNITS}
FINAL ANSWER: <one letter A, B, C or D>

Question:
{QUESTION}

Options:
{OPTIONS}
```

### Prompt: TOOL_AGENT

```text
You are a scientific problem solver with a Linux shell. You have tools: bash, read_file and write_file. Your working directory is /work; it is empty and it is the only place you can write.

Available to you, read-only:
- the GEOS multiphysics simulator, run as `geosx -i <input.xml> -o <output_dir>` (put inputs and outputs in your working directory); MPI and Python (with numpy, h5py and the usual scientific packages) are available;
- the GEOS source code, documentation and example input decks under {GEOS_ROOT} (for example {GEOS_ROOT}/inputFiles and {GEOS_ROOT}/src/docs).

GEOS is installed for this task and is the expected way to answer: the value you report must come from the output of a GEOS simulation that you ran. Do not compute it with your own solver, a hand formula or an estimate; you may use scripts only to prepare GEOS inputs and to read GEOS outputs. An answer that does not come from a GEOS run does not count, even if its letter is right. Beyond that you may do anything that helps you answer: read the source and the examples, build and run simulations, write and run scripts, analyse outputs. Your limits: at most {MAX_GEOS_RUNS} GEOS runs in total, {MAX_TURNS} model turns (each tool call is one turn) and about {WALL_HOURS} hours. Commands run on {CORES} CPU cores. You will be warned when a limit is close; give your final answer before it is reached.

Below is one multiple-choice question. Exactly one of the options is the correct answer. Options A, B and C are numerical values; option D, "None of them", means that none of A, B and C is the correct value. Determine the correct option. Base your choice on your own determination of the requested quantity, not on the options.

When you are done, reply without a tool call, with a short summary of how you obtained your answer, and end with these three lines:
GEOS_RUN: <the geosx command whose output gave your value, and the output file you read it from>
COMPUTED_VALUE: <your own determined number> {UNITS}
FINAL ANSWER: <one letter A, B, C or D>

Question:
{QUESTION}

Options:
{OPTIONS}
```

`{OPTIONS}` is four lines `A. <value> {UNITS}`, `B. …`, `C. …`, `D. None of them`, in
shuffle k's order. `{UNITS}` comes from `answer_key.json`.

### Prompt: CONTROLLER_SYSTEM

```text
You are the controller of a GEOS benchmark experiment. You have tools: bash, read_file and write_file. Commands run in the geos conda environment; you can write only under {BENCH_DIR}. Your working directory {GEN_MEMORY_DIR} is your persistent memory folder across all questions: keep any memory files (such as .nemonist/) there and nowhere else. Write all experiment files under the deck folder you are given, using absolute paths. Follow the protocol file you are given exactly. For the helper scripts use the commands of its Section 3.5 as given; do not read their code unless a command fails. Keep every command's printed output to about 50 lines. Never print a whole file, log, CSV or HDF5 dump. Wait for long jobs inside one command instead of polling with many tool calls. When your part is done, reply without a tool call with a one-paragraph status.
```

---

## Appendix — How the user launches this

Store the key once outside the repo (`~/.config/nemonist/env`, `chmod 600`, sourced from
`~/.bashrc`); it is never written in a script.

```bash
export GEOS_ROOT=/home/domain-adapt/codes/GEOS
export BENCH_DIR=$GEOS_ROOT/geos_sweep_bench
export PROTOCOL_FILE=$GEOS_ROOT/GEOS_Agent/geos_sweep_bench/GEOS_SWEEP_BENCH_AGENT_v1.md
export GEOS_BIN=$GEOS_ROOT/build-conda-geos-release/bin/geosx
export AGENT_RO_BINDS=/home/domain-adapt/codes/thirdPartyLibs
export PARALLEL_JOBS=12
export INPUT_XML=inputFiles/wellbore/DeviatedElasticWellbore_smoke.xml
export RUN_TAG=ag1_run1
export QUESTION_TYPE=infer_then_predict
export NEMONIST_BASE_URL=https://nemonist.com/api/v1
export MODEL=anthropic/claude-sonnet-5.5
export REASONING_MAX_TOKENS=16000     # thinking budget per call (0 = off)
source ~/.config/nemonist/env         # sets NEMONIST_API_KEY

cd "$GEOS_ROOT"
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate geos
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$BENCH_DIR/_global/controller_logs"
tmux new -s ag1 "python $BENCH_DIR/_global/scripts/qa/qa_run_deck.py 2>&1 | tee $BENCH_DIR/_global/controller_logs/qa_$(date +%Y%m%d_%H%M%S).log"
```
