# Team Integration and Delivery

[简体中文](TASKS.md) · [English](TASKS.en.md)

This page maps module ownership to the current integration and delivery work.
Implementation details live in the role briefs and [interface specification](../INTERFACES.en.md).

## Runtime and source conventions

Use an activated Python 3.11+ environment; native Windows delivery uses Conda
`nlp`. Install the project with `python -m pip install -e ".[dev]"`, configure the
course DeepSeek/HKGAI values in ignored `.env`, then follow [setup](../SETUP.en.md).
Do not commit actual credentials or runtime data.

| Entry | Purpose |
| --- | --- |
| `python -m moumou --plan` | Inspect the original task sources without model calls |
| `python -m moumou` | Instructor conversations with real HumanIO and quality review |
| `python -m moumou.eval.cli --tasks eval/supplementary` | Explicit lower-priority supplementary evaluation |
| `python -m pytest -q` / `make unit-test` | Framework regressions, separate from model evaluation |
| `python -m moumou.eval.report --run RUN_DIRECTORY` | Reconstruct a saved report without rerunning tasks |

The default `eval/tasks` contains three instructor groups and six goals. Keep its
original texts and hashes. The retained fifteen-task supplementary bank uses the
same Markdown protocol and a separate denominator. Commands here are reproduction
instructions, not new verification results.

## Ownership and handoff

| Boundary | Provides | Integration acceptance |
| --- | --- | --- |
| P0 orchestration | States, routing, delegation, context, approval, acceptance and evolution | Shared models remain coherent; failure, recovery and cancellation semantics are exercised |
| P1 execution | Tool contracts, workspace isolation, explicit declared artifacts, Git/search and native execution | Tools respect the workspace and approval boundary; consumers receive the declared files |
| P2 interaction | Private/group channels, actual human interaction and replay presentation | Real channel/UI consumers preserve identities, requests, decisions and pending state |
| P3 data and evaluation | Trace/memory persistence, task sources, continuing team sessions, metrics and offline evidence tools | Stored records and reports preserve the full plan, actual quality decisions, provenance and unknown coverage |

For P3 integration, confirm these current boundaries:

- A task result is structured: `summary`, `files`, `notes` and `blockers`. Files
  cross private workspace boundaries through explicit artifact integration.
- Human exchanges use their original request IDs and project/conversation/task/role
  scope. Answer restoration does not authorize a later tool operation.
- `parent_id` is the task graph relationship; `parent_span` is the trace hierarchy.
  A missing historical span parent must remain missing.
- Formal team members use stable role identities and project-role memory across
  their two tasks. Generic task instances retain the documented shared-role policy.
- Replay task identities include the scenario and task ID. Pending, staged and
  consumed human responses are different observations.
- A source-linked replay export is available; its existence is not P2 consumer
  sign-off. Actual P0/P1/P2 confirmation is still required.

Changes to `src/moumou/contracts/` require coordination with affected consumers and
matching documentation/tests. Local implementation changes do not prove that all
consumers have integrated them.

## Completion and evidence

The latest P3 recovery, trace, snapshot, review and replay changes have authored
regressions that have not been executed. Historical framework results belong to
their recorded source version. They do not verify the new tree or complete the
teacher's six goals.

Follow the [delivery checklist](../DELIVERY.en.md) for the repository, architecture,
formal performance report, genuine collaborative commit history and presentation
materials. Preserve real failures, unposted goals and unknown quality; do not turn
scripted fixtures or agent `DONE` into task acceptance.

## Collaboration and Git

Fetch remote `main` before a new task and inspect changes before merging. Use the
assigned development branch (`dev-P3` for P3); do not overwrite another member's work
or resolve a conflict without understanding both changes. For P3 commits, confirm
the concise English message and exact paths before staging/committing. Merge and
push require their own scope. Keep local planning, private evidence and generated
run files outside the submission.

If a contract or integration fails, record the source version, exact command,
environment, observed output and reproduction steps. Use the
[FAQ](../FAQ.en.md), [status](../STATUS.en.md) and role briefs for navigation.
