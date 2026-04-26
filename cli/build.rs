//! Build script to embed enterprise policy at compile time
//!
//! policy.toml が存在する場合、その内容を Rust コードに埋め込む。
//! cargo build --features enterprise でエンタープライズビルドを生成。

use std::env;
use std::fs;
use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=policy.toml");
    println!("cargo:rerun-if-changed=policy.example.toml");

    let out_dir = env::var("OUT_DIR").unwrap();
    let dest_path = Path::new(&out_dir).join("policy.rs");

    let policy_content = if Path::new("policy.toml").exists() {
        // policy.toml が存在する場合はパース
        match fs::read_to_string("policy.toml") {
            Ok(content) => generate_policy_code(&content),
            Err(e) => {
                eprintln!("Warning: Failed to read policy.toml: {}", e);
                generate_default_policy_code()
            }
        }
    } else {
        // policy.toml が存在しない場合はデフォルト
        generate_default_policy_code()
    };

    fs::write(&dest_path, policy_content).unwrap();
}

fn generate_policy_code(toml_content: &str) -> String {
    // シンプルなTOMLパーサー（tomlクレートを避けてビルド依存を最小化）
    let mut org_name = String::from("Unknown");
    let mut disable_debug = false;
    let mut allowed_env_vars: Vec<String> = Vec::new();
    let mut allowed_model_patterns: Vec<String> = Vec::new();
    let mut audit_endpoint = String::new();
    let mut fixed_api_endpoint = String::new();
    let mut fixed_default_model = String::new();
    let mut use_bedrock = false;
    let mut experimental_agent_teams = false;

    let mut in_policy_section = false;
    let mut in_enterprise_policy_section = false;
    let mut in_array_env = false;
    let mut in_array_models = false;

    for line in toml_content.lines() {
        let line = line.trim();

        if line.starts_with('[') {
            in_policy_section = line == "[policy]";
            in_enterprise_policy_section = line == "[enterprise_policy]";
            in_array_env = false;
            in_array_models = false;
            continue;
        }

        if in_enterprise_policy_section {
            if line.starts_with("use_bedrock") {
                use_bedrock = extract_bool_value(line);
            } else if line.starts_with("experimental_agent_teams") {
                experimental_agent_teams = extract_bool_value(line);
            }
            continue;
        }

        if !in_policy_section {
            continue;
        }

        if line.starts_with("organization_name") {
            if let Some(val) = extract_string_value(line) {
                org_name = val;
            }
        } else if line.starts_with("disable_debug") {
            disable_debug = extract_bool_value(line);
        } else if line.starts_with("allowed_env_vars") {
            in_array_env = true;
            in_array_models = false;
        } else if line.starts_with("allowed_model_patterns") {
            in_array_models = true;
            in_array_env = false;
        } else if line.starts_with("audit_endpoint") {
            if let Some(val) = extract_string_value(line) {
                audit_endpoint = val;
            }
        } else if line.starts_with("fixed_api_endpoint") {
            if let Some(val) = extract_string_value(line) {
                fixed_api_endpoint = val;
            }
        } else if line.starts_with("fixed_default_model") {
            if let Some(val) = extract_string_value(line) {
                fixed_default_model = val;
            }
        } else if in_array_env && line.starts_with('"') {
            if let Some(val) = extract_array_string_value(line) {
                allowed_env_vars.push(val);
            }
        } else if in_array_models && line.starts_with('"') {
            if let Some(val) = extract_array_string_value(line) {
                allowed_model_patterns.push(val);
            }
        }
    }

    let env_vars_code = if allowed_env_vars.is_empty() {
        "&[]".to_string()
    } else {
        format!(
            "&[{}]",
            allowed_env_vars
                .iter()
                .map(|s| format!("\"{}\"", s))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };

    let model_patterns_code = if allowed_model_patterns.is_empty() {
        "&[]".to_string()
    } else {
        format!(
            "&[{}]",
            allowed_model_patterns
                .iter()
                .map(|s| format!("\"{}\"", s))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };

    let audit_code = if audit_endpoint.is_empty() {
        "None".to_string()
    } else {
        format!("Some(\"{}\")", audit_endpoint)
    };

    let api_endpoint_code = if fixed_api_endpoint.is_empty() {
        "None".to_string()
    } else {
        format!("Some(\"{}\")", fixed_api_endpoint)
    };

    let default_model_code = if fixed_default_model.is_empty() {
        "None".to_string()
    } else {
        format!("Some(\"{}\")", fixed_default_model)
    };

    format!(
        r##"
/// Enterprise Policy (embedded at build time)
#[derive(Debug)]
pub struct EnterprisePolicy {{
    pub organization_name: &'static str,
    pub disable_debug: bool,
    pub allowed_env_vars: &'static [&'static str],
    pub allowed_model_patterns: &'static [&'static str],
    pub audit_endpoint: Option<&'static str>,
    pub fixed_api_endpoint: Option<&'static str>,
    pub fixed_default_model: Option<&'static str>,
    pub use_bedrock: bool,
    pub experimental_agent_teams: bool,
}}

pub const POLICY: EnterprisePolicy = EnterprisePolicy {{
    organization_name: "{}",
    disable_debug: {},
    allowed_env_vars: {},
    allowed_model_patterns: {},
    audit_endpoint: {},
    fixed_api_endpoint: {},
    fixed_default_model: {},
    use_bedrock: {},
    experimental_agent_teams: {},
}};

impl EnterprisePolicy {{
    pub fn is_env_var_allowed(&self, var: &str) -> bool {{
        if self.allowed_env_vars.is_empty() {{
            return true;
        }}
        self.allowed_env_vars.contains(&var)
    }}

    pub fn is_model_allowed(&self, model: &str) -> bool {{
        if self.allowed_model_patterns.is_empty() {{
            return true;
        }}
        self.allowed_model_patterns.iter().any(|pattern| {{
            if pattern.ends_with('*') {{
                model.starts_with(&pattern[..pattern.len() - 1])
            }} else {{
                model == *pattern
            }}
        }})
    }}
}}
"##,
        org_name,
        disable_debug,
        env_vars_code,
        model_patterns_code,
        audit_code,
        api_endpoint_code,
        default_model_code,
        use_bedrock,
        experimental_agent_teams
    )
}

fn generate_default_policy_code() -> String {
    r##"
/// Default Policy (no restrictions)
#[derive(Debug)]
pub struct EnterprisePolicy {
    pub organization_name: &'static str,
    pub disable_debug: bool,
    pub allowed_env_vars: &'static [&'static str],
    pub allowed_model_patterns: &'static [&'static str],
    pub audit_endpoint: Option<&'static str>,
    pub fixed_api_endpoint: Option<&'static str>,
    pub fixed_default_model: Option<&'static str>,
    pub use_bedrock: bool,
    pub experimental_agent_teams: bool,
}

pub const POLICY: EnterprisePolicy = EnterprisePolicy {
    organization_name: "Default",
    disable_debug: false,
    allowed_env_vars: &[],
    allowed_model_patterns: &[],
    audit_endpoint: None,
    fixed_api_endpoint: None,
    fixed_default_model: None,
    use_bedrock: false,
    experimental_agent_teams: false,
};

impl EnterprisePolicy {
    pub fn is_env_var_allowed(&self, _var: &str) -> bool {
        true
    }

    pub fn is_model_allowed(&self, _model: &str) -> bool {
        true
    }
}
"##
    .to_string()
}

fn extract_string_value(line: &str) -> Option<String> {
    line.split('=')
        .nth(1)
        .and_then(|s| s.trim().strip_prefix('"'))
        .and_then(|s| s.strip_suffix('"'))
        .map(|s| s.to_string())
}

fn extract_bool_value(line: &str) -> bool {
    line.split('=')
        .nth(1)
        .map(|s| s.trim() == "true")
        .unwrap_or(false)
}

fn extract_array_string_value(line: &str) -> Option<String> {
    line.trim()
        .strip_prefix('"')
        .and_then(|s| s.strip_suffix('"').or_else(|| s.strip_suffix("\",")))
        .map(|s| s.to_string())
}
