# Getting Help with Stratoclave

Stratoclave is an alpha-stage open-source project. We do not currently operate
a Slack workspace, Discord server, or mailing list. All user-facing support
happens on this GitHub repository.

## Where to go

| You want to... | Go here |
|----------------|---------|
| Report a reproducible bug | [Open a Bug report issue](https://github.com/littlemex/stratoclave/issues/new?template=bug_report.yml) |
| Propose a feature or change | [Open a Feature request issue](https://github.com/littlemex/stratoclave/issues/new?template=feature_request.yml) |
| Ask a usage / design question | [Start a discussion](https://github.com/littlemex/stratoclave/discussions) (if Discussions is enabled) or open an issue labeled `question` |
| Report a security vulnerability | Follow [`SECURITY.md`](../SECURITY.md). Do **not** open a public issue. |
| Contribute code or docs | Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) |

## Before you open an issue

Please help us help you:

1. **Search existing issues first.** Many questions have already been asked.
2. **Include your version.** Commit SHA or release tag, plus component
   versions (`stratoclave --version`, `node --version`, `python --version`,
   `cargo --version`, etc.) when relevant.
3. **Redact secrets.** Never paste access tokens, API keys, AWS account IDs,
   Cognito User Pool IDs, or full ARNs. Replace them with `<REDACTED>`.
4. **Share a minimal reproducer.** Exact commands or clicks beat vague
   descriptions every time.

## Response expectations

Stratoclave is maintained on a best-effort basis. We aim to triage new issues
within a week, but we do not offer a support SLA. Paid support is not
available.

## Commercial use

Stratoclave is licensed under [Apache-2.0](../LICENSE). You are free to deploy
it inside your organization without contacting the maintainers. If you build
on top of Stratoclave and find bugs or gaps, please open issues or pull
requests — that is our support model.
