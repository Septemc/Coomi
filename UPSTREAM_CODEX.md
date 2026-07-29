# Codex Rust upstream provenance

`codex-rs/` is a fork-and-prune snapshot of OpenAI Codex. It is the architecture
source for Coomi's agent loop, context pipeline, canonical tool protocol,
embedded App Server, TUI runtime, state, approvals, and sandbox abstractions.

## Frozen baseline

| Field | Value |
| --- | --- |
| Repository | `https://github.com/openai/codex.git` |
| Commit | `9a6668f674d74b35418fa534b3b6285a315d0765` |
| Imported subtree | upstream `codex-rs/` to local `codex-rs/` |
| Import commit | `3e788b384c2c796c5288672d5c49a04fa47de45c` |
| Files | 5,394 |
| Verification | path-by-path SHA-256, 0 differences |
| License | Apache-2.0 |

The machine-readable copy of these fields is in `UPSTREAM_CODEX.toml`.

## Remote policy

The Git remote is named `upstream-codex`. Its fetch URL points to the official
repository and its push URL is `DISABLED`. Coomi changes are never pushed to the
OpenAI repository.

## Update procedure

1. Fetch and inspect the candidate upstream revision without changing Coomi.
2. Export only upstream `codex-rs/` into a scratch directory.
3. Compare the candidate against the current frozen snapshot and review license
   or tool-protocol changes before copying files.
4. Import upstream mechanical changes in a dedicated commit containing no Coomi
   product edits.
5. Reapply or update Coomi-owned crates and patches in separate commits.
6. Run upstream tests first, followed by Coomi protocol, cost, provider, and TUI
   conformance suites.
7. Update `UPSTREAM_CODEX.toml`, this document, and the third-party notice.

Never resolve an upstream merge by silently replacing canonical `ToolSpec`,
`ResponseItem`, or `ResponseEvent` with Coomi-local equivalents. Protocol changes
require an ADR and conformance-test update.
