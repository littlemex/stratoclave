<!--
Thanks for contributing to Stratoclave. Please fill out every section below.
PRs that leave sections blank or delete the template will be asked to restore it.

If this PR addresses a security vulnerability, stop and follow SECURITY.md
instead of opening a public pull request.
-->

## Summary

<!-- One or two sentences: what does this PR do, and why? -->

## Motivation

<!-- What problem does this solve? Link the issue(s) it closes. -->

Closes #

## Changes

<!-- Bullet the user-visible and internal changes. Group by component if the PR spans several. -->

- 

## Type of change

<!-- Check all that apply. -->

- [ ] `feat` — new feature
- [ ] `fix` — bug fix
- [ ] `docs` — documentation only
- [ ] `refactor` — code change that neither fixes a bug nor adds a feature
- [ ] `perf` — performance improvement
- [ ] `test` — adds or updates tests
- [ ] `build` / `ci` / `chore` — tooling, dependencies, infrastructure

## Affected components

- [ ] backend (FastAPI)
- [ ] frontend (React / Vite)
- [ ] cli (Rust)
- [ ] iac (CDK)
- [ ] scripts / bootstrap
- [ ] docs

## Test plan

<!--
Describe how you verified this change. Commands you ran, manual steps you
clicked through, and their outcomes. Attach screenshots for UI changes.
-->

- [ ] Unit tests added or updated (`pytest` / `cargo test` / `vitest` / `jest`).
- [ ] Linters and formatters pass locally (`ruff`, `cargo fmt`, `cargo clippy`, `prettier`, `eslint`).
- [ ] Manual verification described below.

**Manual verification steps:**

1. 

## Breaking changes

<!--
If this PR changes HTTP APIs, DynamoDB schemas, CDK construct IDs / logical
resource names, CLI commands, environment variables, or configuration file
shapes, describe the migration path here. Otherwise write "None".
-->

None.

## Security considerations

<!--
Required. Even a short answer is fine. Consider:
- Does this introduce new code paths that handle credentials, tokens, or
  signed requests?
- Does it change what is logged? Could that include secrets or PII?
- Does it change IAM policies, trust policies, or network ingress?
- Does it expand what external input reaches boto3, Bedrock, or Cognito?
-->

## Documentation

- [ ] README, CONTRIBUTING, or per-component docs updated (or N/A).
- [ ] Public APIs, CLI subcommands, or exported types have docstrings.

## Checklist

- [ ] I have read [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/).
- [ ] No secrets, account IDs, or tenant-specific values are hard-coded.
- [ ] This PR is focused; unrelated changes are split into separate PRs.
- [ ] The branch is rebased on the latest `main`.
