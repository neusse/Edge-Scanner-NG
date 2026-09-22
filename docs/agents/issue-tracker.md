# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues at `neusse/Edge-Scanner-NG`. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multiline bodies.
- **Read an issue**: `gh issue view <number> --comments`, including its labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`, with suitable `--label` and `--state` filters.
- **Comment**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`; `gh` does this automatically inside this clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

When changed to `yes`, PRs use the same labels and states as issues:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>`
- **List external PRs**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`, keeping only `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE`
- **Comment, label, or close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, or `gh pr close`

GitHub shares one number space across issues and PRs. Resolve an ambiguous `#42` using `gh pr view 42`, falling back to `gh issue view 42`.

## When a skill says “publish to the issue tracker”

Create a GitHub issue.

## When a skill says “fetch the relevant ticket”

Run `gh issue view <number> --comments`.

## Wayfinding operations

The map is one issue with child issues as tickets.

- **Map**: an issue labelled `wayfinder:map`, containing Notes, Decisions-so-far, and Fog.
- **Child ticket**: link it as a GitHub sub-issue. If sub-issues are unavailable, add it to a task list in the map and put `Part of #<map>` at the top of the child. Use a `wayfinder:<type>` label: `research`, `prototype`, `grilling`, or `task`.
- **Blocking**: use GitHub’s native issue dependencies. If unavailable, put `Blocked by: #<n>, #<n>` at the top of the child.
- **Frontier query**: find the first open, unassigned child without an open blocker.
- **Claim**: `gh issue edit <n> --add-assignee @me`
- **Resolve**: comment with the answer, close the child, and add its context pointer to the map’s Decisions-so-far section.
