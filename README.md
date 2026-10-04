# Fuju RSI

**Test and improve existing AI agents from Codex or Claude Code.**

Connect your agent's real entry point, reproduce failures, compare a scoped change,
and get a report with results and regressions. Start with **text-to-SQL / Ask Data**.
Keep using your existing application and deployment process.

English · [简体中文](README.zh-CN.md) · [Get help](https://github.com/vibeinging/fuju-rsi/issues) · [Contribute](CONTRIBUTING.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](pyproject.toml)

## Start with one request

Open your business project in Codex or Claude Code and paste:

> Follow https://github.com/vibeinging/fuju-rsi to connect Fuju RSI to this project. Find the agent's real entry point and existing tests, run the original version, and give me a baseline report. Tell me which connections or expected answers are missing.

The coding agent follows the Skill to install the tools in a development environment,
bind an adapter to your existing function, command, or test endpoint, and run it.
You supply the required test access and trusted expectations. The connection command
creates a skeleton and checks it; the coding agent completes the binding from your code.

Your first result is a **baseline report**, not an automatically deployed change.
After installing the Skill, select `$fuju-tune` in Codex or `/fuju-tune` in Claude Code
when the host supports that command. See [installation and first connection](docs/guides/connect-existing-agent.md).

## Try it locally — no model credentials

Requires Python 3.8+ and Git. These shell commands work on macOS/Linux:

```bash
git clone https://github.com/vibeinging/fuju-rsi.git
cd fuju-rsi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/try_ask_data.py
```

On Windows, activate `.venv\Scripts\Activate.ps1` in PowerShell.
Use your installed Python command in place of `python3`.

The demo actually executes SQLite. A known mistake counts orders instead of summing
amounts; a fixed rule proposes the correction. Each run creates a fresh directory and
prints the report path. The demo itself uses no network, model, server, or Trace DB.

| Plan | Training cases passed | Validation cases passed | Runtime errors |
| --- | ---: | ---: | ---: |
| Original | 0 / 1 | 0 / 1 | 0 |
| Demo candidate | 1 / 1 | 1 / 1 | 0 |

This is a reproducible **synthetic protocol demo**, not a customer accuracy benchmark.
It uses five runner/proposer callbacks, no independent holdout, and remains
`adoptable=false`. [What the report proves](docs/guides/offline-demo.md).

## Install the Skill into your project

From this cloned repository, choose the host and your existing project directory:

```bash
python skills/fuju-tune/scripts/install.py --host codex --project /path/to/your-project
# Claude Code: use --host claude. Both hosts: use --host both.
```

The installer copies the complete Skill to the host's standard project directory.
It preserves modified installations. The Python package and Skill are installed
separately; keep the package in your development/test environment. Your production
application runs without RSI. [Installation options](docs/guides/connect-existing-agent.md#1-get-the-tools-and-install-the-skill).

## What you get

- **First run:** a Markdown report, a JSON summary, and a list of missing bindings when setup is incomplete.
- **Prompt comparison:** original and candidate text, a diff, development results, and a separate independent verification path.
- **Configuration comparison:** scoped file changes, paired reruns, hashes of the files actually read, and original files for restoration. This currently supplies development evidence only.

Use existing Python functions, CLI commands, or test HTTP endpoints. The tested
application can be written in another language. The Skill is named `fuju-tune`;
the Python package and CLI are `fuju-rsi` (`import fuju_rsi`).

## Current scope

This is a **source preview**; no Fuju RSI package has been published to PyPI.
Install from this checkout rather than assuming `pip install fuju-rsi` is available.

Local tests cover clean package installation, copied Skills, external-project
baseline runs, and an application that continues running after removing RSI.
Codex/Claude host discovery and a real customer accuracy improvement still need
verification. Prompt search and configuration comparison are available; automatic
code optimization and external scenario code plugins are not implemented.

Development scores do not qualify a change for adoption. Keep independent cases
in a separately managed environment and deploy ordinary changes through your
application's existing review and release process.

## Guides and feedback

| Goal | Guide |
| --- | --- |
| Connect an existing agent | [First connection](docs/guides/connect-existing-agent.md) |
| Prepare trusted Ask Data cases | [Ask Data guide](skills/fuju-tune/scenarios/ask-data/guide.md) |
| Choose between metrics, dictionaries, and project rules | [Change targets](skills/fuju-tune/scenarios/ask-data/targets.md) |
| Compare configuration files | [File candidates](skills/fuju-tune/references/file-candidates.md) |
| Verify a frozen candidate independently | [Verification](skills/fuju-tune/references/verification.md) |
| Report setup problems or propose an adapter | [Issues](https://github.com/vibeinging/fuju-rsi/issues/new/choose) |
| Add another scenario | [Contributing](CONTRIBUTING.md) |

Ask Data is the integrated domain scenario. Community proposals can add other tasks
with runnable examples and trusted scoring rules. Share public or synthetic examples
when filing an issue, with credentials and private business data removed.

[Fuju Trace](https://github.com/vibeinging/fuju-trace) is optional diagnostics;
[Fuju / legacy yiTrace](https://github.com/vibeinging/fuju) contains the TraceDB engine
and compatibility packages. Neither is required for the demo or default offline evaluation.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 skills/fuju-tune/scripts/scenarios.py check
```

After console changes, build `console/` and run `python scripts/sync_console.py`.
Build a wheel with `python -m build`; check it with
`python scripts/verify_python_consumer.py /path/to/fuju_rsi.whl`.
See [repository conventions](AGENTS.md). MIT licensed.
