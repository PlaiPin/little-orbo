# Patch separation validation

**Handoff scope: source-only, complete. Binary packaging: pending separately.** The optional contributor build completed successfully (see `artifacts/test-evidence/commons-split-contributor-build.txt`), but the resulting executable identity was not verified and no contributor archive or release bundle was refreshed for this split. Existing archived binary provenance describes the previous patch generation. Packaging and release-integrity verification are excluded from this source-only handoff; the results below remain the source/test evidence. No further builds or expanded validation were run to close the handoff.

Recorded 2026-11-13 on macOS ARM64 with Rust 1.96.0. Compiler/test checks use `CARGO_INCREMENTAL=0 RUSTFLAGS="-D warnings"`. No PR, push, live provider request, enrollment, upload or deployment was performed.

## Source application and separation

- Deployment pin: `8fcb9e6fbfe1ccac5b781fc80399b7662a61f175` (1.0.7).
- Independently inspected upstream review base: `279f1049160bcd14e1fdae851b070d0c5c3c58cc` (1.0.8).
- Each generic patch applies independently to both bases. The full five-patch manifest applies cleanly to the deployment pin and reproduces the extracted final sources.
- Queue, watcher, server and server-test code matches the previous implementation after Rust formatting. The generic changes gain product-neutral tests and documentation; Orbo wire names and behavior are retained.
- Bootstrap tests cover explicit profile/order, idempotence, undeclared/duplicate/missing/unsafe paths, preservation of edited checkouts, checkout-root validation and rollback after a failed series.
- Host suite: **113 passed, 6 target-only skips**. Python lint and shell syntax checks passed.

## Independent generic validation

| Check on upstream review base | Result |
|---|---|
| Observation patch alone: envelope-related tests | 77 passed |
| Observation patch alone: source/parser library tests | 285 passed |
| Approval patch alone: reviewed-approval regressions | 5 passed |
| Approval patch alone: broad daemon IPC tests | 432 passed / 3 failures |
| Untouched upstream: same broad daemon IPC tests | 427 passed / the same 3 failures |
| Formatting, each standalone generic checkout | Passed |
| Clippy, both standalone patches and untouched upstream | Same baseline `nonminimal_bool` failure in `contributor-sources/src/source/opencode_db.rs:765` |

The three shared IPC failures are `a_sign_in_launch_needs_balance_for_its_first_calls`, `managed_sign_in_launch_carries_a_bool_never_the_key`, and `managed_sign_in_redeem_hosts_the_proxy_and_ends_it`. The broad suite and Clippy are **not all-green**; no test weakening or lint suppression was added. Clippy stops at the baseline error, so it does not establish that all later targets are lint-clean.

A shared Cargo target initially reused the wrong same-version test binary and matched zero approval tests. That result was discarded. Workspace packages were cleaned before switching checkouts; the retained approval result executes all five regressions. Prefer a distinct target directory per checkout. External platform/feature/database and full merge-queue CI remain unrun.

## Full integration validation

The full series was freshly applied to the deployment pin and checked with the same warning-as-error flags:

| Check | Result |
|---|---|
| Envelope/preparation tests | 79 passed |
| Broad daemon IPC tests | 435 passed / the same 3 previously recorded sign-in fixture failures |
| Orbo full-event delivery and targeted-selection server tests | 3 passed |
| Existing export authorization, retention and guardrail regressions | 3 passed |
| License-boundary integration tests | 4 passed |
| Full workspace formatting | Passed |
| Server and ingest binary compilation (`cargo check --bins`) | Passed |
| Server and ingest test-target compilation (`cargo test --no-run`) | Passed; this compiles but does not execute the complete server/database suites |
| Prepared/server-delivered fixture through Python consumer | Exact original package recovered; acknowledgment timing 1.25 seconds |
| Minimized child through ordinary contributor preparation | Exact minimized package recovered |

The full Orbo Clippy run also stops on `clippy::nonminimal_bool` in `crates/trace-commons-server/src/versioned_pipeline/service.rs:646`, in addition to the source/parser warning above. Both files are byte-identical to the pinned upstream revision (recorded in `commons-split-lint-source-comparison.json`). The server warning was identified in unchanged source; the separate clean 1.0.8 Clippy reproduction covered the client/source warning. No lint allow-list was widened. Later lint targets may remain unchecked after these failures.

The serialized server response is delivered through an HTTP fixture transport, not a live network service. Existing source-exclusion tests and fresh bootstrap/reentry checks also pass. The contribution flow remains dependent on the full generic-plus-Orbo series; the generic-only profile omits the custom server and recovery contracts.

## Evidence

`artifacts/test-evidence/commons-split-*` contains commands/output, the source-application record, preservation hashes, baseline comparison and host results. `commons-split-rust-results.json` records Rust command exit codes; nonzero baseline failures are retained explicitly. No fixture result establishes live service or physical acceptance.
