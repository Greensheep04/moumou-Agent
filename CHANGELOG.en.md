# Changelog

[简体中文](CHANGELOG.md)

## Current system release

Changes since GitHub baseline `c96fab2`, the starting point for this development round.

- Make the official DeepSeek Flash profile the default and keep HKGAI selectable.
- Add SerpApi and record search allowances and retrieved sources per scenario.
- Keep the start of model requests stable to improve prefix cache reuse.
- Compact less often and prioritize confirmed requirements, delivery versions, and recent work.
- Limit individual request size and cumulative node usage separately, with diagnostics for both.
- Let the lead specify assignments, dependencies, and checks so independent work can run in parallel.
- Let a blocked subtask report missing material so the lead can resume or replace the affected node.
- Update pending dependencies after replacement and reuse other accepted work.
- Give each role a writable directory for each task and carry forward accepted files and rules.
- Hand over numbered versions with file checksums and detect missing or conflicting inputs before testing.
- Check delivery declarations, test scripts, and QA versions before publication so agents can fix specific defects.
- Save QA progress against the tested version and use delivery and QA records to prepare the release.
- Run ordinary commands and cleanup automatically inside the workspace sandbox, with human approval for dependency installation and other controlled actions.
- Track time per node and scenario, let the lead estimate time for each task, and record human waits separately.
- Add closeout checks and traces for diagnosing request timeouts, failed checks, and blocked nodes.
- Keep GitHub's source tracking fix and optional check for repeated commands.
