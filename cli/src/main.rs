//! Stratoclave CLI (Phase 2, clap derive 一本化).
//!
//! コマンドツリー:
//!   stratoclave
//!   ├── auth { login | logout | whoami }
//!   ├── claude [--model X] -- [claude-args]
//!   ├── usage show [--since-days N] [--limit M]
//!   ├── admin
//!   │   ├── user { create | list | show | delete | assign-tenant | set-credit }
//!   │   ├── tenant { create | list | show | delete | set-owner | members | usage }
//!   │   └── usage show [--tenant T] [--user U] [--since X] [--until Y] [--limit N]
//!   ├── team-lead
//!   │   └── tenant { create | list | show | members | usage }
//!   └── ui { open | url }
//!
//! TTY でない stdin があり、かつ引数が無い場合はパイプモード (既存 commands::pipe)。
//! 既存の手パース実装 (login-mvp など) は撤廃。

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
        /// Extra args passed to claude
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
    /// Revoke an API key by its key_hash (see the list command)
    Revoke { key_hash: String },
    /// Admin-only: list every API key in the system
    #[command(name = "admin-list")]
    AdminList {
        #[arg(long)]
        include_revoked: bool,
    },
    /// Admin-only: revoke any API key by key_hash
    #[command(name = "admin-revoke")]
    AdminRevoke { key_hash: String },
}

#[derive(Debug, Subcommand)]
enum AuthAction {
    /// Login with email + password (Cognito User/Pass)
    Login {
        #[arg(long)]
        email: Option<String>,
        #[arg(long)]
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
        /// email (CLI が admin/users list で sub に解決)
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
    // 非 TTY stdin & 引数なし → pipe モード (既存挙動維持)
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 1 && !client::is_stdin_tty() {
        return run_pipe().await;
    }

    let cli = match Cli::try_parse() {
        Ok(c) => c,
        Err(e) => {
            // clap の自動 help / version / error をそのまま出力
            e.print().ok();
            return ExitCode::from(e.exit_code() as u8);
        }
    };

    match cli.command {
        Some(Commands::Auth { action }) => dispatch_auth(action).await,
        Some(Commands::Claude { model, args }) => dispatch_claude(model, args).await,
        Some(Commands::Usage { action }) => dispatch_usage(action).await,
        Some(Commands::Admin { action }) => dispatch_admin(action).await,
        Some(Commands::TeamLead { action }) => dispatch_team_lead(action).await,
        Some(Commands::Ui { action }) => dispatch_ui(&action).await,
        Some(Commands::ApiKey { action }) => dispatch_api_key(action).await,
        Some(Commands::Setup {
            api_endpoint,
            force,
            dry_run,
        }) => wrap(commands::setup::run(api_endpoint, force, dry_run).await),
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

async fn dispatch_claude(model: Option<String>, args: Vec<String>) -> ExitCode {
    match mvp::claude_cmd::run(&args, model.as_deref()).await {
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
        ApiKeyAction::Revoke { key_hash } => wrap(mvp::api_keys::revoke(key_hash).await),
        ApiKeyAction::AdminList { include_revoked } => {
            wrap(mvp::api_keys::admin_list_all(include_revoked).await)
        }
        ApiKeyAction::AdminRevoke { key_hash } => {
            wrap(mvp::api_keys::admin_revoke(key_hash).await)
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
            CliError::General(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for CliError {}
