# Preserve trajectory observations through envelope preparation

## What this changes

A trajectory-v1 observation survives parsing but loses its text during conversion to a contribution envelope: it is an Opaque session event, and the fallback excludes Opaque content. As a result, environment output such as "operation still pending; outcome unknown" disappears before normal content redaction.

Mark recognized trajectory observations with `record_type=trajectory_observation` and project them as Feedback through the existing redactor. System records and unrelated opaque records retain their exclusion. No tool identity, tool result, user rating or success claim is synthesized. There are no server, dependency, reward or consent-policy changes.

The regression parses a generic trajectory, inspects the raw envelope, and checks the prepared envelope after deterministic local redaction. It verifies preservation of the marker/content, exclusion of system text, absence of fabricated tool identity or user feedback, and continued exclusion of unrecognized opaque content. `docs/trajectory-observations.md` documents the representation and its limits.

## Compatibility and review decision

This uses the existing v1 Feedback event enum; the marker distinguishes environment observations from owner evaluations. Consumers must not infer a user rating solely from the event enum. Please review whether this marked carrier is the right compatible representation. A dedicated event variant would require coordinated protocol/consumer work and is not silently introduced by this patch.

The fix applies to trajectory observations from any valid source name; it has no product-specific configuration or fixture dependency. It is independent of the reviewed-approval proposal.

## Verification

Tested independently on upstream `279f1049160bcd14e1fdae851b070d0c5c3c58cc` (1.0.8), Rust 1.96.0, macOS ARM64, with `CARGO_INCREMENTAL=0 RUSTFLAGS="-D warnings"`:

```text
cargo +1.96.0 test -p trace-commons-contributor --lib envelope::tests --locked
77 passed; 0 failed
cargo +1.96.0 test -p trace-commons-contributor-sources --lib --locked
285 passed; 0 failed
cargo +1.96.0 fmt --all -- --check
passed
```

Clippy with the repository allow-list stops on `clippy::nonminimal_bool` in the untouched `crates/trace-commons-contributor-sources/src/source/opencode_db.rs:765`. The same failure was reproduced on a clean checkout of this upstream revision; it has not been suppressed or included as an unrelated fix. Clippy is therefore not claimed green. Platform/feature/database and full merge-queue CI have not been run locally.

The patch also applies independently to the existing 1.0.7 integration pin. `validation.md` in the local review packet indexes retained logs; neither that packet nor Orbo is needed to apply or test this patch.

## Licensing

Changes are confined to permissive contributor/source crates and documentation. No new dependency, AGPL edge or Rust source file is introduced. The submitting author must complete upstream's inbound MIT OR Apache-2.0 acceptance and DCO sign-off; no PR or certification has been submitted.
