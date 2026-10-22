# Evaluation

[简体中文](EVAL.md) · [English](EVAL.en.md)

## How to run

```bash
# needs a key in .env
make eval

# one task only, handy when debugging
.venv/bin/python -m moumou.eval.cli --only s1_hello

# three runs per task (otherwise the yaml's repeats is used)
.venv/bin/python -m moumou.eval.cli --repeats 3
```

Output:

- each task gets its own directory `runs/eval/<task_id>/r<N>/` with its own database, workspace
  and execution log
- the summary report is written to `runs/eval/report.md`

## Task format

One YAML file per task, under `eval/tasks/`.

```yaml
id: s1_hello                     # unique; duplicates are an error
title: Write a runnable script from scratch   # for humans
scenario: from_scratch           # one of the seven scenarios, see below
goal: |                          # the requirement text shown to the agent
  Create hello.py in the current project...

acceptance:                      # must be machine-checkable
  - kind: command
    target: python3 hello.py
    expect: "0"

setup:                           # optional: files seeded into the workspace
  calc.py: |
    def add(a, b):
        return a - b

human:                           # optional: scripted answers for batch runs
  default: approve
  decisions:
    fs_delete: approve           # keyed by action_class
  answers:
    "filename": "hello.py"       # matched against the question text

budget:                          # optional: override the defaults
  max_steps: 15
  max_wall_s: 300

repeats: 1
tags: [scenario-1, easy]
```

### Scenario values

| Value | Meaning |
| --- | --- |
| `from_scratch` | build from nothing |
| `debug` | fix a bug |
| `extend` | add a feature to existing code |
| `research` | web research |
| `needs_approval` | requires human confirmation |
| `use_memory` | depends on historical experience |
| `parallel` | multiple agents in parallel |

### Acceptance kinds

| kind | target | expect |
| --- | --- | --- |
| `command` | the command to run | required exit code, default `"0"` |
| `pytest` | test path, default `.` | required exit code, default `"0"` |
| `file_exists` | file path | not needed |
| `file_not_exists` | file path | not needed |
| `file_contains` | file path | a regular expression |
| `json_key` | JSON file path | `a.b.c`, or `a.b.c=value` |

## Metric definitions

| Metric | Definition | Why this way |
| --- | --- | --- |
| **Completion rate** | tasks reaching `DONE` ÷ tasks created | children count, so more decomposition means a larger denominator — which reflects whether decomposition payed off |
| **Execution time** | wall clock from `submit` to the last task reaching a terminal state | event-driven scheduling, so no polling noise |
| **Token cost** | sum of `tokens_in + tokens_out` over all spans | multi-agent cost runs away easily; this is a real problem |

**Run every task at least three times** and report mean ± standard deviation. The model is
stochastic; a single run proves nothing.

## Current tasks

| id | Scenario | What it tests |
| --- | --- | --- |
| `s1_hello` | from_scratch | the basic path: write a file → run a command |
| `s2_bugfix` | debug | read before editing, rather than rewriting the module from scratch |
| `s5_approval` | needs_approval | a delete triggers approval and completes afterwards |

Twelve more are needed; roughly two per scenario across the seven.

## What the report looks like

`runs/eval/report.md`:

| task | scenario | runs | completion | std | wall (s) | tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| s1_hello | from_scratch | 1 | 1.00 | 0.00 | 4.2 | 7423 |
| s2_bugfix | debug | 1 | 1.00 | 0.00 | 4.2 | 7695 |
| s5_approval | needs_approval | 1 | 1.00 | 0.00 | 4.1 | 9943 |

(Measured 2026-10-22 against `deepseek-flash`; all three tasks cost ¥0.057 in total.)

The overall completion rate is reported at the bottom, together with the tasks that failed.

## Still to add

- time breakdown (model / tools / waiting on a human) — needs aggregation over spans
- ablations (one group with memory off, one with parallelism off)
- charts (matplotlib, or plain markdown tables)
