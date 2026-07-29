use anyhow::Context;
use clap::CommandFactory;
use clap::FromArgMatches;
use clap::Parser;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_exec::Cli as ExecCli;
use codex_tui::AppExitInfo;
use codex_tui::Cli as TuiCli;
use codex_tui::ExitReason;
use codex_utils_cli::CliConfigOverrides;
use coomi_provider_adapters::ConformanceStatus;
use coomi_provider_adapters::HttpProviderTransport;
use coomi_provider_adapters::ProviderRegistry;
use coomi_provider_adapters::run_basic_conformance;
use coomi_provider_adapters::run_full_conformance;
use std::path::Path;
use std::path::PathBuf;

const COOMI_HOME_ENV: &str = "COOMI_HOME";
const CODEX_HOME_ENV: &str = "CODEX_HOME";
const CONFIG_HELP: &str = "Override a configuration value that would otherwise be loaded from `$COOMI_HOME/config.toml`. Use a dotted path for nested values; values are parsed as TOML when possible";
const PROFILE_HELP: &str =
    "Layer `$COOMI_HOME/<name>.config.toml` on top of the base user configuration";
const STRICT_CONFIG_HELP: &str =
    "Error out when config.toml contains fields that this Coomi version does not recognize";

/// Coomi terminal AI coding assistant.
///
/// With no command, Coomi starts the interactive terminal UI.
#[derive(Debug, Parser)]
#[command(
    author,
    version,
    name = "coomi",
    bin_name = "coomi",
    subcommand_negates_reqs = true,
    override_usage = "coomi [OPTIONS] [PROMPT]\n       coomi [OPTIONS] <COMMAND> [ARGS]"
)]
struct CoomiCli {
    #[command(flatten)]
    config_overrides: CliConfigOverrides,

    #[command(flatten)]
    interactive: TuiCli,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, clap::Subcommand)]
enum Command {
    /// Run Coomi non-interactively.
    #[command(visible_alias = "e")]
    Exec(Box<ExecCli>),

    /// Resume a previous interactive session.
    Resume(ResumeCommand),

    /// Inspect and test configured model providers.
    Provider(ProviderCommand),
}

#[derive(Debug, clap::Args)]
struct ResumeCommand {
    /// Session id or name. If omitted, show the session picker.
    #[arg(value_name = "SESSION_ID")]
    session_id: Option<String>,

    /// Continue the most recent session without showing the picker.
    #[arg(long, default_value_t = false)]
    last: bool,

    /// Show sessions from all working directories.
    #[arg(long, default_value_t = false)]
    all: bool,

    /// Include sessions created by non-interactive runs.
    #[arg(long, default_value_t = false)]
    include_non_interactive: bool,
}

#[derive(Debug, clap::Args)]
struct ProviderCommand {
    #[command(subcommand)]
    command: ProviderSubcommand,
}

#[derive(Debug, clap::Subcommand)]
enum ProviderSubcommand {
    /// Run a safe native-tool compatibility probe.
    Test(ProviderTestCommand),
}

#[derive(Debug, clap::Args)]
struct ProviderTestCommand {
    /// Provider id from providers.json. Defaults to the active provider.
    #[arg(long)]
    provider: Option<String>,

    /// Override the configured model for this probe.
    #[arg(long)]
    model: Option<String>,

    /// Use a provider registry other than `$COOMI_HOME/config/providers.json`.
    #[arg(long = "registry", value_name = "FILE")]
    registry: Option<PathBuf>,

    /// Print the conformance report as JSON.
    #[arg(long, default_value_t = false)]
    json: bool,

    /// Run the extended C04-C07 and C10 live probes.
    #[arg(long, default_value_t = false)]
    full: bool,
}

fn main() -> anyhow::Result<()> {
    let coomi_home = resolve_coomi_home()?;
    install_codex_compatibility_environment(&coomi_home);

    arg0_dispatch_or_else(|arg0_paths| async move { run_cli(arg0_paths, coomi_home).await })
}

async fn run_cli(arg0_paths: Arg0DispatchPaths, coomi_home: PathBuf) -> anyhow::Result<()> {
    let CoomiCli {
        config_overrides,
        mut interactive,
        command,
    } = parse_cli();

    match command {
        None => {
            interactive
                .config_overrides
                .prepend_root_overrides(config_overrides);
            let exit_info = codex_tui::run_main(
                interactive,
                arg0_paths,
                codex_config::LoaderOverrides::default(),
                None,
            )
            .await?;
            handle_tui_exit(exit_info)
        }
        Some(Command::Exec(mut exec_cli)) => {
            exec_cli
                .shared
                .inherit_exec_root_options(&interactive.shared.into_inner());
            exec_cli.strict_config |= interactive.strict_config;
            exec_cli
                .config_overrides
                .prepend_root_overrides(config_overrides);
            codex_exec::run_main(*exec_cli, arg0_paths).await
        }
        Some(Command::Resume(resume)) => {
            if resume.last && resume.session_id.is_some() {
                anyhow::bail!("--last cannot be used with SESSION_ID");
            }
            interactive.resume_picker = resume.session_id.is_none() && !resume.last;
            interactive.resume_last = resume.last;
            interactive.resume_session_id = resume.session_id;
            interactive.resume_show_all = resume.all;
            interactive.resume_include_non_interactive = resume.include_non_interactive;
            interactive
                .config_overrides
                .prepend_root_overrides(config_overrides);
            let exit_info = codex_tui::run_main(
                interactive,
                arg0_paths,
                codex_config::LoaderOverrides::default(),
                None,
            )
            .await?;
            handle_tui_exit(exit_info)
        }
        Some(Command::Provider(provider)) => run_provider_command(provider, &coomi_home).await,
    }
}

async fn run_provider_command(command: ProviderCommand, coomi_home: &Path) -> anyhow::Result<()> {
    match command.command {
        ProviderSubcommand::Test(test) => {
            let config_path = test
                .registry
                .unwrap_or_else(|| coomi_home.join("config").join("providers.json"));
            let registry = ProviderRegistry::load(&config_path)?;
            let provider = match test.provider.as_deref() {
                Some(id) => registry
                    .provider(id)
                    .with_context(|| format!("provider `{id}` was not found"))?,
                None => registry.active_provider()?,
            };
            let mut provider = provider.clone();
            if let Some(model) = test.model {
                provider.model = model;
            }
            let model = provider.model.clone();
            let transport = HttpProviderTransport::new(provider)?;
            let report = if test.full {
                run_full_conformance(&transport, &model).await?
            } else {
                run_basic_conformance(&transport, &model).await?
            };

            if test.json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!(
                    "Provider: {}  Model: {}  Protocol: {:?}",
                    report.provider_id, report.model, report.protocol
                );
                for result in &report.results {
                    let status = match result.status {
                        ConformanceStatus::Passed => "PASS",
                        ConformanceStatus::Failed => "FAIL",
                        ConformanceStatus::NotRun => "SKIP",
                    };
                    println!("{status:4} {:?}: {}", result.case, result.detail);
                }
                println!(
                    "Usage: input={} cached={} output={} total={}",
                    report.input_tokens,
                    report.cached_input_tokens,
                    report.output_tokens,
                    report.total_tokens
                );
            }

            if !report.required_tool_loop_passed() {
                anyhow::bail!("provider failed required C01-C03 tool-loop conformance");
            }
            Ok(())
        }
    }
}

fn parse_cli() -> CoomiCli {
    let matches = coomi_command().get_matches();
    CoomiCli::from_arg_matches(&matches).unwrap_or_else(|error| error.exit())
}

fn coomi_command() -> clap::Command {
    brand_runtime_args(CoomiCli::command())
        .name("coomi")
        .bin_name("coomi")
        .mut_subcommand("exec", |command| {
            command
                .bin_name("coomi exec")
                .override_usage(
                    "coomi exec [OPTIONS] [PROMPT]\n       coomi exec [OPTIONS] <COMMAND> [ARGS]",
                )
                .mut_arg("skip_git_repo_check", |arg| {
                    arg.help("Allow running Coomi outside a Git repository")
                })
                .mut_arg("ignore_user_config", |arg| {
                    arg.help("Do not load `$COOMI_HOME/config.toml`; authentication still uses the Coomi home directory")
                })
                .mut_arg("strict_config", |arg| {
                    arg.help(STRICT_CONFIG_HELP).long_help(STRICT_CONFIG_HELP)
                })
                .mut_arg("config_profile_v2", |arg| {
                    arg.help(PROFILE_HELP).long_help(PROFILE_HELP)
                })
        })
}

fn brand_runtime_args(command: clap::Command) -> clap::Command {
    command
        .mut_arg("raw_overrides", |arg| {
            arg.help(CONFIG_HELP).long_help(CONFIG_HELP)
        })
        .mut_arg("strict_config", |arg| {
            arg.help(STRICT_CONFIG_HELP).long_help(STRICT_CONFIG_HELP)
        })
        .mut_arg("config_profile_v2", |arg| {
            arg.help(PROFILE_HELP).long_help(PROFILE_HELP)
        })
}

fn resolve_coomi_home() -> anyhow::Result<PathBuf> {
    resolve_coomi_home_with(
        std::env::var_os(COOMI_HOME_ENV).map(PathBuf::from),
        dirs::home_dir(),
    )
}

fn resolve_coomi_home_with(
    configured_home: Option<PathBuf>,
    user_home: Option<PathBuf>,
) -> anyhow::Result<PathBuf> {
    let home = match configured_home {
        Some(path) if path.as_os_str().is_empty() => {
            anyhow::bail!("{COOMI_HOME_ENV} must not be empty")
        }
        Some(path) => path,
        None => user_home
            .context("could not determine the user home directory")?
            .join(".coomi"),
    };

    std::fs::create_dir_all(&home)
        .with_context(|| format!("failed to create Coomi home at {}", home.display()))?;
    home.canonicalize()
        .with_context(|| format!("failed to canonicalize Coomi home at {}", home.display()))
}

fn install_codex_compatibility_environment(coomi_home: &Path) {
    // SAFETY: this runs before arg0 dispatch creates the Tokio runtime or any
    // worker threads. Codex crates currently resolve their storage root through
    // CODEX_HOME; Coomi owns the user-facing COOMI_HOME contract.
    unsafe {
        std::env::set_var(CODEX_HOME_ENV, coomi_home);
    }
}

fn handle_tui_exit(exit_info: AppExitInfo) -> anyhow::Result<()> {
    if !exit_info.token_usage.is_zero() {
        println!("{}", exit_info.token_usage);
    }
    if let Some(resume_hint) = exit_info.resume_hint {
        println!(
            "To continue this session, run {}",
            coomi_command_hint(&resume_hint)
        );
    }

    match exit_info.exit_reason {
        ExitReason::UserRequested => Ok(()),
        ExitReason::Fatal(message) => anyhow::bail!(message),
    }
}

fn coomi_command_hint(hint: &str) -> String {
    hint.strip_prefix("codex ")
        .map_or_else(|| hint.to_string(), |args| format!("coomi {args}"))
}

#[cfg(test)]
mod tests {
    use super::coomi_command;
    use super::coomi_command_hint;
    use super::resolve_coomi_home_with;
    use tempfile::TempDir;

    #[test]
    fn defaults_to_dot_coomi_under_user_home() {
        let user_home = TempDir::new().expect("temporary user home");
        let resolved = resolve_coomi_home_with(None, Some(user_home.path().to_path_buf()))
            .expect("resolve Coomi home");
        let expected = user_home
            .path()
            .join(".coomi")
            .canonicalize()
            .expect("canonical default Coomi home");
        assert_eq!(resolved, expected);
    }

    #[test]
    fn honors_explicit_coomi_home() {
        let configured = TempDir::new().expect("temporary configured home");
        let resolved = resolve_coomi_home_with(
            Some(configured.path().to_path_buf()),
            /*user_home*/ None,
        )
        .expect("resolve explicit Coomi home");
        assert_eq!(
            resolved,
            configured
                .path()
                .canonicalize()
                .expect("canonical configured Coomi home")
        );
    }

    #[test]
    fn rewrites_codex_resume_hint() {
        assert_eq!(coomi_command_hint("codex resume 1234"), "coomi resume 1234");
    }

    #[test]
    fn renders_coomi_version_and_exec_usage() {
        let version = coomi_command().render_version();
        assert_eq!(version.trim(), "coomi 1.4.0");

        let mut command = coomi_command();
        let exec = command
            .find_subcommand_mut("exec")
            .expect("exec subcommand");
        let help = exec.render_long_help().to_string();
        assert!(help.contains("Usage: coomi exec"));
        assert!(!help.contains("Usage: codex exec"));
        assert!(!help.to_ascii_lowercase().contains("codex"));

        let root_help = coomi_command().render_long_help().to_string();
        assert!(!root_help.to_ascii_lowercase().contains("codex"));

        let mut command = coomi_command();
        let provider_test = command
            .find_subcommand_mut("provider")
            .expect("provider subcommand")
            .find_subcommand_mut("test")
            .expect("provider test subcommand");
        let provider_help = provider_test.render_long_help().to_string();
        assert!(provider_help.contains("--registry"));
        assert!(!provider_help.to_ascii_lowercase().contains("codex"));
    }
}
