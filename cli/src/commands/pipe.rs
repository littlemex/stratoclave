//! Pipe mode (backward compatible)
//!
//! echo "hello" | stratoclave

use std::path::PathBuf;
use std::process::ExitCode;

use crate::auth;
use crate::client::{self, ApiClient};
use crate::config;
use crate::{CliError, OutputFormat};

pub async fn run(
    _output_format: OutputFormat,
    config_path: Option<PathBuf>,
) -> Result<ExitCode, CliError> {
    let app_config = config::AppConfig::load(config_path.clone())
        .map_err(|e| CliError::ConfigError(format!("Failed to load config: {}", e)))?;

    let stdin_message = client::read_stdin_message()
        .map_err(|e| CliError::General(format!("Failed to read stdin: {}", e)))?;

    match stdin_message {
        Some(message) => {
            // Read the x-sc-* attribution/pin env vars (STRATOCLAVE_GROUP_ID /
            // _WORKFLOW_RUN_ID / _MODEL_PIN) through the shared validated
            // ScHeaders — pipe has no flag surface. Fail FAST here, before auth
            // and the network, so a malformed id can't be mistaken for a
            // network/stream failure later.
            let sc_headers = crate::mvp::sc_headers::ScHeaders::from_env().map_err(|e| {
                CliError::General(format!("Invalid STRATOCLAVE_* attribution env var: {e}"))
            })?;
            if let Some(pin) = sc_headers.model_pin() {
                eprintln!(
                    "[INFO] x-sc-model-pin={pin} — server-side pin overrides the configured model."
                );
            }

            // Pipe mode: authenticate and send message
            let token = auth::authenticate(&app_config)
                .await
                .map_err(|e| CliError::AuthExpired(format!("Authentication failed: {}", e)))?;

            let api_client = ApiClient::new(app_config, token, sc_headers)?;

            eprintln!("[INFO] Sending message...");
            let response = api_client.converse(&message).await?;

            // Pipe stdout is data a downstream consumer trusts: a partial /
            // truncated response must NOT be emitted as if complete. Print the
            // partial to stderr for the human and exit nonzero.
            if !response.complete {
                eprintln!(
                    "[ERROR] Incomplete response ({}); received {} chars, not emitting to stdout.",
                    response.reason.as_deref().unwrap_or("unknown"),
                    response.message.chars().count()
                );
                eprintln!("--- partial response (stderr) ---");
                eprintln!("{}", response.message);
                return Ok(ExitCode::from(1));
            }

            // Output response to stdout
            println!("{}", response.message);

            Ok(ExitCode::SUCCESS)
        }
        None => {
            // No pipe input: enter interactive chat mode
            super::chat::run(_output_format, config_path).await
        }
    }
}
