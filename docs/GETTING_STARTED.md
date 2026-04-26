# Getting Started

Stratoclave is a self-hosted gateway that puts a tenant-aware credit budget and role-based access control in front of Amazon Bedrock. This guide walks first-time users from a fresh laptop through signing in, running their first Claude call, and opening the web console.

If you are deploying Stratoclave into your own AWS account, start with [DEPLOYMENT.md](DEPLOYMENT.md) first. Everything below assumes your administrator has already handed you a Stratoclave deployment URL.

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

- macOS, Linux, or WSL2
- Rust 1.75 or newer to build the CLI from source (a pre-built binary will be published as releases mature)
- A Stratoclave deployment URL from your administrator, for example `https://d111111abcdef8.cloudfront.net`
- One of the following sign-in paths:
  - An email address plus a temporary password issued by your administrator, or
  - An AWS profile with `aws sso login` already completed, for deployments that have your AWS account registered as a trusted identity source
- Optional: `claude` (the Claude Code CLI) on your `PATH` if you want to run `stratoclave claude`. Install it from the [Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview).

<!-- TODO(docs): Insert screenshot of `stratoclave --help` output here once we have a canonical terminal theme. -->

---

## 1. Install the CLI

Clone the repository and build the `stratoclave` binary:

```bash
git clone https://github.com/<your-org>/stratoclave.git
cd stratoclave/cli
cargo build --release
```

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
stratoclave setup https://d111111abcdef8.cloudfront.net
```

Replace the URL with the one you were given. The command:

1. Fetches `/.well-known/stratoclave-config` from the deployment (an unauthenticated discovery document).
2. Validates the response schema.
3. Writes `~/.stratoclave/config.toml` with correct Cognito and API fields.
4. Creates the directory with `0o700` and the file with `0o600` permissions.

Expected output:

```
[INFO] Fetching config from https://d111111abcdef8.cloudfront.net/.well-known/stratoclave-config ...

Saved to /home/you/.stratoclave/config.toml
  api_endpoint      = https://d111111abcdef8.cloudfront.net
  cognito.domain    = https://stratoclave.auth.us-east-1.amazoncognito.com
  cognito.region    = us-east-1
  cli.default_model = us.anthropic.claude-opus-4-7

Next steps:
  stratoclave auth login --email you@example.com
  # or
  stratoclave auth sso --profile your-sso-profile
```

Useful flags:

| Flag | Purpose |
|------|---------|
| `--dry-run` | Print the generated `config.toml` to stdout without writing it. Good for review. |
| `--force`, `-f` | Overwrite an existing `config.toml` non-interactively. Any existing file is renamed to `config.toml.bak.<epoch>` first. |

### Set `STRATOCLAVE_API_ENDPOINT` once (recommended)

Several commands (`auth login`, `admin`, `team-lead`, `api-key`, `usage show`) read the API endpoint from either the `STRATOCLAVE_API_ENDPOINT` environment variable or a legacy flat-key TOML layout. Exporting the environment variable is the simplest way to stay compatible with every subcommand:

```bash
export STRATOCLAVE_API_ENDPOINT="https://d111111abcdef8.cloudfront.net"
# Persist it
echo 'export STRATOCLAVE_API_ENDPOINT="https://d111111abcdef8.cloudfront.net"' >> ~/.zshrc
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
# [OK] Logged in as you@example.com
```

Your Cognito `NEW_PASSWORD_REQUIRED` challenge is handled inline. The resulting tokens are stored at `~/.stratoclave/mvp_tokens.json` with `0o600` permissions.

### Option B: AWS SSO (passwordless)

If your AWS account is registered as a trusted identity source, you can exchange your AWS SSO session for a Stratoclave token directly, with no Cognito password:

```bash
aws sso login --profile your-sso-profile
aws sts get-caller-identity --profile your-sso-profile   # sanity check

stratoclave auth sso --profile your-sso-profile
# [INFO] Loading AWS credentials (profile=your-sso-profile, region=us-east-1)...
# [INFO] Presenting identity to Stratoclave backend...
# [OK] Signed in via sso_user as you@example.com
#      org_id=default-org roles=["user"]
```

Under the hood the CLI signs a `sts:GetCallerIdentity` call, forwards the signed headers to the backend, and receives a Cognito access token in return.

Common SSO rejections and what they mean:

| Error message | Cause and fix |
|---------------|---------------|
| `AWS account ... is not a trusted account` | Ask your administrator to add your AWS account ID to the trusted identity sources. |
| `Role ... does not match the allowed patterns` | The administrator's role allowlist is too narrow. Ask them to widen `allowed_role_patterns`. |
| `EC2 Instance Profile login is not allowed` | Instance profiles are denied by default because they are shared across workloads. Switch to AWS SSO or opt in with your administrator. |
| `is not pre-registered` | The deployment runs in invite-only mode. Request an SSO invite from your administrator. |

### Verify the session

```bash
stratoclave auth whoami
# email:   you@example.com
# user_id: a4f824f8-b041-703d-3ec8-f15588b9c969
# org_id:  default-org
# roles:   ["user"]
```

---

## 4. Make your first call

With a valid session in place, ask Claude anything:

```bash
stratoclave claude -- "Hello, who are you?"
```

Behind the scenes the CLI spawns `claude` as a subprocess and injects:

| Environment variable | Value |
|----------------------|-------|
| `ANTHROPIC_BASE_URL` | Your Stratoclave endpoint |
| `ANTHROPIC_API_KEY` | The Cognito access token currently in `~/.stratoclave/mvp_tokens.json` |
| `ANTHROPIC_MODEL` | `us.anthropic.claude-opus-4-7` by default, or whatever `--model` specifies |

Override the model per call:

```bash
stratoclave claude --model claude-haiku-4-5 -- "Summarise the README in three bullets"
```

Forward flags to `claude` after the `--` separator:

```bash
stratoclave claude -- --print "List files"
```

### Using the Anthropic SDK directly

Any Anthropic-compatible SDK works as long as you point `base_url` at your deployment and use a Stratoclave-issued token or long-lived API key as the API key. Example in Python:

```python
import json, os, pathlib
from anthropic import Anthropic

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

For long-lived credentials (scripts, daemons, cowork integrations) issue an API key instead of reusing your Cognito token. See [CLI_GUIDE.md `api-key create`](CLI_GUIDE.md#api-key).

---

## 5. Open the web console

The web console shows your remaining credit, your tenant, your role, and a usage history view. Open it with:

```bash
stratoclave ui open
```

The CLI appends `?token=<access_token>` to the URL so the browser lands already authenticated.

<!-- TODO(docs): Insert screenshot of the Dashboard here. -->

From the Dashboard you can:

- See remaining credit for your tenant
- Jump to **My Usage** for model-by-model token consumption over 7, 30, or 90 days
- Access admin or team-lead panels if your role allows

If you need the URL without opening a browser (handy over SSH), use `stratoclave ui url`.

<!-- TODO(docs): Insert screenshot of the My Usage page here. -->

---

## 6. What actually happens on each request

1. Your CLI or SDK sends `POST /v1/messages` to the Stratoclave endpoint.
2. The backend validates `Authorization: Bearer <token>` using Cognito JWKS (or looks up the API key hash).
3. Role-based access control checks that the caller holds `messages:send`.
4. A credit check compares `credit_used + estimated_cost` against `total_credit` for the caller's tenant. If the user would exceed their budget the request fails with HTTP 403 `credit_exhausted`.
5. The request is translated to a Bedrock inference profile (for example `us.anthropic.claude-opus-4-7`) and forwarded to Bedrock Converse.
6. The response is streamed back as Server-Sent Events in Anthropic's format.
7. Token counts are written to the usage log and the tenant's `credit_used` is atomically incremented.

Everything is visible from the Dashboard and `/me/usage` seconds later.

---

## 7. Where to go next

- [CLI_GUIDE.md](CLI_GUIDE.md) - Full reference for every `stratoclave` subcommand.
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) - Creating tenants, issuing users, and managing credits.
- [DEPLOYMENT.md](DEPLOYMENT.md) - Stand up your own Stratoclave deployment.
- [ARCHITECTURE.md](ARCHITECTURE.md) - How the pieces fit together.
- [COWORK_INTEGRATION.md](COWORK_INTEGRATION.md) - Using Stratoclave as the gateway for Claude Desktop cowork.
- [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md) if you plan to contribute or report a vulnerability.

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `stratoclave setup` fails with `HTTP 404` on `/.well-known/stratoclave-config` | The deployment predates the discovery endpoint. Ask your administrator to upgrade the backend. |
| `stratoclave setup` fails with `Could not reach ...` | Double-check the URL with your administrator. VPN or network policies can also block CloudFront. |
| `auth login` fails with HTTP 400 `NotAuthorizedException` | Wrong password, or the temporary password has expired. Ask your administrator to reset it. |
| `auth login` fails with HTTP 400 and the error mentions `USER_NOT_FOUND` | Your email has not been provisioned. Ask an administrator to create the user. |
| `auth sso` is rejected | See the SSO error table in [section 3](#option-b-aws-sso-passwordless). |
| Requests fail with `401 Unauthorized` | Access tokens expire. Run `stratoclave auth login` or `stratoclave auth sso` again. |
| Requests fail with `403 credit_exhausted` | Your tenant credit is used up. Ask your administrator to raise it from the Admin console. |
| `ui open` shows a stale page | CloudFront caching. Hard reload with `Cmd+Shift+R` / `Ctrl+Shift+R`. |
| `stratoclave claude` reports `Failed to spawn claude` | The Claude Code binary is not on your `PATH`. Install it from the [Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview). |
| `API endpoint not configured` | Set `STRATOCLAVE_API_ENDPOINT`, or re-run `stratoclave setup` and ensure the write succeeded. See [section 2](#set-stratoclave_api_endpoint-once-recommended). |

Still stuck? Check the [FAQ](FAQ.md) if one exists in your copy of the repo, or open an issue with the exact command, the version (`stratoclave --version`), and the full error output.
