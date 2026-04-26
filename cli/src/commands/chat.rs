//! Interactive chat mode
//!
//! stratoclave (without arguments) enters interactive mode

use std::path::PathBuf;
use std::process::ExitCode;
use rustyline::error::ReadlineError;
use rustyline::DefaultEditor;

use crate::auth;
use crate::client::ApiClient;
use crate::config;
use crate::{CliError, OutputFormat};

pub async fn run(
    _output_format: OutputFormat,
    config_path: Option<PathBuf>,
) -> Result<ExitCode, CliError> {
    let app_config = config::AppConfig::load(config_path)
        .map_err(|e| CliError::ConfigError(format!("Failed to load config: {}", e)))?;

    // Authenticate once
    let token = auth::authenticate(&app_config)
        .await
        .map_err(|e| CliError::AuthExpired(format!("Authentication failed: {}", e)))?;

    let api_client = ApiClient::new(app_config, token);

    // Start interactive loop
    println!("Stratoclave Interactive Mode");
    println!("Type your message and press Enter. Type 'exit' or press Ctrl+D to quit.");
    println!();

    let mut rl = DefaultEditor::new()
        .map_err(|e| CliError::General(format!("Failed to initialize readline: {}", e)))?;

    loop {
        let readline = rl.readline("> ");
        match readline {
            Ok(line) => {
                let input = line.trim();

                // Exit commands
                if input.is_empty() {
                    continue;
                }
                if input.eq_ignore_ascii_case("exit") || input.eq_ignore_ascii_case("quit") {
                    println!("Goodbye!");
                    break;
                }

                // Add to history
                let _ = rl.add_history_entry(input);

                // Send message
                match api_client.converse(input).await {
                    Ok(response) => {
                        println!();
                        println!("{}", response.message);
                        println!();
                    }
                    Err(CliError::AuthExpired(msg)) => {
                        eprintln!("[ERROR] {}", msg);
                        eprintln!("Please restart and authenticate again.");
                        return Ok(ExitCode::from(3));
                    }
                    Err(e) => {
                        eprintln!("[ERROR] {}", e);
                        eprintln!();
                    }
                }
            }
            Err(ReadlineError::Interrupted) => {
                // Ctrl+C
                println!("^C");
                continue;
            }
            Err(ReadlineError::Eof) => {
                // Ctrl+D
                println!("Goodbye!");
                break;
            }
            Err(err) => {
                eprintln!("[ERROR] Readline error: {}", err);
                return Ok(ExitCode::from(1));
            }
        }
    }

    Ok(ExitCode::SUCCESS)
}
