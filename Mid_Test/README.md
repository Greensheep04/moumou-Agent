# Three Multi-Agent Collaboration Scenarios

These materials define only the group members, individual role prompts, and tasks. You can design the system-level and general collaboration mechanisms (Harness). During execution, the agent team determines the task breakdown, workflow, technical approach, testing methods, and completion criteria in consultation with the human requester.

| Scenario                                                                                  | Four agent roles                                                              | Task 1                                                                        | Task 2                                                                                      |
| ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| [Software Development Team](scenario_1_software_engineering 2/01_role_initialization.md)  | Technical Lead, Software Developer, Test Engineer, Release Manager            | [Meeting Room Booking System](scenario_1_software_engineering 2/02_task_1.md) | [Room Usage Dashboard](scenario_1_software_engineering 2/03_task_2.md)                        |
| [Newsroom](scenario_2_newsroom 2/01_role_initialization.md)                               | Editor-in-Chief, Reporter, Fact-Checker, Editor                               | [Initial News Report](scenario_2_newsroom 2/02_task_1.md)                     | [Fact-Check AI-Writing Detectors](scenario_2_newsroom 2/03_task_2.md)                       |
| [Securities Investment Fund Team](scenario_3_securities_fund 2/01_role_initialization.md) | Fund Manager, Investment Research Analyst, Risk Manager, Marketing Specialist | [Simulated Investment Proposal](scenario_3_securities_fund 2/02_task_1.md)    | [Semiconductor Industry Analysis in HTML Slides](scenario_3_securities_fund 2/03_task_2.md) |

Each scenario folder contains three files:

```text
01_role_initialization.md  # Group members and individual role prompts
02_task_1.md               # The first task posted to the group
03_task_2.md               # The second task for the same team
```

Create the group and its agents, then post Task 1. Subsequently, post Task 2 to the same group so that the team continues working with its existing collaboration context. Move to the next scenario after both tasks. The human responds to the actual conversation rather than following a predefined question-and-answer script.

Task quality can be evaluated by providing the complete dialogue history and task outputs to models such as DeepSeek or ChatGPT.
