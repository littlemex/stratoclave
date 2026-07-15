//! Interactive chat mode
//!
//! stratoclave (without arguments) enters interactive mode

use std::path::PathBuf;
use std::process::ExitCode;
use rustyline::error::ReadlineError;
use rustyline::DefaultEditor;

use crate::auth;
use crate::client::{ApiClient, ChatTurn};
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

    let api_client = ApiClient::new(app_config, token)?;

    // Cap on retained turns (user+assistant entries). The whole history is
    // re-sent every turn (the Messages API is stateless), so without a bound a
    // long session grows unboundedly, eventually exceeds the model context and
    // then EVERY turn fails identically. We trim oldest pairs first, keeping
    // recent context. `/clear` resets it explicitly.
    const MAX_HISTORY_TURNS: usize = 40;

    // Start interactive loop
    println!("Stratoclave Interactive Mode");
    println!("Type your message and press Enter. Type 'exit' or press Ctrl+D to quit.");
    println!("Commands: 'exit'/'quit' to leave, '/clear' to reset the conversation.");
    println!();

    let mut rl = DefaultEditor::new()
        .map_err(|e| CliError::General(format!("Failed to initialize readline: {}", e)))?;

    // The Anthropic Messages API is stateless, so the client owns the running
    // history. Each turn we append the user message, send the whole
    // conversation, and append the assistant reply on success — this is what
    // keeps follow-up questions ("double that") in context.
    let mut history: Vec<ChatTurn> = Vec::new();

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
                if input.eq_ignore_ascii_case("/clear") {
                    history.clear();
                    println!("[Conversation cleared]");
                    let _ = rl.add_history_entry(input);
                    continue;
                }

                // Add to history
                let _ = rl.add_history_entry(input);

                // Send the whole conversation so context is preserved. Append
                // the user turn first; only commit the assistant turn on
                // success so a failed turn does not poison later context.
                history.push(ChatTurn::user(input));
                match api_client.send_turns(&history).await {
                    Ok(response) => {
                        println!();
                        println!("{}", response.message);
                        if !response.complete {
                            // Interactive users benefit from seeing a partial
                            // answer, unlike pipe mode — but flag it clearly.
                            eprintln!(
                                "\n[WARN] Response was incomplete ({}).",
                                response.reason.as_deref().unwrap_or("unknown")
                            );
                        }
                        println!();
                        // Never commit an EMPTY assistant turn (Fable review
                        // Finding 3): the Messages API rejects a message with
                        // empty content, so an empty turn would 400 EVERY
                        // subsequent request — and the error handler's pop only
                        // removes the new user turn, leaving the poison in place
                        // forever. Drop the just-added user turn instead so the
                        // history stays a clean, even, alternating sequence.
                        if response.message.is_empty() {
                            history.pop();
                            eprintln!("[WARN] Empty response; turn not added to history.");
                        } else {
                            history.push(ChatTurn::assistant(response.message));
                        }
                        // Trim oldest PAIRS so history can't grow without bound
                        // and always stays even/alternating (a user-first,
                        // assistant-rejecting API needs whole-pair drops).
                        if history.len() > MAX_HISTORY_TURNS {
                            let mut drop = history.len() - MAX_HISTORY_TURNS;
                            if drop % 2 != 0 {
                                drop += 1; // round up to a whole pair
                            }
                            drop = drop.min(history.len());
                            history.drain(0..drop);
                        }
                        debug_assert!(history.len() % 2 == 0, "history must stay pairwise");
                    }
                    Err(CliError::AuthExpired(msg)) => {
                        eprintln!("[ERROR] {}", msg);
                        eprintln!("Please restart and authenticate again.");
                        return Ok(ExitCode::from(3));
                    }
                    Err(e) => {
                        eprintln!("[ERROR] {}", e);
                        eprintln!();
                        // Drop the user turn that failed so it doesn't desync
                        // the alternating user/assistant sequence.
                        history.pop();
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
