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
            // Pipe mode: authenticate and send message
            let token = auth::authenticate(&app_config)
                .await
                .map_err(|e| CliError::AuthExpired(format!("Authentication failed: {}", e)))?;

            let api_client = ApiClient::new(app_config, token)?;

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
