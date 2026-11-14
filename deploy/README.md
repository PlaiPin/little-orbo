# Deploy and test the embodied integrations

The installed `orbo` CLI includes `live-e2e` and `intents`. They are explicit
operator tools; starting the runtime does not run cloud tests, export a trace,
or transfer funds. Implementation and network-free tests are separate from
credentialed service acceptance and physical robot qualification.

| Component | Setup and executable checks |
| --- | --- |
| NEAR AI, GPT-Live, embodied task and Commons round trip | [Live E2E runner](live-e2e/README.md) |
| Compatible Commons contributor/server build, deployment and readiness | [Trace Commons deployment](trace-commons/README.md) |
| Reviewed NEAR-origin token swaps and durable recovery | [NEAR Intents](../integrations/near_intents/README.md) |
| Local microphone/speaker commissioning | [Live audio](audio/README.md) |
| Existing Pi camera stack | [Camera setup](ros2-camera/README.md) |

## Shared external credentials

The public [credentials.toml](credentials.toml) points to
`~/.config/orbo/credentials.env`, outside the repository. Create the private blank
template once, then fill it with a local editor:

```sh
uv run --frozen orbo credentials init
uv run --frozen orbo credentials status
```

`init` never overwrites an existing file. `status` prints presence booleans only.
The file must be owned by the executing user and mode 0600. It uses literal
`KEY=value` lines with optional matching quotes and whole-line comments; it is
not shell code and has no variable/command interpolation. Do not `source` it.
Neither actual values nor private signer keys belong in the TOML or repository.

Runtime providers, the Live E2E runner, optional accounting, remote credentials,
and Intents automatically use this source. No shell exports of provider keys
are needed or consulted. Select a different external path in the TOML, or set
`ORBO_CREDENTIALS_CONFIG` to another public pointer file. Relative secret paths,
symlinks and secret files inside the repository/deployment tree are rejected.
Standalone wheel installations outside a checkout must select their pointer via
`ORBO_CREDENTIALS_CONFIG`.

The generated template contains `NEARAI_API_KEY`, `OPENAI_API_KEY`, Commons
consumer/accounting tokens, the optional NEAR reporting and Intents API keys,
and `NEAR_SIGNER_ACCOUNT_ID`, `NEAR_SIGNER_PUBLIC_KEY`,
`NEAR_SIGNER_PRIVATE_KEY` for an optional dedicated funded signer. Commons native
services use `TRACE_NEAR_AI_PRIVACY_API_KEY` and their applicable database/issuer
credentials from the same source through the native launcher. Leave unused
values blank. Nonsecret account, endpoint, route and spending choices stay in
the ordinary runtime/Intents/server configuration.

For systemd hosts, the single source is `/etc/orbo/credentials.env`, owned by
root and mode 0600. Each supplied service uses
`LoadCredential=orbo.env:/etc/orbo/credentials.env`; systemd makes a private
readable copy for that service's UID. The public
[service pointer](credentials.service.toml) selects this copy. This works with
the units' home-directory protection and does not require a shared Unix group
or per-key files. If changing the source location, update the unit's
`LoadCredential` path. Restart the affected service after rotating credentials
so systemd refreshes its copy.

## Raspberry Pi runtime

On the intended Ubuntu 24.04 ARM64 Pi with ROS 2 Jazzy already installed, use
`sudo bash deploy/install-pi.sh --install-systemd`. Stop the existing robot
services first; the installer refuses an active stack. It installs locked Python
extras and builds the ROS workspace. It preserves existing configuration/data,
copies `embodied.toml` only when absent, and does not enable or start services.
The default preserves the installed camera stack.

Edit `/etc/orbo/embodied.toml` for the commissioned map, camera, calibration,
devices and owner. Install the external `/etc/orbo/credentials.env` described
above, then copy
[orbo-embodied.conf.example](systemd/orbo-embodied.conf.example) to
`/etc/systemd/system/orbo-runtime.service.d/embodied.conf`. Create that drop-in
directory if necessary. The service loads the public pointer and systemd's
private copy automatically; the robot profile contains no secrets.

`near.enabled` enables verified reasoning. `live.enabled` additionally requires
NEAR and embodied support, the alternate chained voice profile disabled, and a
qualified audio path. The prerecorded-audio E2E runner opens its own Live session
and can delegate to a runtime whose physical `live.enabled` remains false.

Optional `[commons]` and `[reporting]` examples are commented in the profiles.
Configure the same-owner contributor socket, compatible HTTPS origin and enrolled
account together. Contributor enrollment is distinct from `COMMONS_EXPORT_TOKEN`
for consumer retrieval, `COMMONS_CONTRIBUTOR_TOKEN` for accounting reads, and
`NEARAI_REPORTING_TOKEN` for authoritative NEAR usage reporting.

After configuring the robot stack and credentials, reload systemd and start the
runtime deliberately. Existing grants are still required. The E2E runner does
not create grants. Select their destination, expiry and scope for the intended
run through `orbo request permission.set`; inspect the task/environment before
authorizing motion or capture. Relevant scopes are:

- Task execution and evidence: `task.execute`, `observation.capture`,
  `observation.retain`, `observation.infer`, `memory.read`, `memory.write` for the
  configured environment, plus `cloud.reasoning` for `near`.
- Navigation: `robot.command` for the configured robot ID; registered places must
  resolve to commissioned viewpoints on the active map.
- Result release: `result.release` for `local` or `openai`, matching the selected
  delivery path. Live voice also requires `cloud.voice` for `openai` and
  `speech.play` for `local`.
- Commons review, contribution, retrieval and accounting remain separately
  granted and subject to exact reviewed bytes and recipient selection.

## Gazebo with real providers

The Pixi lock includes NEAR, Live audio/WebSocket and Intents dependencies in the
same environment as ROS. Installing them does not contact a provider.

```sh
pixi install --locked
pixi run --locked orbo-sim embodied:=true
```

Copy [gazebo-embodied.toml](profiles/gazebo-embodied.toml) to an ignored local
location such as `.live-validation/gazebo-embodied.toml`. Enable NEAR in that copy
and fill `NEARAI_API_KEY` in the external credentials file. In another terminal:

```sh
pixi run --locked bash tools/run-embodied-runtime.sh .live-validation/gazebo-embodied.toml
```

The launcher fixes the simulator-only DDS domain and Gazebo partition, and
accepts the optional profile path shown above. Run from the repository root;
profile-relative artifact paths are interpreted by the runtime from that root.
Configure the runner's socket to the absolute path of
`artifacts/private/gazebo-runtime.sock`. Register the intended place and grant
the selected environment/robot scope before submitting tasks. The profile uses
environment `gazebo-living-room`, map `living-room-map` and robot `orbo-gazebo`.

For example, register the existing `tv` waypoint through the owner socket:

```sh
pixi run --locked orbo request place.register \
  --socket artifacts/private/gazebo-runtime.sock \
  --args '{"name":"tv area","viewpoints":["tv"]}'
```

The separate, cloud-free regression run is:

```sh
pixi run --locked bash tools/run-embodied-sim-test.sh
```

It uses real Gazebo physics, Nav2, camera capture and the velocity gate, with an
explicit fixture reasoner. It cannot pass the live runner's provider-evidence
check. Do not weaken command freshness or leases to accommodate an overloaded
development machine; inspect the failure and stop competing builds before retry.

## Staged end-to-end run

1. Run the live runner's network-free preflight with the chosen PNG, WAV and
   keys installed locally. Run the bounded NEAR and stationary Live stages.
2. Use `live_backend: runtime` for recorded speech through actual GPT-Live
   delegation and the deployed NEAR task backend. This preserves partial-turn
   context and requires OpenAI result-release permission. Start in Gazebo.
3. Repeat on the robot after audio/map commissioning. Observe uncertainty,
   audible results, barge-in, explicit task cancellation and restart recovery.
4. Prepare and review the resulting task export. Submit/retrieve the exact
   reviewed Commons package using its separate runner stage. The public-source
   Commons build does not include the production scoring/settlement/payout
   adapters; an operator must provide those for a commercial rewards deployment.
5. Independently review an Intents route and quote. Its unfunded quote/preflight
   can run before a signer is installed. A funded test requires a concrete quote
   digest, dedicated funded account and explicit execution flag. A successful
   swap is not evidence of provider-credit funding.

Local reports, images, package archives, databases and credential files belong
in ignored private locations. Keep maintained documentation in READMEs.
