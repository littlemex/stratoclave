# Contributing to Stratoclave

Thanks for your interest in improving Stratoclave. This document describes
how to set up a development environment, the expectations we hold around
code quality, and the workflow for submitting changes.

By participating you agree to uphold our
[Code of Conduct](./CODE_OF_CONDUCT.md).

> **Note:** Stratoclave is in alpha. APIs, schemas, and infrastructure
> constructs may change between commits. If you are planning non-trivial
> work, please open an issue to discuss the approach before investing
> significant time.

## Table of Contents

- [Ways to contribute](#ways-to-contribute)
- [Reporting bugs](#reporting-bugs)
- [Proposing features](#proposing-features)
- [Development setup](#development-setup)
- [Running the stack locally](#running-the-stack-locally)
- [Testing](#testing)
- [Coding style](#coding-style)
- [Commit messages](#commit-messages)
- [Pull requests](#pull-requests)
- [Security issues](#security-issues)

## Ways to contribute

- **Bug reports** with clear reproduction steps.
- **Feature proposals** that articulate the problem first, solution second.
- **Documentation improvements** — typo fixes, clearer examples, translations.
- **Code contributions** for bugs, features, or refactors.
- **Reviews** of open pull requests from other contributors.

## Reporting bugs

Use the **Bug report** issue template. Include:

- What you expected to happen vs. what actually happened.
- Minimal reproduction steps.
- Environment: commit SHA or release tag, AWS region, OS, browser / CLI
  version, Node/Python/Rust versions as relevant.
- Redacted logs if they help. **Remove secrets** (tokens, API keys, ARNs
  containing account IDs).

Do not report suspected vulnerabilities in public issues — see
[Security issues](#security-issues).

## Proposing features

Use the **Feature request** issue template. Keep the focus on:

1. The problem you're trying to solve, for whom.
2. Your proposed approach.
3. Alternatives you considered and why you discarded them.

We favour small, composable changes. Large features usually require a
design discussion in an issue before a PR is reviewed.

## Development setup

### Prerequisites

- **Python 3.11+** with `uv` or `pip`
- **Node.js 20+** with `npm`
- **Rust stable** (1.78 or newer) via `rustup`
- **AWS CDK v2** (installed via `npm install` inside `iac/`)
- **finch** or **Docker** for container builds
- An AWS account where you can run Amazon Bedrock, Cognito, DynamoDB, ECS,
  ALB, and CloudFront.

### Fork and clone

```bash
git clone https://github.com/<your-username>/stratoclave.git
cd stratoclave
git remote add upstream https://github.com/littlemex/stratoclave.git
```

Work on feature branches created from `main` (or `feature/draft-version` if
directed by maintainers for alpha-era changes):

```bash
git checkout -b feat/descriptive-name
```

### Component builds

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd frontend
npm install

# CLI
cd cli
cargo build

# IaC
cd iac
npm install
```

## Running the stack locally

A fully-local stack isn't yet supported (Cognito, Bedrock, and DynamoDB have
no free local substitutes we target). The current development loop is:

1. Deploy a disposable Stratoclave instance into a sandbox AWS account:
   ```bash
   cd iac && npx cdk deploy --all
   ```
2. Run the Frontend dev server against the deployed backend:
   ```bash
   cd frontend
   VITE_BACKEND_PROXY_TARGET=https://<alb-or-cloudfront> npm run dev
   ```
3. Point the CLI at your deployment:
   ```bash
   stratoclave setup https://<cloudfront>
   stratoclave auth login --email you@example.com
   ```

See `iac/README.md` for the full deployment workflow.

## Testing

We expect PRs to be accompanied by tests where reasonable:

- **Backend:** `pytest` (under `backend/tests/` — currently being rebuilt).
- **CLI:** `cargo test` inside `cli/`.
- **Frontend:** `vitest` (under `frontend/src/**/*.test.{ts,tsx}` — currently
  being rebuilt).
- **IaC:** `jest` via `npm test` inside `iac/` (CDK synth snapshot tests).

If a test harness is missing for the area you're touching, add one or note
the gap in the PR description.

For manual verification, include reproduction steps in the PR. Screenshots
are expected for UI changes.

## Coding style

Formatters and linters are authoritative — run them before pushing.

| Component  | Formatter            | Linter                |
|------------|----------------------|-----------------------|
| Python     | `ruff format`        | `ruff check`          |
| Rust       | `cargo fmt`          | `cargo clippy -- -D warnings` |
| TypeScript | `prettier --write .` | `eslint .`            |

Other expectations:

- Prefer small, focused modules over large omnibus files.
- Avoid introducing new dependencies without justifying them in the PR.
- Public APIs (HTTP endpoints, CLI subcommands, exported TS types) must
  have docstrings / inline documentation.
- **Do not hard-code account IDs, CloudFront URLs, Cognito IDs, or any
  tenant-specific values.** Use environment variables or configuration
  files.

## Commit messages

We use **[Conventional Commits](https://www.conventionalcommits.org/)**:

```
<type>(<scope>): <short summary>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `build`,
`ci`, `perf`. Example:

```
feat(cli): add `stratoclave setup <url>` bootstrap command
fix(backend): treat missing tenant as 404 instead of 500
```

Keep commits atomic and write meaningful bodies for non-trivial changes.
Reference issues with `Refs #N` or `Closes #N`.

## Pull requests

1. Rebase on the latest `main` before opening the PR.
2. Fill in the pull-request template completely (summary, motivation,
   changes, testing, breaking changes, related issues).
3. Keep PRs focused. If a change grows, split it.
4. Ensure CI is green (formatters, linters, unit tests, CDK synth).
5. Request review from a maintainer. We typically respond within a week.
6. Address review feedback with additional commits; we squash on merge.

We do not require Contributor License Agreements (CLAs); the
Apache-2.0 license covers contributions.

## Security issues

Do **not** report suspected vulnerabilities in public issues or pull
requests. Follow the process in [`SECURITY.md`](./SECURITY.md).

---

If you have questions before filing an issue or PR, feel free to reach out
via GitHub Discussions (once enabled) or an issue tagged `question`.
