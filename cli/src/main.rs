//! Stratoclave CLI (Phase 2, clap derive only).
//!
//! Command tree:
//!   stratoclave
//!   ├── auth      { login | logout | whoami }
//!   ├── claude    [--model X] -- [claude-args]
//!   ├── codex     [--model X] -- [codex-args]
//!   ├── usage     show [--since-days N] [--limit M]
//!   ├── admin
//!   │   ├── user   { create | list | show | delete | assign-tenant | set-credit }
//!   │   ├── tenant { create | list | show | delete | set-owner | members | usage | pool-budget }
//!   │   └── usage  show [--tenant T] [--user U] [--since X] [--until Y] [--limit N]
//!   ├── team-lead
//!   │   └── tenant { create | list | show | members | usage }
//!   └── ui        { open | url }
//!
//! Invoked with no arguments and a non-TTY stdin, the binary falls back
//! to pipe mode (`commands::pipe`). The legacy hand-rolled subcommand
//! parsers (`login-mvp` etc.) have been removed.

mod auth;
mod client;
mod commands;
mod config;
mod mvp;
mod output;
mod policy;

use clap::{Parser, Subcommand};
use std::process::ExitCode;

use crate::commands::ui::UiCommand;

#[derive(Debug, Parser)]
#[command(
    name = "stratoclave",
    version,
    about = "Stratoclave CLI: Bedrock proxy with tenant-level RBAC",
    disable_help_subcommand = true,
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Authentication (Cognito User/Pass login)
    Auth {
        #[command(subcommand)]
        action: AuthAction,
    },
    /// Launch claude code via Stratoclave proxy
    Claude {
        /// Override model ID (ANTHROPIC_MODEL)
        #[arg(long)]
        model: Option<String>,
        /// Attribution group id (x-sc-group-id header), [A-Za-z0-9._:-]{1,64}.
        /// Must appear BEFORE any args destined for claude itself.
        #[arg(long)]
        group_id: Option<String>,
        /// Workflow run id (x-sc-workflow-run-id header), [A-Za-z0-9._:-]{1,64}.
        /// If absent the backend generates a wr_* id. Must appear before child args.
        #[arg(long)]
        workflow_run_id: Option<String>,
        /// VSR hard model pin (x-sc-model-pin header), [A-Za-z0-9._:/-]{1,128}.
        /// Pins every request to exactly this model — no cascade. Before child args.
        #[arg(long)]
        model_pin: Option<String>,
        /// Extra args passed to claude
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Launch OpenAI codex via Stratoclave proxy
    Codex {
        /// Override model ID (e.g. openai.gpt-5.4)
        #[arg(long)]
        model: Option<String>,
        /// Attribution group id (x-sc-group-id header), [A-Za-z0-9._:-]{1,64}.
        /// Must appear BEFORE any args destined for codex itself.
        #[arg(long)]
        group_id: Option<String>,
        /// Workflow run id (x-sc-workflow-run-id header), [A-Za-z0-9._:-]{1,64}.
        /// If absent the backend generates a wr_* id. Must appear before child args.
        #[arg(long)]
        workflow_run_id: Option<String>,
        /// VSR hard model pin (x-sc-model-pin header), [A-Za-z0-9._:/-]{1,128}.
        /// Pins every request to exactly this model — no cascade. Before child args.
        #[arg(long)]
        model_pin: Option<String>,
        /// Extra args passed to codex
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Self usage summary + recent history
    Usage {
        #[command(subcommand)]
        action: UsageAction,
    },
    /// Admin operations (admin role only)
    Admin {
        #[command(subcommand)]
        action: AdminAction,
    },
    /// Team Lead operations (own tenants only)
    #[command(name = "team-lead")]
    TeamLead {
        #[command(subcommand)]
        action: TeamLeadAction,
    },
    /// Open the Stratoclave web UI
    Ui {
        #[arg(default_value = "open")]
        action: String,
    },
    /// Long-lived API keys (sk-stratoclave-*) for cowork or custom gateways
    #[command(name = "api-key")]
    ApiKey {
        #[command(subcommand)]
        action: ApiKeyAction,
    },
    /// Bootstrap CLI configuration from a Stratoclave deployment URL
    Setup {
        /// Stratoclave API endpoint (e.g. https://xxx.cloudfront.net)
        api_endpoint: String,

        /// Overwrite existing ~/.stratoclave/config.toml without prompting
        #[arg(long, short = 'f')]
        force: bool,

        /// Print resulting config.toml to stdout without writing
        #[arg(long)]
        dry_run: bool,

        /// Also patch ~/.codex/config.toml with the [model_providers.stratoclave]
        /// block so the system-wide `codex` binary points at this deployment.
        /// Without this flag codex configuration is left untouched; the
        /// `stratoclave codex` subcommand uses its own ephemeral config.
        #[arg(long)]
        codex: bool,
    },
}

#[derive(Debug, Subcommand)]
enum ApiKeyAction {
    /// Create a new long-lived API key (plaintext shown once)
    Create {
        /// Human-readable label, e.g. "cowork on macbook"
        #[arg(long)]
        name: Option<String>,
        /// Scope (permission) to attach, repeatable. Must be a subset of your roles.
        /// Defaults to messages:send + usage:read-self when omitted.
        #[arg(long = "scope")]
        scopes: Vec<String>,
        /// Expiration in days. 0 means no expiration. Default 30.
        #[arg(long = "expires-days")]
        expires_days: Option<u32>,
    },
    /// List your own API keys
    List {
        #[arg(long)]
        include_revoked: bool,
    },
    /// Revoke an API key by its key_id (see the list command)
    Revoke { key_id: String },
    /// Admin-only: list every API key in the system
    #[command(name = "admin-list")]
    AdminList {
        #[arg(long)]
        include_revoked: bool,
    },
    /// Admin-only: revoke any API key by key_id
    #[command(name = "admin-revoke")]
    AdminRevoke { key_id: String },
}

#[derive(Debug, Subcommand)]
enum AuthAction {
    /// Login with email + password (Cognito User/Pass)
    Login {
        #[arg(long)]
        email: Option<String>,
        /// DEPRECATED: passing the password on the command line exposes it
        /// to anyone who can read the process list (`ps`/`/proc`/audit
        /// logs). Prefer the `STRATOCLAVE_PASSWORD` environment variable
        /// or the interactive prompt instead. The flag is retained only
        /// for migration scripts and is hidden from `--help`.
        #[arg(long, hide = true)]
        password: Option<String>,
        /// Save password to OS keychain (macOS only for MVP)
        #[arg(long)]
        save_password: bool,
    },
    /// Login via AWS SSO / STS (uses local AWS credentials, seamless for `aws sso login` users)
    Sso {
        /// AWS profile name (defaults to AWS_PROFILE env var or default profile)
        #[arg(long)]
        profile: Option<String>,
        /// AWS region for STS endpoint (defaults to AWS_REGION env var or us-east-1)
        #[arg(long)]
        region: Option<String>,
    },
    /// Clear local tokens
    Logout,
    /// Print current user info + credit summary
    Whoami,
}

#[derive(Debug, Subcommand)]
enum UsageAction {
    /// Show usage summary + recent history for the current user
    Show {
        #[arg(long, default_value_t = 30)]
        since_days: u32,
        #[arg(long, default_value_t = 20)]
        limit: u32,
    },
}

#[derive(Debug, Subcommand)]
enum AdminAction {
    /// User management
    User {
        #[command(subcommand)]
        action: AdminUserAction,
    },
    /// Tenant management
    Tenant {
        #[command(subcommand)]
        action: AdminTenantAction,
    },
    /// Admin-wide usage logs
    Usage {
        #[command(subcommand)]
        action: AdminUsageAction,
    },
}

#[derive(Debug, Subcommand)]
enum AdminUserAction {
    /// Create a new user
    Create {
        #[arg(long)]
        email: String,
        /// admin | team_lead | user (default user)
        #[arg(long, default_value = "user")]
        role: String,
        #[arg(long)]
        tenant: Option<String>,
        #[arg(long)]
        total_credit: Option<u64>,
    },
    /// List users (admin)
    List {
        #[arg(long)]
        role: Option<String>,
        #[arg(long)]
        tenant: Option<String>,
        #[arg(long, default_value_t = 50)]
        limit: u32,
    },
    /// Show a single user by user_id
    Show { user_id: String },
    /// Delete a user (Cognito + DynamoDB; archived UserTenants stays for audit)
    Delete { user_id: String },
    /// Move a user to a different tenant (triggers re-login)
    #[command(name = "assign-tenant")]
    AssignTenant {
        user_id: String,
        #[arg(long)]
        tenant: String,
        #[arg(long, default_value = "user")]
        new_role: String,
        #[arg(long)]
        total_credit: Option<u64>,
    },
    /// Overwrite a user's credit budget
    #[command(name = "set-credit")]
    SetCredit {
        user_id: String,
        #[arg(long)]
        total: u64,
        #[arg(long, default_value_t = false)]
        reset_used: bool,
    },
    /// Promote/demote a user (replaces role). Backend enforces last-admin
    /// protection and blocks demoting a team_lead who still owns a tenant.
    #[command(name = "set-role")]
    SetRole {
        user_id: String,
        #[arg(long, value_parser = ["admin", "team_lead", "user"])]
        role: String,
    },
}

#[derive(Debug, Subcommand)]
enum AdminTenantAction {
    /// Create a tenant (Admin owns by default via admin-owned)
    Create {
        #[arg(long)]
        name: String,
        /// team_lead user_id (Cognito sub) or "admin-owned"
        #[arg(long, conflicts_with = "team_lead_email")]
        team_lead: Option<String>,
        /// email (the CLI resolves it to a Cognito sub via `admin user list`)
        #[arg(long, conflicts_with = "team_lead")]
        team_lead_email: Option<String>,
        #[arg(long)]
        default_credit: Option<u64>,
    },
    /// List all tenants
    List {
        #[arg(long, default_value_t = 50)]
        limit: u32,
    },
    /// Show a tenant
    Show { tenant_id: String },
    /// Archive a tenant (status=archived)
    Delete { tenant_id: String },
    /// Reassign owner (Critical C-C)
    #[command(name = "set-owner")]
    SetOwner {
        tenant_id: String,
        #[arg(long, conflicts_with = "team_lead_email")]
        team_lead: Option<String>,
        #[arg(long, conflicts_with = "team_lead")]
        team_lead_email: Option<String>,
    },
    /// List members of a tenant (with user_id)
    Members { tenant_id: String },
    /// Tenant usage summary
    Usage {
        tenant_id: String,
        #[arg(long, default_value_t = 30)]
        since_days: u32,
    },
    /// Manage the tenant's dollar pool budget (A-1)
    #[command(name = "pool-budget", subcommand)]
    PoolBudget(AdminPoolBudgetAction),
    /// Manage the tenant/user routing config (P0-11: chain, quotas, allowlist)
    #[command(name = "routing-config", subcommand)]
    RoutingConfig(AdminRoutingConfigAction),
}

#[derive(Debug, Subcommand)]
enum AdminRoutingConfigAction {
    /// Show the current routing config (tenant, or a user override with --user)
    Get {
        tenant_id: String,
        /// Show a per-user override instead of the tenant config
        #[arg(long)]
        user: Option<String>,
    },
    /// Replace the routing config from a JSON file (or stdin with "-")
    Set {
        tenant_id: String,
        /// Path to a JSON body, or "-" to read stdin. Tenant shape:
        /// {"chain":[...],"allowlist":[...],"quotas":{model:{"limit":N}},
        ///  "fallback_default":"on|off"}. User shape:
        /// {"chain":[...],"preferred_model":...,"fallback":"on|off"}.
        #[arg(long)]
        file: String,
        /// Write a per-user override instead of the tenant config
        #[arg(long)]
        user: Option<String>,
    },
}

#[derive(Debug, Subcommand)]
enum AdminPoolBudgetAction {
    /// Set (create or update) the pool ceiling for a period
    Set {
        tenant_id: String,
        /// Ceiling in USD, e.g. "500", "$500", "500.50"
        #[arg(long)]
        limit_usd: String,
        /// Billing period YYYY-MM (UTC). Defaults to the current month.
        #[arg(long)]
        period: Option<String>,
        /// Pool status
        #[arg(long, default_value = "active", value_parser = ["active", "suspended"])]
        status: String,
    },
    /// Show the pool budget and live usage for a period
    Show {
        tenant_id: String,
        /// Billing period YYYY-MM (UTC). Defaults to the current month.
        #[arg(long)]
        period: Option<String>,
    },
}

#[derive(Debug, Subcommand)]
enum AdminUsageAction {
    /// List raw usage logs (filterable)
    Show {
        #[arg(long)]
        tenant: Option<String>,
        #[arg(long)]
        user: Option<String>,
        #[arg(long)]
        since: Option<String>,
        #[arg(long)]
        until: Option<String>,
        #[arg(long, default_value_t = 100)]
        limit: u32,
    },
}

#[derive(Debug, Subcommand)]
enum TeamLeadAction {
    /// Manage own tenants
    Tenant {
        #[command(subcommand)]
        action: TeamLeadTenantAction,
    },
}

#[derive(Debug, Subcommand)]
enum TeamLeadTenantAction {
    /// Create a tenant (owned by the team lead)
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        default_credit: Option<u64>,
    },
    /// List own tenants
    List,
    /// Show a single own tenant
    Show { tenant_id: String },
    /// List members of own tenant (email + credit only, user_id is NOT shown)
    Members { tenant_id: String },
    /// Own tenant usage summary
    Usage {
        tenant_id: String,
        #[arg(long, default_value_t = 30)]
        since_days: u32,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    // A-10-policy: when the embedded enterprise policy disables debug,
    // strip every debug / log-verbosity environment variable before any
    // crate has a chance to read it. Previously `disable_debug` was
    // parsed from `policy.toml` and surfaced on the `POLICY` constant
    // but never consulted, so an enterprise build that opted in to
    // silenced debug output still exposed full `RUST_LOG=trace` /
    // tokio-console / reqwest dumps once an end user set the env.
    if policy::POLICY.disable_debug {
        // SAFETY: removing env vars from the process environment is
        // racy in multi-threaded code, but this runs before tokio
        // spawns a runtime / before any auxiliary thread starts.
        for k in [
            "RUST_LOG",
            "RUST_BACKTRACE",
            "STRATOCLAVE_DEBUG",
            "RUST_SPANTRACE",
            "REQWEST_LOG",
            "TOKIO_CONSOLE_BIND",
        ] {
            std::env::remove_var(k);
        }
    }

    // Non-TTY stdin and no args → fall back to pipe mode (legacy behaviour).
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 1 && !client::is_stdin_tty() {
        return run_pipe().await;
    }

    let cli = match Cli::try_parse() {
        Ok(c) => c,
        Err(e) => {
            // Pass clap's automatic help / version / error output through.
            e.print().ok();
            return ExitCode::from(e.exit_code() as u8);
        }
    };

    match cli.command {
        Some(Commands::Auth { action }) => dispatch_auth(action).await,
        Some(Commands::Claude {
            model,
            group_id,
            workflow_run_id,
            model_pin,
            args,
        }) => dispatch_claude(model, group_id, workflow_run_id, model_pin, args).await,
        Some(Commands::Codex {
            model,
            group_id,
            workflow_run_id,
            model_pin,
            args,
        }) => dispatch_codex(model, group_id, workflow_run_id, model_pin, args).await,
        Some(Commands::Usage { action }) => dispatch_usage(action).await,
        Some(Commands::Admin { action }) => dispatch_admin(action).await,
        Some(Commands::TeamLead { action }) => dispatch_team_lead(action).await,
        Some(Commands::Ui { action }) => dispatch_ui(&action).await,
        Some(Commands::ApiKey { action }) => dispatch_api_key(action).await,
        Some(Commands::Setup {
            api_endpoint,
            force,
            dry_run,
            codex,
        }) => wrap(commands::setup::run(api_endpoint, force, dry_run, codex).await),
        None => {
            eprintln!("Usage: stratoclave <command> --help");
            ExitCode::from(1)
        }
    }
}

// ------------------------------------------------------------------
// Dispatchers
// ------------------------------------------------------------------
async fn dispatch_auth(action: AuthAction) -> ExitCode {
    match action {
        AuthAction::Login {
            email,
            password,
            save_password,
        } => wrap(
            mvp::auth::login(mvp::auth::LoginOptions {
                email,
                password,
                save_password,
            })
            .await,
        ),
        AuthAction::Sso { profile, region } => wrap(
            mvp::sso::login(mvp::sso::SsoLoginOptions { profile, region }).await,
        ),
        AuthAction::Logout => wrap(mvp::auth::logout()),
        AuthAction::Whoami => wrap(mvp::auth::whoami().await),
    }
}

async fn dispatch_claude(
    model: Option<String>,
    group_id: Option<String>,
    workflow_run_id: Option<String>,
    model_pin: Option<String>,
    args: Vec<String>,
) -> ExitCode {
    // Validate the x-sc-* header flags BEFORE run() loads config or mints an
    // ephemeral key, so a malformed value costs zero network calls. Exit code 2
    // marks a usage/validation error, matching clap's own convention.
    let headers = match mvp::sc_headers::ScHeaders::validated(group_id, workflow_run_id, model_pin) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            return ExitCode::from(2);
        }
    };
    match mvp::claude_cmd::run(&args, model.as_deref(), &headers).await {
        Ok(code) => code,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            ExitCode::from(1)
        }
    }
}

async fn dispatch_codex(
    model: Option<String>,
    group_id: Option<String>,
    workflow_run_id: Option<String>,
    model_pin: Option<String>,
    args: Vec<String>,
) -> ExitCode {
    let headers = match mvp::sc_headers::ScHeaders::validated(group_id, workflow_run_id, model_pin) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            return ExitCode::from(2);
        }
    };
    match mvp::codex_cmd::run(&args, model.as_deref(), &headers).await {
        Ok(code) => code,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            ExitCode::from(1)
        }
    }
}

async fn dispatch_usage(action: UsageAction) -> ExitCode {
    match action {
        UsageAction::Show { since_days, limit } => wrap(mvp::usage::show(since_days, limit).await),
    }
}

async fn dispatch_admin(action: AdminAction) -> ExitCode {
    match action {
        AdminAction::User { action } => match action {
            AdminUserAction::Create {
                email,
                role,
                tenant,
                total_credit,
            } => wrap(
                mvp::admin::user_create(&email, &role, tenant.as_deref(), total_credit).await,
            ),
            AdminUserAction::List {
                role,
                tenant,
                limit,
            } => wrap(mvp::admin::user_list(role.as_deref(), tenant.as_deref(), limit).await),
            AdminUserAction::Show { user_id } => wrap(mvp::admin::user_show(&user_id).await),
            AdminUserAction::Delete { user_id } => wrap(mvp::admin::user_delete(&user_id).await),
            AdminUserAction::AssignTenant {
                user_id,
                tenant,
                new_role,
                total_credit,
            } => wrap(
                mvp::admin::user_assign_tenant(&user_id, &tenant, &new_role, total_credit).await,
            ),
            AdminUserAction::SetCredit {
                user_id,
                total,
                reset_used,
            } => wrap(mvp::admin::user_set_credit(&user_id, total, reset_used).await),
            AdminUserAction::SetRole { user_id, role } => {
                wrap(mvp::admin::user_set_role(&user_id, &role).await)
            }
        },
        AdminAction::Tenant { action } => match action {
            AdminTenantAction::Create {
                name,
                team_lead,
                team_lead_email,
                default_credit,
            } => wrap(
                mvp::admin::tenant_create(
                    &name,
                    team_lead.as_deref(),
                    team_lead_email.as_deref(),
                    default_credit,
                )
                .await,
            ),
            AdminTenantAction::List { limit } => wrap(mvp::admin::tenant_list(limit).await),
            AdminTenantAction::Show { tenant_id } => wrap(mvp::admin::tenant_show(&tenant_id).await),
            AdminTenantAction::Delete { tenant_id } => {
                wrap(mvp::admin::tenant_delete(&tenant_id).await)
            }
            AdminTenantAction::SetOwner {
                tenant_id,
                team_lead,
                team_lead_email,
            } => wrap(
                mvp::admin::tenant_set_owner(
                    &tenant_id,
                    team_lead.as_deref(),
                    team_lead_email.as_deref(),
                )
                .await,
            ),
            AdminTenantAction::Members { tenant_id } => {
                wrap(mvp::admin::tenant_members(&tenant_id).await)
            }
            AdminTenantAction::Usage {
                tenant_id,
                since_days,
            } => wrap(mvp::admin::tenant_usage(&tenant_id, since_days).await),
            AdminTenantAction::PoolBudget(action) => match action {
                AdminPoolBudgetAction::Set {
                    tenant_id,
                    limit_usd,
                    period,
                    status,
                } => wrap(
                    mvp::admin::tenant_pool_budget_set(
                        &tenant_id,
                        &limit_usd,
                        period.as_deref(),
                        &status,
                    )
                    .await,
                ),
                AdminPoolBudgetAction::Show { tenant_id, period } => wrap(
                    mvp::admin::tenant_pool_budget_show(&tenant_id, period.as_deref()).await,
                ),
            },
            AdminTenantAction::RoutingConfig(action) => match action {
                AdminRoutingConfigAction::Get { tenant_id, user } => wrap(
                    mvp::admin::routing_config_get(&tenant_id, user.as_deref()).await,
                ),
                AdminRoutingConfigAction::Set {
                    tenant_id,
                    file,
                    user,
                } => wrap(
                    mvp::admin::routing_config_set(&tenant_id, &file, user.as_deref()).await,
                ),
            },
        },
        AdminAction::Usage { action } => match action {
            AdminUsageAction::Show {
                tenant,
                user,
                since,
                until,
                limit,
            } => wrap(
                mvp::admin::usage_logs(
                    tenant.as_deref(),
                    user.as_deref(),
                    since.as_deref(),
                    until.as_deref(),
                    limit,
                )
                .await,
            ),
        },
    }
}

async fn dispatch_team_lead(action: TeamLeadAction) -> ExitCode {
    match action {
        TeamLeadAction::Tenant { action } => match action {
            TeamLeadTenantAction::Create {
                name,
                default_credit,
            } => wrap(mvp::team_lead::tenant_create(&name, default_credit).await),
            TeamLeadTenantAction::List => wrap(mvp::team_lead::tenant_list().await),
            TeamLeadTenantAction::Show { tenant_id } => {
                wrap(mvp::team_lead::tenant_show(&tenant_id).await)
            }
            TeamLeadTenantAction::Members { tenant_id } => {
                wrap(mvp::team_lead::tenant_members(&tenant_id).await)
            }
            TeamLeadTenantAction::Usage {
                tenant_id,
                since_days,
            } => wrap(mvp::team_lead::tenant_usage(&tenant_id, since_days).await),
        },
    }
}

async fn dispatch_ui(action: &str) -> ExitCode {
    let cfg = match crate::config::AppConfig::load(None) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            return ExitCode::from(1);
        }
    };
    let cmd = match action {
        "open" => UiCommand::Open,
        "url" => UiCommand::Url,
        other => {
            eprintln!("[ERROR] Unknown ui action: {other}. Use `open` or `url`.");
            return ExitCode::from(1);
        }
    };
    match commands::ui::run(cmd, &cfg).await {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            ExitCode::from(1)
        }
    }
}

async fn dispatch_api_key(action: ApiKeyAction) -> ExitCode {
    match action {
        ApiKeyAction::Create {
            name,
            scopes,
            expires_days,
        } => wrap(mvp::api_keys::create(name, scopes, expires_days).await),
        ApiKeyAction::List { include_revoked } => {
            wrap(mvp::api_keys::list(include_revoked).await)
        }
        ApiKeyAction::Revoke { key_id } => wrap(mvp::api_keys::revoke(key_id).await),
        ApiKeyAction::AdminList { include_revoked } => {
            wrap(mvp::api_keys::admin_list_all(include_revoked).await)
        }
        ApiKeyAction::AdminRevoke { key_id } => {
            wrap(mvp::api_keys::admin_revoke(key_id).await)
        }
    }
}

async fn run_pipe() -> ExitCode {
    match commands::pipe::run(OutputFormat::Human, None).await {
        Ok(code) => code,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            ExitCode::from(1)
        }
    }
}

fn wrap(res: anyhow::Result<()>) -> ExitCode {
    match res {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("[ERROR] {e}");
            ExitCode::from(1)
        }
    }
}

// ------------------------------------------------------------------
// Error types / OutputFormat (used by existing pipe module)
// ------------------------------------------------------------------
#[derive(Debug, Clone, Copy)]
pub enum OutputFormat {
    Human,
    #[allow(dead_code)]
    Json,
}

#[derive(Debug)]
pub enum CliError {
    AuthExpired(String),
    PermissionDenied(String),
    NotFound(String),
    RateLimited(String),
    ServerError(String),
    NetworkError(String),
    ConfigError(String),
    /// HTTP 402: personal or tenant-pool budget / per-model quota exhausted.
    BudgetExceeded(String),
    General(String),
}

impl std::fmt::Display for CliError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CliError::AuthExpired(s) => write!(f, "{s}"),
            CliError::PermissionDenied(s) => write!(f, "{s}"),
            CliError::NotFound(s) => write!(f, "{s}"),
            CliError::RateLimited(s) => write!(f, "{s}"),
            CliError::ServerError(s) => write!(f, "{s}"),
            CliError::NetworkError(s) => write!(f, "{s}"),
            CliError::ConfigError(s) => write!(f, "{s}"),
            CliError::BudgetExceeded(s) => write!(f, "{s}"),
            CliError::General(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for CliError {}
