# Bind reviewed approval to the exact prepared envelope

## What this changes

A client can inspect one prepared envelope and then approve after that entry's preview has changed. A client-side check followed by ordinary approval cannot make the review and approval refer to the same bytes atomically.

Add `approve_reviewed` for one entry and an expected SHA-256 envelope digest. Compare the pinned digest and Pending state while holding the same queue lock that approves. Refuse stale or missing previews without implicitly rebuilding them. Reject bulk selectors, outcome/correction mutations and malformed required fields. Existing scopes, policy, expiry, persistence and upload validation still apply; the ordinary `approve` method retains its current semantics.

A distinct method name makes older daemons reject the operation instead of silently ignoring a new argument to ordinary approval. `docs/contributor-reviewed-approval.md` documents the request, errors, old-daemon behavior and uncertain-response boundary.

## Compatibility and review decision

This is an additive daemon IPC method. It adds no persisted queue fields, recovery method, server endpoint or product-specific schema. Approval is not proof of upload or delivery; a client must not automatically retry approval after a lost response. Permanent revocation is a separate concern.

Please review the public method/error naming. The expected digest check is intentionally inside the approval lock, not an earlier client or daemon precheck. This proposal is independent of observation preservation.

## Verification

The patch includes regressions for a changed digest, a matching digest, missing/malformed fields, forbidden bulk/content mutations, absent previews and changed queue state. The existing dispatcher registry assertion includes the new method.

Tested independently on upstream `279f1049160bcd14e1fdae851b070d0c5c3c58cc` (1.0.8), Rust 1.96.0, macOS ARM64, with `CARGO_INCREMENTAL=0 RUSTFLAGS="-D warnings"`:

```text
cargo +1.96.0 test -p trace-commons-contributor --lib reviewed_approval --locked
5 passed; 0 failed
cargo +1.96.0 test -p trace-commons-contributor --lib daemon::ipc::tests --locked
432 passed; 3 failed
cargo +1.96.0 fmt --all -- --check
passed
```

A clean checkout of the same upstream revision has **427 passed / the same three failures**:

- `a_sign_in_launch_needs_balance_for_its_first_calls`
- `managed_sign_in_launch_carries_a_bool_never_the_key`
- `managed_sign_in_redeem_hosts_the_proxy_and_ends_it`

Clippy with the repository allow-list stops on `clippy::nonminimal_bool` in untouched `crates/trace-commons-contributor-sources/src/source/opencode_db.rs:765`, also reproduced on clean upstream. Neither the broad suite nor Clippy is claimed all-green; no suppression was added. Full platform/feature/database and merge-queue CI remain separate.

The patch also applies independently to the existing 1.0.7 integration pin. The local review packet's `validation.md` indexes retained logs.

## Licensing

Only the permissive contributor crate and documentation change. No dependency, AGPL edge or new Rust source file is introduced. The submitting author completes upstream's inbound MIT OR Apache-2.0 acceptance and DCO sign-off; no PR or certification has been submitted.
