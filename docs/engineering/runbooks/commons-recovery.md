# Commons review, minimization and recovery

Software revision: 13 November 2026. These operations are local software capabilities. Account enrollment, privacy-service processing, actual upload/admission/retrieval and credit application still need separately authorized live acceptance. NEAR Intents and automatic Fish funding remain outside this change.

## Upgrade together

Use the complete manifest-ordered series in `integrations/trace_commons/patches/series.json` (two generic fixes plus three Orbo extensions), the rebuilt contributor and the updated Python runtime. `orbo/0003-recovery-and-targeted-evidence.patch` adds durable entry revocation, exact-entry recovery and targeted server retrieval. The new calls fail closed against older daemons/servers; there is no fallback to blind approval or broad retrieval.

The bootstrap deliberately refuses to overwrite an SDK checkout with a different patch set. Preserve an existing checkout and use `python3 tools/bootstrap-commons.py --directory .sdk/commons-orbo` (or another fresh directory). Set `ORBO_COMMONS_SOURCE` to that directory when building. The queue's new `orbo_revoked` field defaults to false for old records and survives daemon restarts. The default bootstrap/build source is now `.sdk/commons-orbo`; old `.sdk/commons-locked` checkouts are not overwritten. Generic-only builds omit the Orbo recovery APIs and cannot replace this daemon. Upgrade the daemon before resuming contributions. Never downgrade a queue containing revocation fences: an older binary would ignore the new field.

## Minimize before approval

1. Close the task, call `export.prepare`, then inspect `export.preview`. The original journal/export stays immutable and private.
2. Call `export.derive` with the parent `export_id`, `redactions` and/or `omit_event_ids`. A redaction path is an array such as `["events", 2, "payload", "text"]`; event indices are zero-based in the preview. Only existing payload string leaves and `["episode", "owner"]` can be masked. An omitted event becomes an explicit `evidence.gap` preserving its identity and timestamps. Outcome, time, event type and provenance cannot be arbitrarily rewritten.
3. Inspect the returned child package and its new SHA-256. It includes parent-digest lineage and disclosure limitations, not the removed values. Metrics exclude affected events, omissions remain visible, and physical success remains unestablished. This is selective owner minimization, not automatic anonymization: IDs, times and remaining metadata may still be sensitive.
4. Grant the intended recipient and call `export.approve` for the **child** digest and expiry. Parent approval is revoked when deriving; the child inherits no approval. If any parent contribution is active/uncertain, first revoke and recover it. A previously delivered parent cannot be recalled by creating a child.
5. Stage `export.trajectory` from the approved child in the isolated manual-review directory. Obtain a fresh contributor entry. Call `commons.review`, inspect the exact prepared body, then `commons.submit_reviewed` with the returned envelope/body digests. The daemon's ordinary privacy filtering still runs. If it changes required evidence, review fails closed: revise the local derivative and repeat review; never accept changed bytes against the old digest.

Example local request arguments (replace IDs and indices from your preview):

```json
{"export_id":"PARENT_EXPORT_ID","redactions":[["events",2,"payload","text"],["episode","owner"]],"omit_event_ids":["EVENT_ID_TO_OMIT"]}
```

## Recover without duplicate sends

`commons.recovery_queue` returns owned entries in `approval_unknown`, `approved`, `uploading` or `revoke_pending`. After a runtime restart, call `commons.recover` with each `export_id` and `entry_id`. No inference, upload approval or purchase is automatically replayed.

| Local state | Recovery behavior |
|---|---|
| `approval_unknown` | Query the exact daemon entry and source hash. Positive matching-envelope evidence can establish approved/uploading/uploaded/refused/expired/superseded. Pending or absent evidence stays unknown. Never resend approval based on absence. |
| `approved` / `uploading` | Refresh exact entry state and source-linked history. If the local approval/grant has expired or been revoked, request durable revocation. |
| `revoke_pending` | Retry only the idempotent revocation operation. A timeout leaves this state durable. |
| `revoked` | The daemon acknowledged a permanent fence for this entry; it cannot be approved or claimed for upload again. Already claimed uploads may complete and delivered copies are not recalled. New contribution requires a fresh entry and review. |

`commons.revoke` first withdraws local approval and persists `revoke_pending`. `orbo_revoke_reviewed` fences the entry under the same queue lock used by approval/upload claim, atomically saves the queue, and reports whether an upload may already have started. A delayed approval response cannot replace a revocation decision locally. An in-flight worker cannot bypass the fence by restoring an approved/retry state. If daemon persistence fails, no successful fence is reported.

`commons.status` uses `orbo_entry_status`, querying a specific entry/source pair. History is filtered by source and current account **before** paging. Pages contain at most 100 records and a snapshot digest; changed snapshots fail the read so the operator can retry. Reads are bounded to 10,000 matching records; exceeding that bound is explicit failure, never a complete or missing result. Cached history is evidence of daemon knowledge, not an authoritative award/receipt/allocation ledger. `delivery_established` remains false until authorized consumer retrieval.

Legacy locally `revoked` entries from before the durable-recovery extension (formerly patch 0006) require `commons.recover` once to establish the daemon fence. Do not treat their historical cancel response as permanent revocation.

## Retrieve the exact authorized contributions

`consumer.retrieve` now requires parallel arrays `source_digests` and `submission_ids`: each expected reviewed package digest is paired with one submission UUID from source-linked status/accounting. Both arrays must be unique, contain 1–10 values, and fit the requested `limit`. Example:

```json
{"purpose":"attention_timing","source_digests":["REVIEWED_CHILD_SHA256"],"submission_ids":["SUBMISSION_UUID"],"privacy_risk":"low","limit":1}
```

The server selects these IDs before applying the result limit. Exporter authentication, tenant/access policy, consent, risk, retention and delivery-time revalidation still apply. Missing or unauthorized records yield incomplete results; the endpoint does not expose why a particular UUID is unavailable. The consumer checks **both** submission identity and package digest and rejects mismatched deliveries. Selection does not imply consent or bypass server policy.

## Validation boundary

Tests exercise adversarial response ordering, durable restart recovery, snapshot changes, exact retrieval beyond a first page, authorization/retention gates, immutable derivatives and fresh approval. Synthetic/local results are not live Commons acceptance, received NEAR credit or applied usage. See `../pending-validation.md` for the separate external checks.

The targeted endpoint returns `X-Orbo-Task-Selection` with the normalized requested IDs, including on an empty successful result. The consumer requires that exact acknowledgment, so an older server that silently ignores the selector fails closed. This header confirms protocol support, not permission or receipt of an award.
