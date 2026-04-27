# Security Policy

Stratoclave takes security seriously. We appreciate reports from security
researchers and the community that help keep our users safe. This document
describes how to report vulnerabilities and what to expect in response.

## Supported Versions

Stratoclave is currently **alpha** software. No stable release has been cut;
only the latest commit on the `main` branch is supported. Once we cut `v0.1.0`,
this section will be updated to reflect supported release lines.

| Version / Branch       | Supported          |
|------------------------|--------------------|
| `main` (latest commit) | :white_check_mark: |
| everything else        | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Use one of the following private channels instead:

1. **Preferred — GitHub Private Vulnerability Report.** From the repository's
   **Security** tab, click **Report a vulnerability**. This opens a
   confidential advisory visible only to maintainers.
2. **Fallback — direct email to the maintainers.** If private advisories are
   unavailable for your account, open a regular issue titled
   *"Request for private disclosure channel"* (without vulnerability details)
   and a maintainer will provide an email address.

When reporting, please include:

- A clear description of the vulnerability and its impact.
- Step-by-step reproduction, including:
  - The affected commit SHA or version tag.
  - The deployment topology (self-hosted account, region).
  - Any HTTP requests, payloads, or minimal proof-of-concept code.
- Whether the issue is already publicly known or has a CVE assigned.
- Your name and affiliation if you want public credit in the advisory.

Encrypted submissions are welcome. Include your PGP public key or Signal
handle in the initial contact if you want an encrypted reply channel.

## Scope

In scope:

- The Stratoclave source code in this repository (`backend/`, `frontend/`,
  `cli/`, `iac/`, `scripts/`).
- Default deployment configurations shipped in this repository.
- Documentation that, if followed as-written, would produce an insecure
  deployment.

Out of scope:

- Vulnerabilities in third-party services Stratoclave integrates with
  (Amazon Cognito, Amazon Bedrock, DynamoDB, CloudFront) — please report
  those directly to the provider.
- Social engineering, denial-of-service via resource exhaustion, or physical
  attacks.
- Self-hosted deployments that deviate from the documented configuration
  (e.g., disabling authentication).

## Our Commitment

When you report a vulnerability through the channels above, we commit to:

1. **Acknowledge** receipt within **three business days**.
2. **Triage** and provide an initial assessment within **seven business days**,
   including a severity estimate and expected next steps.
3. **Keep you informed** of progress at reasonable intervals while we
   investigate and develop a fix.
4. **Coordinate disclosure.** We aim to release fixes and publish a public
   advisory within **90 days** of your report. If you need an earlier
   disclosure date (for example, to align with a coordinated release), let
   us know and we'll work with you.
5. **Credit** you in the advisory and in release notes, unless you prefer
   to remain anonymous.

## Safe Harbor

We will not pursue legal action against security researchers who:

- Make a good-faith effort to avoid privacy violations, data destruction, or
  disruption of service.
- Report vulnerabilities privately before any public disclosure.
- Do not exploit the vulnerability beyond what is necessary to demonstrate it.

If you are unsure whether a specific activity is in scope, contact us first.

## Hall of Fame

Once we begin receiving reports, we plan to maintain a public list of
researchers who have helped us improve Stratoclave's security. Let us know
if you'd like to be included.

## Preventing accidental secret disclosure

Contributors must avoid committing deployment-specific identifiers
(CloudFront distribution IDs, Cognito User Pool IDs, ALB DNS names, AWS
account IDs, live JWTs, access keys) to documentation, blog drafts, or
source files.

### Automated check

A shape-based scan runs on every push and pull request via
[`.github/workflows/secrets-scan.yml`](./.github/workflows/secrets-scan.yml).
It invokes [`scripts/check-no-hardcoded-secrets.sh`](./scripts/check-no-hardcoded-secrets.sh),
which greps for the following *patterns* (not fixed values) across tracked
and untracked files:

- CloudFront distribution: `[a-z0-9]{13,14}\.cloudfront\.net`
- Cognito User Pool ID: `<region>_[A-Za-z0-9]{9}`
- ALB DNS: `<name>-<9-10 digits>.<region>.elb.amazonaws.com`
- ECR URI: `<12-digit account>.dkr.ecr.<region>.amazonaws.com`
- AWS access key: `(AKIA|ASIA)[A-Z0-9]{16}`
- JWT: three-segment base64url tokens (minimum length gated)
- AWS secret access key in a key-like context

Legitimate test fixtures and documentation placeholders are allowlisted
(`<your-...>`, `example.com`, `test-alb-`, `d111111abcdef8`, etc.).

### Running the scan locally

```bash
./scripts/check-no-hardcoded-secrets.sh
```

### Installing the pre-commit hook (recommended)

```bash
./scripts/install-git-hooks.sh
```

This installs `.git/hooks/pre-commit` that runs the scan before every
commit. Bypass with `git commit --no-verify` only when necessary.

### When a match is a false positive

Extend `ALLOWLIST_REGEX` in
[`scripts/check-no-hardcoded-secrets.sh`](./scripts/check-no-hardcoded-secrets.sh)
rather than hard-coding the value. Prefer anchor strings like `test-`,
`fake-`, `dummy-`, or angle-bracket placeholders that are obviously
non-production.
