# Supplementary Collaboration Scenarios

<!-- evaluation-bank: supplementary -->

Lower-priority self-authored regression tasks. The original instructor bank in `eval/tasks` is the default and always takes priority. Select this bank explicitly with `python -m moumou.eval.cli --tasks eval/supplementary`. It uses the same conversation entry, role files, real human input, search accounting and review rules. These results are separate from instructor scores.

The original 15 tasks are retained in eight groups. The final group contains one task; no extra task is invented. Historical budgets and scripted approval settings are documentation only. The original memory-task warm-up exercises are included as explicit preparation in those task prompts; they do not recreate the previous isolated ablation experiment or preload answers.

| Scenario | Four agent roles | Task 1 | Task 2 |
|---|---|---|---|
| [Basic Implementation](scenario_1/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Create an exact-output hello script](scenario_1/02_task_1.md) | [Create a reusable statistics function and CLI](scenario_1/03_task_2.md) |
| [Bug Fixes](scenario_2/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Fix addition and retain the script entry point](scenario_2/02_task_1.md) | [Fix whitespace and duplicate tag normalization](scenario_2/03_task_2.md) |
| [Command Line and Filtering](scenario_3/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Extend a CLI without changing default behavior](scenario_3/02_task_1.md) | [Add category filtering while preserving price behavior](scenario_3/03_task_2.md) |
| [Source Research](scenario_4/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Research interpreter identity from official Python sources](scenario_4/02_task_1.md) | [Research HTTP 429 and Retry-After from RFC 6585](scenario_4/03_task_2.md) |
| [Human Approval](scenario_5/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Delete a deprecated file only after approval](scenario_5/02_task_1.md) | [Honor a rejected deletion](scenario_5/03_task_2.md) |
| [Experience Transfer](scenario_6/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Transfer a CSV parsing lesson to a held-out customer import](scenario_6/02_task_1.md) | [Transfer nested-path and UTF-8 writing experience](scenario_6/03_task_2.md) |
| [Parallel Delegation](scenario_7/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Delegate independent statistics then integrate](scenario_7/02_task_1.md) | [Delegate independent word counts then merge](scenario_7/03_task_2.md) |
| [Composite Collaboration](scenario_8/01_role_initialization.md) | Technical Lead, Software Developer, Test Engineer, Release Manager | [Research, delegate and approve HTTP-policy cleanup](scenario_8/02_task_1.md) | - |
