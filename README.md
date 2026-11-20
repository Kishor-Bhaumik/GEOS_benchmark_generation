# GEOS benchmark generation: no-tool vs tool-agent MCQ (AGENT v1)

For one GEOS input deck, this pipeline produces **one hard multiple-choice question** that a model **without
tools gets wrong** but a **tool agent with GEOS access gets right**. Every model call goes through the
Nemonist OpenAI-compatible gateway, and the real cost of each role is recorded.

The full specification is in [`GEOS_SWEEP_BENCH_AGENT_v1.md`](GEOS_SWEEP_BENCH_AGENT_v1.md). The harness reads
its prompts from that file, so it is part of the code.

## How one attempt works

1. **Generator (Part A):** a controller model explores the deck in a sandbox, runs a GEOS parameter sweep,
   writes one question (default type `infer_then_predict`), refines and verifies the true value, then freezes it.
2. **MCQ:** options are the true value, two distractors taken from the sweep, and D "None of them" (never correct).
3. **No-tool gate:** the same model answers 4 shuffles of the options, one fresh call each. It goes on only if
   the model is wrong at least 3 times (otherwise `TOO_EASY`).
4. **Tool agent:** the agent runs in a bubblewrap sandbox. It sees the GEOS repo (including `inputFiles/`) and
   the GEOS binary, but never this bench. It answers the same 4 shuffles, one fresh session each, and stops
   after 2 failures. At least 3 right means `KEPT`; otherwise `AGENT_FAILED`.
5. There are up to `MAX_ATTEMPTS` attempts per deck, with one memory entry per attempt (`deck_memory.md`).

The same model is used for every role: `anthropic/claude-sonnet-5.5`.

## Repository layout

```
README.md
GEOS_SWEEP_BENCH_AGENT_v1.md     protocol = single source of truth (prompts are read from it)
run_agent_deck.sh                launcher (edit the export lines, or set them as env variables)
check_api_cost.py                one API call, prints the gateway's usage/cost fields
requirements.txt
bench/                           BENCH_DIR (default): harness + results
  _global/pricing.json           $/token rates (cross-check of the gateway's cost)
  _global/scripts/qa/            the harness (Python, standard library only)
    qa_run_deck.py               launcher: config checks, attempt loop, finish/cleanup
    qa_agent_loop.py             tool loop for controller and agent (bash / read_file / write_file)
    qa_llm.py                    one API call: cache markers, thinking, retries, cost record, budget stop
    qa_sandbox.py                bwrap sandboxes for both roles + self-tests
    qa_verify.py                 checks + freeze of the question
    qa_build_mcq.py              distractors, options, shuffles
    qa_eval.py                   no-tool and agent evaluation, scoring, memory entry
    qa_sweep.py                  reads sweep / refinement points
    qa_common.py                 config, prompt blocks from the protocol, pricing, spend ledger
    qa_screen_infer.py           screens the sweep table for infer_then_predict (A, B) pairs in one command
    tests/                       offline mock of the gateway (no API key used)
  _global/scripts/check_mesh_description.py   mesh-description helper used by the controller
hard_xml_list.txt                coupled-multiphysics decks (INPUT_XML candidates)
caching_tests/                   standalone prompt-caching tests (no GEOS needed), see below
example_run/ag1_run5/            a real (stopped) run: transcript + per-call usage showing the caching problem
```

Results are written to `bench/decks/<deck>__<hash>/<RUN_TAG>/` (git-ignored).

## Requirements

- Linux, with **bubblewrap** (`bwrap`), `tmux`, `git` and Python 3.8+ (we use a conda env named `geos`).
- A **GEOS build** (`geosx`) and the GEOS **source checkout** it was built from. We use GEOS `develop` at commit
  `04701ec63d`. The agent sees the GEOS checkout, so it must be a git repo; untracked and ignored files are
  hidden from the agent, except the build folder.
- `numpy` and `h5py` (only for `check_mesh_description.py`).
- A Nemonist API key in a file, by default `~/.config/nemonist/env`:
  ```
  export NEMONIST_API_KEY=...
  ```
  The key is never printed or written by the harness, and it is not passed into either sandbox.

## Running

1. Edit the `export` lines at the top of `run_agent_deck.sh`, or set them before calling it:

   | Variable | Meaning | Default |
   |---|---|---|
   | `GEOS_ROOT` | GEOS source checkout | `~/codes/GEOS` |
   | `GEOS_BIN` | `geosx` binary | `$GEOS_ROOT/build-conda-geos-release/bin/geosx` |
   | `AGENT_RO_BINDS` | extra read-only folders GEOS needs at run time (e.g. the thirdPartyLibs install), `:`-separated | empty |
   | `INPUT_XML` | deck, relative to `GEOS_ROOT` | `inputFiles/wellbore/DeviatedElasticWellbore_smoke.xml` |
   | `RUN_TAG` | name of this run (reuse it to resume) | `run1` |
   | `MODEL` | model for all roles | `anthropic/claude-sonnet-5.5` |
   | `MAX_ATTEMPTS` | attempts per deck | `1` |
   | `CONDA_ENV` | conda env to activate | `geos` |

   Other settings (limits, budget, thinking tokens, etc.) are in `DEFAULTS` in `qa_common.py`, and each can be
   overridden with an environment variable of the same name.

2. **Free check first** (config, pricing row, both sandbox self-tests; no model call):
   ```
   AGENT_RO_BINDS=/path/to/thirdPartyLibs bash run_agent_deck.sh --check
   ```
   It should end with `startup checks passed (no model call made)`.

3. **Real run** (costs money):
   ```
   bash run_agent_deck.sh             # opens tmux session "ag1"  (Ctrl-b d to detach, tmux attach -t ag1)
   bash run_agent_deck.sh --inside    # if you are already inside tmux
   ```
   The log goes to `bench/_global/controller_logs/`. Rerunning with the same `RUN_TAG` resumes.

### Cost guards

- **Daily budget:** the harness stops itself when today's spend reaches `DAILY_BUDGET_USD` (default $8). It
  exits with code 75 and can be resumed the next day. The ledger is `bench/_global/spend_ledger.jsonl`, kept
  per `BENCH_DIR`.
- The real cost of each call is `usage.cost_details.upstream_inference_cost` (Nemonist BYOK), and it is
  recorded per call in `usage.jsonl`.
- Expected calls per attempt: generator ~100–200, no-tool 4, agent ~30–80 per shuffle.

## Status and recent changes

- **First fair KEPT question (2026-11-20, `hydraulicFracturing/walshQuarterNoChombo_smoke.xml`, attempt 2,
  $7.88 over two days):** the no-tool model was wrong 4/4 (no cut-off), and the agent right 4/4 **with GEOS**
  (6–29 GEOS runs per shuffle). The generator refined the mesh to 10×30×31 cells. Attempt 1 had failed
  because the question left out the matrix porosity and permeability; attempt 2 read the agent's answers and
  fixed it.
- **GEOS is required** (`AGENT_REQUIRE_GEOS=on`, advisor's rule): a right letter counts only if the session ran
  GEOS (a wrapper-counted launch or a `geosx ... -i` command); otherwise the outcome is `correct_no_geos`. The
  agent prompt says the value must come from a GEOS run and asks for a `GEOS_RUN:` line.
- **Deck choice:** `hard_xml_list.txt` lists 105 coupled-multiphysics decks (poromechanics, fractures, thermal,
  hydraulic fracturing, induced seismicity) that run in ≤ 30 s. Protocol A4: an internal mesh is refined so
  that a hand-written solver is impractical, while one GEOS run stays ≤ 3 minutes.
- **Hard token cap:** `reasoning.max_tokens` is only a target; the top-level `max_tokens` is the hard cap.
  `finish_reason` is recorded per call. A no-tool reply cut off by `max_tokens` is retried with
  `NO_TOOL_RETRY_MAX_TOKENS` (40000); if it is still cut off, the outcome is `cut_off`, which is not counted
  as a no-tool failure.
- **Gateway limit:** Nemonist refuses a call (HTTP 402) when its worst-case cost (prompt + `max_tokens`) exceeds
  what is left of the key's limit. The harness saves state and stops; rerun the launcher to resume.
  `DAILY_BUDGET_USD=0` disables the harness's own daily stop.
- **First KEPT question (2026-11-17, `ag1_run8`, $1.60 in total):** the no-tool model was wrong 4/4 and the agent
  right 4/4. Note: the agent did not run GEOS. It rebuilt the fully specified 75-node mesh and solved it with its
  own Python finite-element code.
- **Thinking cap:** `REASONING_MAX_TOKENS=2000` cut the generator cost from $4.06 to $1.16 on the same deck.
  The cap applies to every role. `reasoning.effort` is only a hint (Sonnet 5.5 does not think at "low"), so
  `max_tokens` is used as the hard cap (`caching_tests/effort_check.py`).
- **Protocol Section 3.5:** copy-paste commands for every helper script, so the generator does not read the
  scripts' code. `qa_screen_infer.py` screens all (A, B) pairs of the sweep table at once.
- **MCQ:** `qa_build_mcq.py --placement above|below|straddle` (the controller's choice), so the true value is
  not always the middle option. The four shuffles are always distinct.
- **Next attempt after TOO_EASY:** the controller reads the no-tool answers themselves (Section 3.5, A4).
- **Streaming:** API calls stream by default (`QA_STREAM=1`). `STREAM_IDLE_S` (120) seconds without data is
  treated as a dead connection and retried. Before this, a reply lost in transit made the client wait for the
  whole 30-minute timeout.
- **Fallbacks:** a cache-related HTTP 400 disables cache markers only for that call. Repair errors are saved in
  `usage.jsonl` as `repair_errors`.

## Prompt caching

The generator is a long multi-turn session, with prompts up to ~55k tokens, so it depends on prompt caching.
In `example_run/ag1_run5` (Nemonist, served by Google Vertex), only **781 tokens** were read from cache on nearly
every one of 44 calls.

**Cause (found by Nemonist, fixed 2026-11-16):** when the model looked for a missing `.nemonist/key`,
Nemonist added a note to the system prompt that carried a new key on every request. The system prompt
therefore changed on every call, and nothing after it could be cached. Now the note stays the same within a
conversation: it changes once (call 1 to call 2) and is then identical.

**Still open:** Google Vertex sometimes doesn't reuse a cache entry written a second earlier. This is seen
on OpenRouter pinned to `google-vertex` too, so it can't be fixed by the gateway. Nemonist serves Claude Sonnet
only through Vertex (zero-data-retention account). AWS/Anthropic (e.g. OpenRouter default routing) cached
every call in our tests.

**"Found 5" (fixed by Nemonist, 2026-11-16):** Nemonist kept old cache marks from its own copy of earlier
messages, so a request could reach the provider with more than 4 blocks, even with none from us. Nemonist
also adds one mark when a Claude request has none. Evidence and tests: `caching_tests/results/evidence_found5.txt`,
`markers_check.py`, `markers_repro.py`.

**Cache markers:** since 2026-11-16, the harness marks only the **system prompt and the newest message** with
`cache_control`; before, it marked the last two user/tool messages. In Nemonist's tests, this gave fewer
misses on Vertex. Earlier thinking is never sent back.

### Reproducing it (any machine; Python 3 only, no GEOS)

`caching_tests/caching_check.py` (no tools) and `caching_tests/caching_tools_check.py` (with tools, like the
agent): put your keys in the two strings at the top (`NEMONIST_KEY = ""`, `OPENROUTER_KEY = ""`), then:

```
python3 caching_tests/caching_tools_check.py --gateway nemonist
python3 caching_tests/caching_tools_check.py --gateway openrouter
python3 caching_tests/caching_tools_check.py --gateway openrouter --provider google-vertex
python3 caching_tests/caching_tools_check.py --gateway nemonist --old-markers    # marker placement before 2026-11-16
```

Each run costs about $0.10–0.20. `--model` changes the model, and `--provider` pins one provider with no
fallbacks. The scripts make no tool calls: every tool request is answered "(not executed in this test)".

`caching_tests/caching_multiturn_test.py` is the same test with more switches (`--tools`, `--thinking`,
`--markers harness|two|last|none`, `--mode same|grow`, `--calls`, `--dry-run`). It reads the keys from `NEMONIST_API_KEY` /
`OPENROUTER_API_KEY`. Use `--dry-run` to print the exact request bodies without calling the API.

## Other notes

- **Thinking:** with Claude via Nemonist, thinking works only as `reasoning: {"max_tokens": N}`
  (`reasoning_effort` is ignored). The harness sends `REASONING_MAX_TOKENS` (16000), with
  `max_tokens = REASONING_MAX_TOKENS + MAX_ANSWER_TOKENS`. Signed thinking blocks of earlier turns are not sent
  back, because they failed verification ("Invalid signature").
- **Nemonist memory:** when tools are present, Nemonist adds ~1,200 hidden prompt tokens that tell the model to
  read/write `.nemonist/key` and `.nemonist/config`. The generator's working folder
  (`bench/_global/generator_memory/`) persists across attempts, while each agent session gets a fresh, empty
  `/work` that is deleted afterwards. Once the generator creates `.nemonist/key` in its folder (following
  the note), the note no longer appears for it.
- **Sandbox:** the agent's root is a tmpfs. System folders are read-only, `$HOME` is hidden except `GEOS_ROOT`,
  conda and `AGENT_RO_BINDS`, and `BENCH_DIR` plus every untracked GEOS path (except the build) is hidden.
  `/tmp` is private (MPI needs it). The agent cap on GEOS runs is soft (a counting wrapper).
- **Offline test:** `bench/_global/scripts/qa/tests/` contains a mock gateway. Set `QA_LLM_MOCK=<tests>/mock_llm.py`
  (plus `MOCK_*` variables, see `mock_llm.py`) and no network call is made.
