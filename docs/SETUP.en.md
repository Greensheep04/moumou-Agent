# Moumou Agent Command-Line Guide

[简体中文](SETUP.md) · [Architecture](ARCHITECTURE.en.md)

## 1. Install and configure

Use Python 3.11 or later. Run the commands below from the repository root.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

In Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` and copy the settings with `Copy-Item .env.example .env`. You can also activate an existing Python environment.

Open `.env`. For the default official DeepSeek API, set `DEEPSEEK_API_KEY_OFFICIAL`. For HKGAI, set `DEEPSEEK_API_KEY`, configure the gateway model and URL as supplied by the service, and pass `--llm-profile hkgai` when running. Keep real keys local. For web search, configure Tavily or SerpApi and its matching key.

## 2. Write your own task

The CLI reads a Markdown directory passed to `--tasks`. Create `runs/my-input/my-project/` and create the three files below. Role descriptions and task text can be written in English or Chinese.

**`runs/my-input/README.md`**

```markdown
# My project

<!-- evaluation-bank: supplementary -->

- [Team](my-project/01_role_initialization.md)
- [Task 1](my-project/02_task_1.md)
```

**`runs/my-input/my-project/01_role_initialization.md`**

````markdown
# Scenario 1: My project

| ID | Role |
| --- | --- |
| `lead` | Lead |
| `builder` | Builder |
| `reviewer` | Reviewer |
| `publisher` | Publisher |

## Lead (lead)

```text
Clarify the requirements with the user, assign work, and assemble the final delivery.
```

## Builder (builder)

```text
Create the requested output and explain how to use it.
```

## Reviewer (reviewer)

```text
Independently check the submitted output and record the findings and checks.
```

## Publisher (publisher)

```text
Prepare the final delivery from the checked version.
```
````

**`runs/my-input/my-project/02_task_1.md`**

````markdown
# Task 1

```text
Create a Markdown project kickoff checklist and deliver it as kickoff.md.
Organize it into preparation, execution, and review. For each stage, explain
what to do and how to tell when it is complete.
Ask me first if you need the project type or intended audience.
```
````

Replace the last file's contents with your own task. Declare four roles in the team file. The first role is the lead. The table and descriptions must list the same roles in the same order. To give the same team a second task, add `03_task_2.md` and link it in the input README. Use a single fenced `text` block for the second task as well.

## 3. Run from one terminal

```bash
python -m moumou --tasks runs/my-input --scenario scenario_1 --plan
python -m moumou --tasks runs/my-input --scenario scenario_1 --human console --out runs/my-run
```

`--plan` checks input format, selected roles, model settings, and budgets without calling the model. The second command starts the run. Choose a fresh path for `--out`. The framework creates the directory and its role workspaces. Choose a new name when running again. `--out` is for results and `--tasks` points to task definitions.

When you see `Your answer:`, read the request and check its type.

| Request | Terminal input |
| --- | --- |
| Clarification | Type your answer or choose a displayed option |
| Tool approval | Type `approve` or `reject`, with an optional note |
| Result review | Inspect the delivered files, then type `approve` or `reject` with an optional note |

Pass `--approval-mode manual` to approve each Shell, Python, or file deletion request. The default `auto` mode selects command approval based on the environment. Use `--scenario-timeout` or `--task-timeout` to change execution time in seconds. The latter applies to an individual node. Run `python -m moumou --help` for all options.

## 4. Answer from a second terminal

To keep interaction separate, install the optional UI and connect both terminals to the same run directory.

```bash
python -m pip install -e '.[p2]'
# Terminal 1
python -m moumou --tasks runs/my-input --scenario scenario_1 --human files --out runs/my-file-run
# Terminal 2
python -m moumou.ui.cli --run runs/my-file-run
```

Use `/pending` to see request IDs. Send an answer with `/answer ID your answer` and a decision with `/approve ID` or `/reject ID`. Use `/history` to view recorded interactions. `/quit` detaches the interface while the backend keeps running.

## 5. Find replies, files, and workspaces

- **Run status** Open `runs/my-run/report.md`. The structured summary is in `test.json`.
- **Text result** Find the root task result in `runs/my-run/scenario_1/task_1-result.json`. The second task uses `task_2-result.json`.
- **Delivered files** Open `runs/my-run/scenario_1/task_1-artifacts/manifest.json`. Match the original `path` in `entries`. The `file` field gives the saved copy relative to the scenario folder. Review that copy.
- **Workspaces** Look for `workspaces` in the task result. It records the actual directory for each role. Each run creates fresh workspaces, and later tasks in the same scenario receive published inputs through the handoff.
- **Interaction log** The scenario's `dialogue.jsonl` records requests, responses, and tool interaction. `human/` stores human decisions. Run logs can contain your task materials and answers.

You can rebuild the report offline from an existing run.

```bash
python -m moumou.eval.report --run runs/my-run
```
