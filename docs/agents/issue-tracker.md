# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in `williamxhero/stock_advisor`. Use the `gh` CLI for all operations.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
- Read: `gh issue view <number> --comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
- Comment: `gh issue comment <number> --body "..."`.
- Label: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

Infer the repository from `git remote -v`. Pull requests are not a triage request surface.

## Skill routing

- When a skill says “publish to the issue tracker”, create a GitHub issue.
- When a skill says “fetch the relevant ticket”, run `gh issue view <number> --comments`.
- Use native GitHub issue dependencies for blocking edges when available. Otherwise include `Blocked by: #<number>` at the top of the issue body.
- A ticket is ready only when all blockers are closed.
