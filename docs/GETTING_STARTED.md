<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# Getting Started

> A Japanese translation is available at [ja/GETTING_STARTED.md](./ja/GETTING_STARTED.md).

Stratoclave is a self-hosted gateway that puts a tenant-aware credit budget and role-based access control in front of Amazon Bedrock. This guide walks a first-time user from a fresh laptop through signing in, running their first Claude call, and opening the web console.

If you are deploying Stratoclave into your own AWS account, start with [DEPLOYMENT.md](DEPLOYMENT.md) first. Everything below assumes an operator has already handed you a Stratoclave deployment URL.

## Contents

- [Prerequisites](#prerequisites)
- [1. Install the CLI](#1-install-the-cli)
- [2. Bootstrap your CLI configuration](#2-bootstrap-your-cli-configuration)
- [3. Sign in](#3-sign-in)
- [4. Make your first call](#4-make-your-first-call)
- [5. Open the web console](#5-open-the-web-console)
- [6. What actually happens on each request](#6-what-actually-happens-on-each-request)
- [7. Where to go next](#7-where-to-go-next)
- [8. Troubleshooting](#8-troubleshooting)

---

## Prerequisites

- macOS, Linux, or WSL2.
- Rust 1.75 or newer to build the CLI from source. Pre-built binaries are not yet published to the GitHub Releases page of [`littlemex/stratoclave`](https://github.com/littlemex/stratoclave); until then, `cargo build --release` is the supported path.
- A Stratoclave deployment URL from your administrator, for example `https://<your-deployment>.cloudfront.net` (this guide uses the URL as a concrete illustration; substitute your deployment URL).
- One of the following sign-in paths:
  - An email address that an administrator has provisioned, together with a temporary password they have set via `aws cognito-idp admin-set-user-password`, **or**
  - An AWS profile with `aws sso login` already completed, for deployments that have your AWS account registered as a trusted identity source.
- Optional: the `claude` binary (from [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)) on your `PATH` if you want to run `stratoclave claude`.

---

## 1. Install the CLI

Clone the repository and build the `stratoclave` binary:

```bash
git clone https://github.com/littlemex/stratoclave.git
cd stratoclave/cli
cargo build --release
```

A cold build on Apple Silicon compiles roughly 500 crates (the AWS SDK pulls in most of them) and takes **about 2 minutes**; Linux x86_64 is similar. Expect the first build to look idle for the last minute while `reqwest` and friends link — that is normal, not a hang.

The binary lands at `target/release/stratoclave`. Put it on your `PATH`:

```bash
# Option A: symlink into a directory already on PATH
sudo ln -sf "$PWD/target/release/stratoclave" /usr/local/bin/stratoclave

# Option B: add an alias to your shell rc file
echo "alias stratoclave='$PWD/target/release/stratoclave'" >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc

stratoclave --help
```

You should see a help listing with `auth`, `claude`, `usage`, `admin`, `team-lead`, `ui`, `api-key`, and `setup` subcommands.

---

## 2. Bootstrap your CLI configuration

Stratoclave ships a single-command bootstrap. Point `stratoclave setup` at the deployment URL your administrator shared:

```bash
stratoclave setup https://<your-deployment>.cloudfront.net   # your deployment URL
```

The command:

1. Fetches `/.well-known/stratoclave-config` from the deployment (an unauthenticated discovery document).
2. Validates the response schema (`schema_version == "1"`).
3. Writes `~/.stratoclave/config.toml` with the correct Cognito and API fields.
4. Creates `~/.stratoclave/` with mode `0700` and the file with mode `0600`.

Expected output:

```
[INFO] Fetching config from https://<your-deployment>.cloudfront.net/.well-known/stratoclave-config ...

Saved to /home/you/.stratoclave/config.toml
  api_endpoint      = https://<your-deployment>.cloudfront.net
  cognito.domain    = https://stratoclave.auth.us-east-1.amazoncognito.com
  cognito.region    = us-east-1
  cli.default_model = us.anthropic.claude-opus-4-7

Next steps:
  stratoclave auth login --email you@example.com
  # or
  stratoclave auth sso --profile your-sso-profile
```

> **Note.** The summary line reads `cli.default_model` for historical reasons, while the underlying TOML file stores the same value under `[defaults] model`. The values are identical; only the display label differs.

Useful flags:

| Flag | Purpose |
|------|---------|
| `--dry-run` | Print the generated `config.toml` to stdout without writing it. Good for review. |
| `--force`, `-f` | Overwrite an existing `config.toml` non-interactively. The previous file is renamed to `config.toml.bak.<epoch>`. |

### Export `STRATOCLAVE_API_ENDPOINT`

Several subcommands (`auth login`, `admin ...`, `team-lead ...`, `api-key ...`, `usage show`) read the API endpoint from the `STRATOCLAVE_API_ENDPOINT` environment variable rather than from the `[api]` section of `config.toml`. Until this is unified, **export `STRATOCLAVE_API_ENDPOINT` in your shell rc file** so every subcommand behaves consistently:

```bash
export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"
echo 'export STRATOCLAVE_API_ENDPOINT="https://<your-deployment>.cloudfront.net"' >> ~/.zshrc
```

See [CLI_GUIDE.md](CLI_GUIDE.md#configuration-file) for the full precedence rules.

---

## 3. Sign in

Stratoclave supports two sign-in methods. Most teams pick one and standardise on it.

### Option A: Cognito email and password

Ask your administrator for your email plus a one-time password, then run:

```bash
stratoclave auth login --email you@example.com
# Password:  <paste the temporary password>
# [INFO] Temporary password detected. Please set a new password.
# New password:  <pick a new one>
# Confirm new password:  <same again>
# [OK] Logged in as you@example.com. Token saved to ~/.stratoclave/mvp_tokens.json
```

The password is typed into the terminal; no browser is opened. The Cognito `NEW_PASSWORD_REQUIRED` challenge is handled inline on first login. The resulting tokens are stored at `~/.stratoclave/mvp_tokens.json` with mode `0600`.

### Option B: AWS SSO (passwordless)

If your AWS account is registered as a trusted identity source, you can exchange your AWS SSO session for a Stratoclave token directly, with no Cognito password:

```bash
aws sso login --profile your-sso-profile
aws sts get-caller-identity --profile your-sso-profile   # sanity check

stratoclave auth sso --profile your-sso-profile
# [INFO] Loading AWS credentials (profile=your-sso-profile, region=us-east-1)...
# [INFO] Presenting identity to Stratoclave backend...
# [OK] Signed in via sso_user as you@example.com
```

Under the hood the CLI signs a `sts:GetCallerIdentity` call, forwards the signed headers to the backend, and receives a Cognito access token in return. Your long-term AWS credentials never leave your laptop.

Common SSO rejections:

| Error message | Cause and fix |
|---------------|---------------|
| `AWS account ... is not a trusted account` | Ask your administrator to add your AWS account ID to the trusted identity sources. |
| `Role ... does not match the allowed patterns` | The administrator's role allowlist is too narrow. Ask them to widen `allowed_role_patterns`. |
| `EC2 Instance Profile login is not allowed` | Instance profiles are denied by default because they are shared across workloads. Switch to AWS SSO or opt in with your administrator. |
| `is not pre-registered` | The deployment runs in invite-only mode. Request an SSO invite from your administrator. |

### Verify the session

```bash
stratoclave auth whoami
# email: you@example.com
# user_id: a4f824f8-b041-703d-3ec8-f15588b9c969
# org_id: default-org
# roles: user
# total_credit: 1000000
# credit_used: 42318
# remaining_credit: 957682
```

`roles` is a comma-separated list; a user who holds multiple roles appears as `roles: admin,team_lead`.

---

## 4. Make your first call

With a valid session in place, ask Claude anything:

```bash
stratoclave claude -- "Hello, who are you?"
```

Behind the scenes the CLI spawns `claude` as a subprocess and injects:

| Environment variable | Value |
|----------------------|-------|
| `ANTHROPIC_BASE_URL` | Your Stratoclave endpoint. |
| `ANTHROPIC_API_KEY` | The Cognito access token currently in `~/.stratoclave/mvp_tokens.json`. |
| `ANTHROPIC_MODEL` | `us.anthropic.claude-opus-4-7` by default, or whatever `--model` specifies. |

Override the model per call:

```bash
stratoclave claude --model claude-haiku-4-5 -- "Summarise the README in three bullets"
```

Forward flags to `claude` after the `--` separator:

```bash
stratoclave claude -- --print "List files"
```

### Using the Anthropic SDK directly

Any Anthropic-compatible SDK works as long as you point `base_url` at your deployment and use a Stratoclave-issued token or long-lived API key as the API key.

The example below re-uses the short-lived access token that `stratoclave auth login` / `stratoclave auth sso` writes to `~/.stratoclave/mvp_tokens.json`. That file only exists **after** you have signed in with the CLI at least once; if you need a credential for unattended scripts or long-running agents, mint an API key instead (see [`api-key create`](CLI_GUIDE.md#api-key)) and assign the `sk-stratoclave-…` string directly.

```python
import json, os, pathlib
from anthropic import Anthropic

# Requires a prior `stratoclave auth login` (or `auth sso`).
# Access tokens expire hourly — for daemons use an sk-stratoclave-* API key.
tokens = json.loads(pathlib.Path("~/.stratoclave/mvp_tokens.json").expanduser().read_text())

client = Anthropic(
    base_url=os.environ["STRATOCLAVE_API_ENDPOINT"] + "/v1",
    api_key=tokens["access_token"],
)

msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=200,
    messages=[{"role": "user", "content": "Hello"}],
)
print(msg.content)
```

For long-lived credentials (scripts, daemons, Cowork integrations) issue an API key instead of reusing your Cognito token. See [`api-key create`](CLI_GUIDE.md#api-key) for the details.

---

## 5. Open the web console

The web console shows your remaining credit, your tenant, your role, and a usage history view. Open it with:

```bash
stratoclave ui open
```

The CLI appends `?token=<access_token>` to the URL so the browser lands already authenticated.

From the Dashboard you can:

- See remaining credit for your tenant.
- Jump to **My Usage** for model-by-model token consumption over 7, 30, or 90 days.
- Access Admin or Team Lead panels if your role allows.

If you need the URL without opening a browser (handy over SSH), use `stratoclave ui url`. Treat the output as a secret.

---

## 6. What actually happens on each request

1. Your CLI or SDK sends `POST /v1/messages` to the Stratoclave endpoint.
2. The backend validates `Authorization: Bearer <token>` using Cognito JWKS, or looks up the SHA-256 hash of a long-lived API key.
3. Role-based access control checks that the caller holds `messages:send`.
4. A credit check compares `credit_used + estimated_cost` against `total_credit` for the caller's tenant. Exceeding the budget fails the request with HTTP `402 credit_exhausted`.
5. The request is translated to a Bedrock inference profile (for example `us.anthropic.claude-opus-4-7`) and forwarded to Bedrock Converse.
6. The response is streamed back as Server-Sent Events in Anthropic's format.
7. Token counts are written to the usage log and the tenant's `credit_used` is incremented with a conditional DynamoDB update.

Everything is visible from the Dashboard and `stratoclave usage show` seconds later.

---

## 7. Where to go next

- [CLI_GUIDE.md](CLI_GUIDE.md) -- full reference for every `stratoclave` subcommand.
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) -- creating tenants, provisioning users, and managing credits.
- [DEPLOYMENT.md](DEPLOYMENT.md) -- stand up your own Stratoclave deployment.
- [ARCHITECTURE.md](ARCHITECTURE.md) -- how the pieces fit together.
- [COWORK_INTEGRATION.md](COWORK_INTEGRATION.md) -- using Stratoclave as the gateway for Claude Desktop Cowork.

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `API endpoint not configured` | Set `STRATOCLAVE_API_ENDPOINT`, or re-run `stratoclave setup <url>` and ensure the write succeeded. See [Export `STRATOCLAVE_API_ENDPOINT`](#export-stratoclave_api_endpoint). |
| `stratoclave setup` fails with `HTTP 404` on `/.well-known/stratoclave-config` | The deployment predates the discovery endpoint. Ask your administrator to upgrade the backend. |
| `stratoclave setup` fails with `Could not reach ...` | Double-check the URL with your administrator. VPNs and corporate proxies can also block CloudFront. |
| `auth login` fails with HTTP 400 and an error like `NotAuthorizedException` | Wrong password, or the temporary password has expired. Ask your administrator to reset it via `aws cognito-idp admin-set-user-password --no-permanent`. |
| `auth login` fails and mentions `USER_NOT_FOUND` | Your email has not been provisioned. Ask an administrator to run `stratoclave admin user create`. |
| `auth sso` is rejected | See the SSO error table in [section 3](#option-b-aws-sso-passwordless). |
| Requests fail with `401 Unauthorized` | Access tokens expire after one hour. Run `stratoclave auth login` or `stratoclave auth sso` again. |
| Requests fail with `402 Payment Required` / `credit_exhausted` | Your tenant credit is used up. Ask your administrator to raise it with `stratoclave admin user set-credit <user_id> --total N --reset-used`. |
| Requests fail with `400 invalid_model` | The requested model is not on the deployment's allowlist. See the model table in [CLI_GUIDE.md](CLI_GUIDE.md#supported-model-ids) and ask your administrator if the model you want is missing. |
| Requests fail with `422 max_tokens exceeds 32768` | The backend caps `max_tokens` at 32768 per request. Reduce the value in your SDK call. |
| `ui open` shows a stale page | CloudFront caching. Hard reload with `Cmd+Shift+R` / `Ctrl+Shift+R`. |
| `stratoclave claude` reports `Failed to spawn claude` | The `claude` binary is not on your `PATH`. Install Claude Code from the [official docs](https://docs.claude.com/en/docs/claude-code/overview). |

Still stuck? Open an issue at [`littlemex/stratoclave`](https://github.com/littlemex/stratoclave/issues) with the exact command, the CLI version (`stratoclave --version`), and the full error output.

---

## Uninstall

To remove the CLI and all local state:

```bash
rm -f /usr/local/bin/stratoclave
rm -rf ~/.stratoclave
# If you built from source and want to reclaim disk:
rm -rf /path/to/your/clone/target
```

`rm -rf ~/.stratoclave` removes the tokens file (`mvp_tokens.json`) and your `config.toml`, so future runs will require `stratoclave setup` again. If you also want to invalidate the Cognito session server-side, ask an administrator to run `aws cognito-idp admin-user-global-sign-out` for your email.

To tear down an entire Stratoclave deployment (operator-only), see [DEPLOYMENT.md](DEPLOYMENT.md#teardown).
