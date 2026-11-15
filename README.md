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
    tests/                       offline mock of the gateway (no API key used)
  _global/scripts/check_mesh_description.py   mesh-description helper used by the controller
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

## Known issue: prompt caching on the Google Vertex route

The generator is a long multi-turn session, with prompts up to ~55k tokens. Without prompt caching, it
pays the full input price for the whole history on every call. In `example_run/ag1_run5` (Nemonist, served
by Google), only **781 tokens** were read from cache on nearly every one of 44 calls, and the attempt was
stopped because the cost kept climbing.

The harness marks the system prompt and the last two user/tool messages with `cache_control`, and earlier
thinking is never sent back.

Tests (6-call growing conversation; all results in `caching_tests/results/`):

| | Google via Nemonist | Google via OpenRouter | AWS via OpenRouter |
|---|---|---|---|
| no tools | 91% cached | 82% cached | 100% |
| with tools | 66% (calls 2–4 miss) | 69% (calls 2–4 miss) | 100% |

The misses follow the provider (Google Vertex), not the gateway. AWS/Anthropic cached every call.

### Reproducing it (any machine; Python 3 only, no GEOS)

`caching_tests/caching_check.py` (no tools) and `caching_tests/caching_tools_check.py` (with tools, like the
agent): put your keys in the two strings at the top (`NEMONIST_KEY = ""`, `OPENROUTER_KEY = ""`), then:

```
python3 caching_tests/caching_tools_check.py --gateway nemonist
python3 caching_tests/caching_tools_check.py --gateway openrouter
python3 caching_tests/caching_tools_check.py --gateway openrouter --provider google-vertex
python3 caching_tests/caching_tools_check.py --gateway nemonist --provider anthropic
```

Each run costs about $0.10–0.20. `--model` changes the model, and `--provider` pins one provider with no
fallbacks. The scripts make no tool calls: every tool request is answered "(not executed in this test)".

`caching_tests/caching_multiturn_test.py` is the same test with more switches (`--tools`, `--thinking`,
`--markers`, `--mode same|grow`, `--calls`, `--dry-run`). It reads the keys from `NEMONIST_API_KEY` /
`OPENROUTER_API_KEY`. Use `--dry-run` to print the exact request bodies without calling the API.

## Other notes

- **Thinking:** with Claude via Nemonist, thinking works only as `reasoning: {"max_tokens": N}`
  (`reasoning_effort` is ignored). The harness sends `REASONING_MAX_TOKENS` (16000), with
  `max_tokens = REASONING_MAX_TOKENS + MAX_ANSWER_TOKENS`. Signed thinking blocks of earlier turns are not sent
  back, because they failed verification ("Invalid signature").
- **Nemonist memory:** when tools are present, Nemonist adds ~1,200 hidden prompt tokens that tell the model to
  read/write `.nemonist/key` and `.nemonist/config`. The generator's working folder
  (`bench/_global/generator_memory/`) persists across attempts, while each agent session gets a fresh, empty
  `/work` that is deleted afterwards. Caching tests show this injected text is not the cause of the cache misses.
- **Sandbox:** the agent's root is a tmpfs. System folders are read-only, `$HOME` is hidden except `GEOS_ROOT`,
  conda and `AGENT_RO_BINDS`, and `BENCH_DIR` plus every untracked GEOS path (except the build) is hidden.
  `/tmp` is private (MPI needs it). The agent cap on GEOS runs is soft (a counting wrapper).
- **Offline test:** `bench/_global/scripts/qa/tests/` contains a mock gateway. Set `QA_LLM_MOCK=<tests>/mock_llm.py`
  (plus `MOCK_*` variables, see `mock_llm.py`) and no network call is made.
