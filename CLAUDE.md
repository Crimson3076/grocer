@AGENTS.md

## Rules for the automated GitHub agent

When fixing an issue:
1. Reproduce first: write a failing test that shows the bug, then fix it, then run the full test suite.
2. Keep the change minimal and focused on the issue. No refactors, renames, dependency bumps or formatting churn.
3. Never edit `.github/`, Dockerfiles/compose files, dependency/lock files, migrations, auth or secret handling.
   If the fix needs that, stop and explain in a comment instead.
4. Open a pull request with `gh pr create`, title `fix: <short description>`, and a body that starts with
   `Fixes #<issue number>` followed by: root cause, what changed, and how it was tested.
5. If the issue is unclear or not reproducible, comment on the issue asking for exactly what's missing and stop.
