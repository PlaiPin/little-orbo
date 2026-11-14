# Live end-to-end checks

This runner exercises real provider transports using production adapters. It is separate from the older NEAR/Fish smoke runner. It never supplies simulated provider responses. No stage runs implicitly: `preflight` is network-free and `run` requires explicit flags for selected effects. Runtime grants remain authoritative; the runner never grants itself robot or export permissions.

Install `uv sync --frozen --extra near --extra live`. Copy [config.json.example](config.json.example) into `.live-validation/e2e/config.json` and put an explicitly selected PNG and recorded request WAV beside it. Paths in the configuration resolve relative to that file. The PNG must be 16–1920 by 16–1080 pixels and at most 2 MB. Audio must be 0.1–20 seconds of mono PCM16 at 24 kHz. Record a request that needs delegation, for example “Please ask the application to describe its capabilities.” Actual model delegation is tested, not forced or fabricated.

Set `NEARAI_API_KEY` and `OPENAI_API_KEY` in the private external file referenced by [deploy/credentials.toml](../credentials.toml), which defaults to `~/.config/orbo/credentials.env`. Initialize its empty template with `uv run python -m orbo_runtime.credentials init`, then edit that external file locally. The repository configuration contains only its path; provider keys in ambient environment variables and repository-contained secret files are not used. `ORBO_CREDENTIALS_CONFIG` can select another public TOML pointer, including the deployed systemd pointer. The runtime and both smoke runners use this same credential source. Task stages also require enabled runtime providers and existing grants.

```sh
uv run --frozen --extra near --extra live orbo live-e2e preflight --config .live-validation/e2e/config.json
uv run --frozen --extra near --extra live orbo live-e2e run --config .live-validation/e2e/config.json --allow-cloud
```

The same CLI is available through `python -m orbo_runtime.live_e2e` and `uv run ... python tools/run-live-e2e.py`. Missing credentials produce preflight exit 2, without opening a socket. Successful execution exits 0, failed checks exit 1, and setup failures exit 2. Reports are owner-only JSON under ignored `.live-validation/e2e/`. Raw provider text, transcripts, images, audio and exception bodies are not written to reports. Runtime task journals retain their normal private evidence. Do not publish the reports without reviewing identifiers.

## Stages and evidence

| Stage | Checks | Required flags |
| --- | --- | --- |
| `near` | One exact text response, one image response, E2EE and verified model TEE signatures | `--allow-cloud` |
| `live` with `live_backend: near` | Real paced WAV input, one NEAR backend call after actual delegation, accepted result context, subsequent output audio and final usage | `--allow-cloud` |
| `task` | Submit the configured bounded task through owner IPC, wait for completion, release result and require live NEAR evidence | `--allow-cloud --allow-task`; add `--allow-motion` when `allowed_actions` includes `navigate` |
| `live` with `live_backend: runtime` | WAV → GPT-Live transcript/delegation → runtime NEAR interpretation/task → verified task result → GPT-Live context/audio | `--allow-cloud --allow-task --allow-motion` |
| `commons` | Optional exact reviewed submission, status lookup, accepted-only retrieval, reconstruction and source digest match | `--allow-commons`; add `--allow-export` when `submit: true` |

Choose stages explicitly, e.g. `["near", "live", "task"]`. For a single integrated voice-to-robot path use `["live"]` and `live_backend: runtime`. That path interprets the recorded request through the deployed conversation backend; the `task` configuration applies only to the separate structured `task` stage. Runtime voice delegation can request navigation, so it requires motion opt-in even for a recording intended to be stationary. Restrict runtime grants and commission the map/viewpoints before running. Use 120 live seconds and at least 180 stage seconds for longer task responses. Failure to finish within those limits fails the check.

Start with the Gazebo runtime using real providers, then the physical robot profile. A completed fixture-backed task cannot pass the task evidence check. Synthetic robot provenance is retained even when inference is live. A task timeout requests cancellation of its known task ID and never resubmits it. A lost admission response can leave an uncertain task; use the report run ID as the `live-e2e:<run_id>` request key to inspect runtime state before retrying. Each new invocation is a new run, not automatic recovery.

`task.evidence` requires the owner's `memory.read` grant for the environment. Its report includes `runtime_near_enabled`, `live_service`, `verified_response_count`, and a bounded list of model/signature/proof hashes for the current revision. Evidence is recorded at inference, not independently reverified by the runner. A real NEAR provider can be used with a synthetic Gazebo robot: provider provenance and robot provenance are separate. Voice-to-runtime requests use `task.delegate`, preserving the actual delegation ID, offset and transcript fragments with `complete_turn: false`. A partial transcript is never promoted to a completed typed request. This endpoint fixes the result destination to `openai` and requires its release grant; local-only results cannot be forwarded through this path. If voice admission becomes uncertain, the failure report retains the session/delegation IDs for reconciliation with the runtime conversation journal.

Recorded audio avoids microphone/speaker echo, but does not qualify physical AEC or prove audibility. Use the [audio deployment procedure](../audio/README.md) for physical full duplex. `frames_after_context_acceptance` counts output only after the matching result-context acknowledgment, excluding earlier queued audio; it still does not prove that the result's meaning was spoken correctly. A listener must verify the spoken result, uncertainty and interruptions on the real device. Image interpretation likewise needs independent scene labels. Reports deliberately keep these outcomes unestablished.

## Task-to-Commons handoff

The task report includes `task_id`, `episode_id` and provider evidence; the runtime Live report includes these under `tasks`. There is an intentional review boundary before export. Prepare `embodied_process` for the reported episode using `orbo request export.prepare`, preview/minimize with `export.preview` / `export.derive`, and approve the exact reviewed digest with `export.approve`. Follow the [Commons setup](../../integrations/trace_commons/README.md) to stage the approved trajectory and call `commons.review` for its contributor entry. Review the actual redacted body and recipient before using the returned envelope/body digests.

For the subsequent runner invocation select `["commons"]`; set its `export_id`, `entry_id`, package `source_digest`, and privacy risk. Set `submit: true` and the reviewed `envelope_digest` and `body_digest` only when authorizing that exact upload. Then run with `--allow-commons --allow-export`. With `submit: false`, the runner only reconciles and retrieves an already submitted entry. It never prepares, approves or blindly retries an export. Pending admission may cause retrieval to fail; reconcile before trying retrieval again. Any refused/revoked/expired/superseded entry fails. A successful retrieval establishes digest-preserving process evidence, not visual artifact delivery, a reward or applied credits.

NEAR Intents is a separate money authorization boundary; this runner does not automatically transact after an AI/Commons test. Use the Intents CLI's reviewed quote and explicit execution workflow.

## Limits and failure tests

Provider calls, input bytes, task budgets and session duration are bounded. These are usage limits, not a guaranteed currency cap; configure account spending limits before running. One delegation is allowed per Live smoke run and failed billed requests are not retried. Reports stop at the first failed stage. Persistent `running` after process death means unknown, not success; reconcile runtime tasks and Commons entries before another run.

The network-free tests cover authentication opt-ins, bounded inputs, response verification, live delegation/audio acceptance, task cancellation, fixture rejection, export review fences and report redaction. Separately run physical scenarios for barge-in versus task cancellation, stale/occluded views, disconnect/reconnect, restart recovery and revoked consent. The runner is a bounded smoke test, not evidence that these hardware scenarios passed.

Protocol references: [NEAR verified SDK](https://docs.near.ai/cloud/inference-sdk/python), [GPT-Live client delegation](https://developers.openai.com/api/docs/guides/live-delegation), and [GPT-Live WebSocket audio](https://developers.openai.com/api/docs/guides/voice-websockets). Account/model access is determined by the live run, never inferred from an installed SDK.
