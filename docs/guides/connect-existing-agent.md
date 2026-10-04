# Connect an existing agent

Use this guide with a coding agent in your business project. Fuju RSI keeps the
evaluation tools in a development environment and calls your existing application
through a test adapter.

## 1. Get the tools and install the Skill

Clone [the repository](https://github.com/vibeinging/fuju-rsi), create a development
virtual environment, and install the package from source as shown in the
[README](../../README.md#try-it-locally--no-model-credentials).
The package is not yet published to PyPI.

From the cloned repository, choose the host:

```bash
python skills/fuju-tune/scripts/install.py --host codex --project /path/to/business-project
```

Use `--host claude` for Claude Code or `--host both` for both. Omitting `--project`
installs the Skill for your user. Project destinations are
`.agents/skills/fuju-tune` for Codex and `.claude/skills/fuju-tune` for Claude Code;
user destinations use the corresponding folders under your home directory.

The installer copies resources; it does not install the Python package or bind
your application. An identical installation is left unchanged. A modified
installation is refused rather than overwritten. Start a new host chat or use
its Skill reload mechanism if needed, then explicitly select `$fuju-tune` or
`/fuju-tune` where supported. Actual host discovery needs checking in your environment.

## 2. Ask for the original-version report

Open your business project and ask:

> Use Fuju RSI to find this agent's real entry point and existing tests. Connect it in a development environment, run the original version, and give me a baseline report. List missing access, connections, or trusted expectations before running dependent steps.

The coding agent should identify:

- The real function, command, or test endpoint, and how to isolate a test session.
- The current prompt/configuration actually used by that entry point.
- Existing cases and independently supported expected results.
- Complete output and final answer access, plus a scorer for your business contract.

Keep the original application environment available; the adapter can call it from
another environment or language. Provide normal test access when required. Missing
expected answers are setup gaps; the tested agent's own answer cannot supply them.

## 3. Inspect and prepare the connection

The installed CLI provides these stages; the coding agent normally runs them:

```bash
python -m fuju_rsi connect inspect --project /path/to/business-project
python -m fuju_rsi connect init --project /path/to/business-project --kind ask-data \
  --benchmark /path/to/frozen-development-bundle \
  --validation-ledger /path/to/shared-validation-ledger
```

`inspect` lists filename clues without executing project code. `init` saves the
connection and creates an unfinished adapter under `.fuju-rsi/connection/`.
The coding agent must bind its runner, original configuration, cases, and scoring
to your real program. Factory loading must be free of runner/model side effects.

For Ask Data, use a reviewed and frozen development casebook and an existing
shared validation ledger. Their preparation is described in the
[Ask Data guide](../../skills/fuju-tune/scenarios/ask-data/guide.md). Preserve that
ledger across experiments. Independent holdout material belongs in the verifier
environment. For other tasks, `--kind custom` provides the generic adapter contract;
it does not load an external scenario code plugin.

An existing adapter can be reused with `--agent module:factory --adapter-dir tests`.
Paths in this example are placeholders; use your actual module and test directory.

## 4. Check, then run

```bash
python -m fuju_rsi connect check --project /path/to/business-project
python -m fuju_rsi connect baseline --project /path/to/business-project --max-calls 10
```

`check` loads and validates the factory and development materials without calling
the runner or proposer. `ready` means the connection can load; it still reports
`executionChecked=false` and the planned runner count.

Choose the callback budget after checking that count. `--max-calls` limits runner
and proposer callbacks, not model requests inside a callback or monetary spending.
Set any model/service spending controls through your existing application.

`baseline` runs only the original prompt in a fresh experiment directory and reuses
the shared ledger. It does not propose changes, execute holdout, or deploy anything.
Wrong answers can appear in a completed run; check passed counts, runtime errors,
and completeness separately. Reports remain under `.fuju-rsi/baselines/`.

## 5. Review before improving

Read the Markdown report and JSON summary, confirm the original runtime and scoring,
then choose a bounded change. Prompt comparisons and configuration file comparisons
have different contracts. Follow [file candidates](../../skills/fuju-tune/references/file-candidates.md)
or [verification](../../skills/fuju-tune/references/verification.md) as appropriate.

Local scores are development evidence. Deliver ordinary prompts/configuration/code
through the application's usual review, release, and restoration process.
Removing RSI should leave the application able to run.

If you get stuck, [report the setup problem](https://github.com/vibeinging/fuju-rsi/issues/new/choose)
with redacted steps or public/synthetic examples. Include the commit, host, Python
version, and the observed state; leave out credentials and private casebooks/results.
