//! Minimal OSC 52 clipboard writer — works across any terminal that supports
//! the Operating System Command 52 escape sequence (iTerm2, kitty, WezTerm,
//! Windows Terminal, tmux ≥ 3.3, etc.) without requiring a system clipboard
//! binary like pbcopy or xclip.

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use std::io::{self, Write};

/// Copy `text` to the system clipboard via OSC 52.
///
/// This writes an escape sequence directly to stdout. The terminal decodes the
/// base64 payload and places it on the clipboard. Works inside SSH sessions and
/// tmux where native clipboard tools are unavailable.
pub fn copy_to_clipboard(text: &str) -> io::Result<()> {
    if text.is_empty() {
        return Ok(());
    }
    let encoded = BASE64.encode(text.as_bytes());
    // OSC 52: \x1b]52;c;<base64>\x1b\\
    // c = clipboard selection (c = default/clipboard)
    let mut stdout = io::stdout().lock();
    write!(stdout, "\x1b]52;c;{}\x1b\\", encoded)?;
    stdout.flush()
}

/// Attempt to copy using the best available backend.
/// Falls back through: OSC 52 → pbcopy (macOS) → clip.exe (Windows) → xclip/xsel (Linux).
pub fn copy_best(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    // Always try OSC 52 first — it works inside SSH, tmux, and most modern terminals.
    if copy_to_clipboard(text).is_ok() {
        return true;
    }
    // Platform-specific fallbacks for terminals that don't support OSC 52.
    platform_copy(text)
}

#[cfg(target_os = "macos")]
fn platform_copy(text: &str) -> bool {
    pipe_to("pbcopy", text)
}

#[cfg(target_os = "windows")]
fn platform_copy(text: &str) -> bool {
    pipe_to("clip.exe", text)
}

#[cfg(target_os = "linux")]
fn platform_copy(text: &str) -> bool {
    pipe_to("xclip", text) || pipe_to("xsel", text)
}

#[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
fn platform_copy(_text: &str) -> bool {
    false
}

#[cfg(any(target_os = "macos", target_os = "windows", target_os = "linux"))]
fn pipe_to(command: &str, text: &str) -> bool {
    use std::process::{Command, Stdio};
    Command::new(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .and_then(|mut child| {
            if let Some(stdin) = child.stdin.as_mut() {
                let _ = stdin.write_all(text.as_bytes());
            }
            child.wait()
        })
        .is_ok()
}
