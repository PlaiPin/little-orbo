# Local contribution and retrieval

The local development lane runs the actual pinned Orbo contributor, upload-claim
issuer and ingest binaries. It needs no Trace Commons invitation, NEAR account,
provider key or production operator. It creates its own instance signing keys,
device enrollment, PostgreSQL password and scoped local reviewer/export tokens.

This is a **synthetic-only development instance**. Ingest uses its upstream
development gate and local encrypted artifact store; PostgreSQL holds the issuer's
device/enrollment registry. The server's ordinary deterministic scrubbing runs,
but the NEAR semantic privacy classifier does not. Medium-risk fixture content
is quarantined and explicitly approved through the reviewer API with zero credit.
This proves transport, review, preservation and retrieval, not production scoring,
privacy classification, rewards, visual evidence or physical success.

## Existing installation

The Mac instance is configured at
`https://127.0.0.1:19443`. State and reports are in the Git-ignored
`artifacts/local-commons/` directory. All listeners bind to loopback. The generated
CA is trusted only by the test processes; no system trust settings were changed.

From the little-orbo root:

```sh
uv run --frozen --extra local-commons python tools/local-commons.py status
uv run --frozen --extra local-commons python tools/local-commons.py start
uv run --frozen --extra local-commons python tools/local-commons-contribute.py
uv run --frozen --extra local-commons python tools/local-commons.py stop --database
```

`start` restarts the instance's labeled PostgreSQL container when configured.
Docker Desktop must be running. `stop --database` stops the managed server, issuer
and database without deleting their state. Bare `stop` leaves the database up.
The contribution test starts a bounded native daemon, watches only its newly
created synthetic staging folder, then stops the daemon and restores its settings.
It does not install a background contributor service or watch coding sessions.

Each contribution test creates a new `contribution-*/report.json` with the
approved export, prepared review events, upload status, server review receipt and
consumer report beside it. A passing report requires:

1. An Orbo runtime synthetic episode and digest-bound export approval.
2. Native daemon discovery, parser/redactor preparation and complete body review.
3. `approve_reviewed` for the exact envelope and a device-signed upload claim
   from the real local issuer.
4. Actual HTTP upload, server quarantine and authorized zero-credit review.
5. Consumer selection by submission ID, exact package digest reconstruction and
   reproduction of the synthetic 1.25-second acknowledgment analysis.
6. Rejection of a deliberately incorrect expected digest.

Verified build: `8fcb9e6fbfe1-orbo-8ebe22e08c3b4b15`. The initial native flow passed
in `artifacts/local-commons/contribution-1794691510516036000/report.json`.
A fresh-directory repeat (including certificate/key generation, database
creation and native enrollment) passed in
`artifacts/cm-repro/contribution-1794691621348725000/report.json`. The repeat
instance and its database were stopped afterward; evidence and volumes remain.

`report.json` at the instance root records a separate server API test covering
unauthenticated/unauthorized requests, withholding before review, cross-tenant
isolation, idempotency, actual server restart persistence and post-revocation
withholding. That test uses a static development contributor credential and the
prepared upstream synthetic fixture; it does not substitute for the native daemon
test. Run it once on a fresh corpus, **before** adding the issuer/enrollment.

Observation-only trajectories are refused by the daemon with `no-user-messages`.
The native integration fixture includes explicitly synthetic request and response
events, which the runtime exports alongside its marked observations. No scoring
or admission checks were patched out to pass this test.

## Reproduce from a fresh directory

Install dependencies with `uv sync --frozen --extra local-commons`. Use Rust
1.96.0 and the maintained five-patch source. Build/verify the native package using
the main deployment README if `artifacts/commons-current-macos-arm64` is absent.
The issuer is a third upstream binary; build it with the package's source identity:

```sh
uv run --frozen --extra local-commons python - <<'PY'
import json, os, pathlib, subprocess
root = pathlib.Path.cwd()
source = root / '.sdk/commons-orbo'
manifest = json.loads((root / 'artifacts/commons-current-macos-arm64/manifest.json').read_text())
subprocess.run(['python3', 'tools/bootstrap-commons.py', '--directory', str(source)], check=True)
env = dict(os.environ, TRACE_COMMONS_BUILD_COMMIT=manifest['build_commit'],
           CARGO_TARGET_DIR=str(source / 'target'), CARGO_INCREMENTAL='0', RUSTFLAGS='-D warnings')
subprocess.run(['cargo', '+1.96.0', 'build', '--locked', '-p', 'trace-commons-server',
                '--bin', 'trace-commons-upload-claim-issuer'], cwd=source, env=env, check=True)
PY
```

On an unused default state directory and ports:

```sh
uv run --frozen --extra local-commons python tools/local-commons.py init
uv run --frozen --extra local-commons python tools/local-commons.py start
uv run --frozen --extra local-commons python tools/local-commons.py test
uv run --frozen --extra local-commons python tools/local-commons-issuer.py init
uv run --frozen --extra local-commons python tools/local-commons-issuer.py enroll
uv run --frozen --extra local-commons python tools/local-commons-contribute.py
```

Initialization refuses to overwrite existing state. All three tools accept
`--directory PATH`; keep paths short enough for the native Unix socket (about
100 bytes including `/contributor/daemon.sock` on macOS). For another simultaneous
instance, supply unused `init --port/--backend-port` values, and issuer
`--pg-port/--issuer-port/--admin-port` values. Default ports are 19443, 13907,
15432, 13917 and 13918 respectively. The development PostgreSQL image is
`postgres:15`; the database container and volume are named from the state path.
The issuer records a binary hash and uses upstream-generated PKCS8 keys.

Enrollment uses the upstream `mint-grant` and `login --grant` commands against our
local instance. Existing enrollment is preserved and never replaced. A native OS
credential store is still required; the working Mac path uses the ordinary
contributor credential store. This is not a proof of Linux headless keyring setup.
The test CA expires after 30 days; provision fresh isolated state for later runs.

## Credentials by test scope

| Scope | What the owner must provide |
|---|---|
| Synthetic local contribution/retrieval above | **Nothing.** Local passwords, signing keys, enrollment grants and exporter tokens are generated. |
| Real semantic privacy filtering | An ordinary NEAR AI Cloud key in `TRACE_NEAR_AI_PRIVACY_API_KEY`, plus credits. This is self-service, not a Trace Commons invitation. A separately qualified self-hosted filter is another option. |
| Full live voice/robot demo | `NEARAI_API_KEY` and `OPENAI_API_KEY`, appropriate model/account access, authorized usage and the deployed robot/runtime configuration. |
| Authoritative rewards or credit application | A qualified scoring/settlement/payout deployment and recipient/account configuration; API keys alone do not establish this. |
| Their hosted production Commons | Separate hosted enrollment/access and deployment compatibility. Our generated local credentials have no authority there. |

Provider keys belong in the existing project `.env` for the production/live lane.
These development tools intentionally do not load that file or inherit provider
credentials into the Commons processes. Local secrets remain inside the private
ignored state directory. Do not copy the development authorization, database
superuser, gate or synthetic review settings into the production service profile.

### Obtain provider credentials

These acquisition instructions follow the linked public documentation.
Authenticated provider requests have not been verified with this project's account.

1. Sign up at [NEAR AI Cloud](https://cloud.near.ai/signin), using Google,
   GitHub or NEAR. In the dashboard, open **Credits** to add credit, then
   **API Keys** to create a key. The [official quickstart](https://docs.near.ai/cloud/quickstart)
   documents this self-service flow without an invitation step.
2. For semantic privacy filtering, put that key in
   `TRACE_NEAR_AI_PRIVACY_API_KEY` in the ignored project `.env`.
   [Privacy Filter](https://near.ai/models/openai/privacy-filter) explicitly
   documents an ordinary NEAR AI Cloud bearer key for
   `POST https://cloud-api.near.ai/v1/privacy/classify`, model
   `openai/privacy-filter`. The `openai/` model namespace does not mean an
   OpenAI API key is used for this NEAR-hosted endpoint.
3. For Orbo reasoning/vision, set `NEARAI_API_KEY` from the same NEAR dashboard.
   These two variables can contain the same ordinary Cloud key when its
   permissions allow both endpoints; separate keys allow independent rotation.
   There is no special Trace Commons privacy-key application.
4. For live voice, create a project key at
   [OpenAI API Keys](https://platform.openai.com/api-keys), configure
   [API billing](https://platform.openai.com/account/billing/overview), and put it
   in `OPENAI_API_KEY`. Our configuration uses `gpt-live-1`.
   The [OpenAI quickstart](https://developers.openai.com/api/docs/quickstart)
   and [GPT-Live guide](https://developers.openai.com/api/docs/guides/live)
   document the project-key route, with no Trace Commons enrollment requirement.
   Confirm model access and quota with a bounded live session; public docs do not
   establish this particular account's entitlement.

Adding keys does not switch this synthetic development instance to production
privacy filtering. The local tools intentionally exclude provider credentials;
the regular deployment/contributor launchers use the shared `.env` and require
the privacy key. Use that separate configuration to test hosted classification.

### Other entries in `.env.example`

These are optional features or instance credentials, not additional prerequisites
for the local contribution/retrieval test.

| Credential | How it is obtained / when needed |
|---|---|
| `COMMONS_EXPORT_TOKEN` | Issued by the operator of the selected Commons instance with export permission. Our local setup generates its own scoped token and supplies it to the test consumer. A NEAR inference key does not grant corpus retrieval access. |
| `TRACE_COMMONS_INVITE`, `TRACE_COMMONS_DEVICE_CODE` | Alternative operator enrollment methods. Local `local-commons-issuer.py enroll` mints an instance-signed grant automatically. Current upstream also documents ordinary NEAR AI login without an invite; that does not prove our pinned custom API is deployed on their hosted service. |
| `DATABASE_URL`, `TRACE_COMMONS_LOGIN_RESOLVER_DATABASE_URL` | Connection strings issued by your PostgreSQL administrator; local setup provisions its own database/password. Production roles and privileges must follow the pinned deployment docs. |
| `COMMONS_CONTRIBUTOR_TOKEN` | A token authorized for the selected account's Commons accounting reads, obtained from that instance's operator/authentication flow. Not needed for upload through the enrolled native daemon, or for the local test. |
| `NEARAI_REPORTING_TOKEN` | Optional authoritative cost reporting. A NEAR organization owner/admin creates a separate `usage:read` reporting token with the management API using their user access token; it is not an inference key. Also configure the organization UUID. |
| `NEAR_INTENTS_API_KEY` | Optional partner JWT from the [NEAR Intents Partner Dashboard](https://partners.near-intents.org). Our quote client supports an empty value. Not required for Commons or voice. |
| `NEAR_SIGNER_ACCOUNT_ID`, `NEAR_SIGNER_PUBLIC_KEY`, `NEAR_SIGNER_PRIVATE_KEY` | Your dedicated NEAR account and its matching signing key pair, provisioned through your wallet/account tooling only for the funded-swap feature. Not issued by Trace Commons and not required here. |
| `ORBO_REMOTE_OWNER_PHONE_TOKEN` | A locally generated secret paired with its token hash in our optional remote-task gateway configuration. No external credential portal. |

Sources: [upstream contributor enrollment](https://github.com/TraceCommons/trace-commons/blob/main/crates/trace-commons-contributor/README.md#quickstart),
[NEAR usage reporting](https://docs.near.ai/cloud/guides/usage-reporting), and
[Intents API keys](https://docs.near-intents.org/integration/distribution-channels/1click-api/authentication).
The reporting guide currently shows `cloud-api.near.ai`, whereas our reporting
client uses `cloud-platform.near.ai`; that optional path still needs endpoint
compatibility verification before claiming a live accounting pass.

Real rewards additionally require an operational scoring/settlement/payout
deployment. There is no single API key that makes the open development server
provide their commercial rewards service.
