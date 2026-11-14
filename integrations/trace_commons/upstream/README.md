# Generic Commons review packet

**Source-only handoff complete (2026-11-13); PRs not opened.** The descriptions below are local drafts ready for review, subject to the documented upstream validation limitations. Binary packaging and release-manifest refresh are separate pending work and are not prerequisites for reviewing these patches.

Two independent, product-neutral source patches are prepared for upstream review. They stay in the patch-based Orbo build. This directory contains descriptions and evidence, not published PRs. No upstream branch, fork, push, PR or issue was created.

| Proposed PR | Patch | Description |
|---|---|---|
| Preserve trajectory observations through envelope preparation | `../patches/generic/0001-preserve-trajectory-observations.patch` | [Observation fix](observation-preservation.md) |
| Bind reviewed approval to the exact prepared envelope | `../patches/generic/0002-approve-exact-reviewed-envelope.patch` | [Approval fix](exact-reviewed-approval.md) |

Each includes production code, standalone regressions and upstream documentation; neither imports Orbo code, fixtures, schemas, endpoints or dependencies. The patches touch disjoint source files and do not depend on each other. They can be proposed separately. They introduce no new crate/dependency/license-boundary edge. Server event export and Orbo revocation/status APIs remain exclusively in the Orbo series and are not bundled into these PRs.

## Review bases and reproduction

The recorded upstream review base is `279f1049160bcd14e1fdae851b070d0c5c3c58cc` (release 1.0.8), resolved from upstream `main` on 2026-11-13. The Orbo production dependency stays at `8fcb9e6fbfe1ccac5b781fc80399b7662a61f175` (1.0.7). Both defects are still present in the reviewed 1.0.8 source. See [validation](validation.md) for the actual checked scope; this is not a claim about future upstream revisions or unrun platform/CI jobs.

For an independent review, use a fresh Trace Commons checkout at `PR_BASE_REVISION` and apply **one** patch, not the full Orbo series. Example from the little-orbo root, with an existing clean checkout at `.sdk/commons-review-one`:

```sh
git -C .sdk/commons-review-one apply --check \
  "$PWD/integrations/trace_commons/patches/generic/0001-preserve-trajectory-observations.patch"
git -C .sdk/commons-review-one apply \
  "$PWD/integrations/trace_commons/patches/generic/0001-preserve-trajectory-observations.patch"
CARGO_TARGET_DIR="$PWD/.sdk/commons-review-one/target" \
CARGO_INCREMENTAL=0 RUSTFLAGS="-D warnings" cargo +1.96.0 test \
  --manifest-path .sdk/commons-review-one/Cargo.toml \
  -p trace-commons-contributor --lib envelope::tests --locked
```

Use a separate clean checkout for patch 0002 and the `reviewed_approval` test filter (five tests must execute). Keep the default per-checkout Cargo target directory. If sharing a target directory between same-version checkouts, clean the workspace packages before switching: Cargo can otherwise reuse another checkout's test binary. A zero-test result is not validation. The complete generic profile can also be prepared with the bootstrap command in the integration README. The source and IPC suites, formatting, clippy and license-boundary checks are documented in the validation record.

## Future submission procedure

1. Refresh upstream and check whether the defect is already fixed. Rebase/retest the patch if required; do not silently change the pinned Orbo dependency.
2. On a fresh branch, apply the single generic patch, inspect the diff and reproduce its regression tests. Use the accompanying description, updated to the final validation results.
3. Follow upstream `CONTRIBUTING.md`: short imperative commit title, DCO sign-off and inbound MIT OR Apache-2.0 terms. No author sign-off or licensing acceptance has been manufactured here; the submitting author performs those steps when committing/submitting.
4. Run the applicable upstream CI checks. Local evidence lists exactly what ran; it does not stand in for platform, feature, database or merge-queue CI.
5. Only when submission is authorized, push the branch and open the PR. Keep the local patch until the pinned release includes the accepted fix.

Maintainer decisions remain normal review questions: whether marked Feedback is the appropriate compatible representation for environment observations, and whether `approve_reviewed` is the preferred public IPC contract. Neither is a promise of acceptance. Generic revocation/recovery and full-event export would be separate designs with durable-state, authorization, disclosure and upgrade semantics, not cosmetic renames of the Orbo extension.
