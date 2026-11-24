# Moumou Agent

[简体中文](README.md)

Moumou Agent is a Python system for teams of agents. You provide a task and its delivery requirements. A lead assigns work, and each sub-agent works in its own directory. The framework schedules independent tasks in parallel, records human decisions, and checks the exact versions handed over for delivery.

## Quick start

Use Python 3.11 or later. Activate your Python environment and run this command from the repository root.

```bash
python -m pip install -e .
```

Copy `.env.example` to `.env` and set `DEEPSEEK_API_KEY_OFFICIAL`. The default model is DeepSeek's official `deepseek-flash`. To use HKGAI, configure its key and pass `--llm-profile hkgai`. Web search is optional.

Follow the [user guide](docs/SETUP.en.md) to create your own Markdown task directory. These commands save the session in a fresh output directory.

```bash
python -m moumou --tasks runs/my-input --plan
python -m moumou --tasks runs/my-input --human console --out runs/my-run
```

Answer questions and make approval and review decisions in the terminal. Open `runs/my-run/report.md` for the run status and delivery list. The framework creates the agents' workspaces inside the output directory.

## System components

| Component | Responsibility |
| --- | --- |
| Orchestration | Propose assignments, schedule dependencies, and recover blocked tasks |
| Context and memory | Keep confirmed rules, input versions, and recent results within the request budget |
| Tools and workspaces | Scope file access to each role and request permission for controlled actions |
| Versions and acceptance | Save declared deliveries and check dependencies, tests, and final files |
| Models and records | Call model and search services and record usage, human decisions, and run evidence |

[User guide](docs/SETUP.en.md) · [Architecture](docs/ARCHITECTURE.en.md) · [Changelog](CHANGELOG.en.md)
