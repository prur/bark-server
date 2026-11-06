# Z Bark server operations

This fork requires a complete explicit APNs configuration. Keep the `.p8` on the
server, mounted read-only. Never put it in a build context, report, or git commit.

Required: `BARK_APNS_KEY_PATH` (or `BARK_APNS_KEY`), `BARK_APNS_KEY_ID`,
`BARK_APNS_TEAM_ID`, `BARK_APNS_TOPIC`. Set `BARK_APNS_ENV=production` for the
Ad Hoc client. `development` and `sandbox` select the sandbox endpoint. The
Bundle ID, Team and `aps-environment` must agree with the **exported** IPA.
Invalid/incomplete settings fail startup before the database opens.

## Closed enrollment with working token refresh

`BARK_SERVER_REGISTRATION_MODE=existing` permits registration only with a known
nonempty push key. It preserves that key while updating its token. `open` is the
upstream-compatible default; `disabled` blocks every registration. Invalid mode
values fail startup. Clients retain their key on registration failure.

An unconditional reverse-proxy 403 on `/register` breaks existing-client token
rotation. Forward that route only after verifying the server's `existing`
policy. Keep `/info` and `/mcp` hidden. A push key is a bearer credential: anyone
holding it can send notifications and refresh that registration. Keep it secret.

New installs and key resets need an operator-controlled enrollment window;
they deliberately fail under `existing`. Do not open public enrollment as a
health check. Do not erase the database to resolve a registration error.

## Regression and reproducible build

```sh
go test -race ./...
go vet ./...
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/build.py --os linux --arch amd64
```

Tests use ephemeral keys, local TLS/HTTP/2 APNs responses and temporary databases.
They cover provider rejection, payload/header preservation, silent priority,
credential validation/rebuild, closed enrollment, token rotation and bbolt
restart persistence. They never send to Apple. `build.json` records the source,
toolchain and binary hash. Builds reject dirty source; dependency resolution is
read-only. Use the same toolchain and commit for bit-for-bit reconstruction.

`deploy/Dockerfile.zbark` packages that static binary. Use a runtime base containing
CA roots and pin it by an inspected image digest/ID. Put only `bark-server` and
the Dockerfile in the build context. Build an immutable `zbark-<commit>` tag and
record its image ID; mutable `custom`/`latest` tags alone are not provenance.

## Update and rollback

1. Record the current container image ID, Compose service definition, APNs public
   settings, data mount, credential mount and registration gate/timer state.
2. Build and test first. Save protected copies of the Compose and proxy config.
   Stop **only** Bark briefly, copy `bark.db` after its writer has exited, and
   restart it. Keep the backup outside the active data directory, mode 0600.
3. Start a canary on the same Docker network with a **copy** of that backup,
   the same read-only key mount, no published ports and registration `existing`.
   Verify version/hash, health, closed enrollment and idempotent refresh using
   a copied existing key/token without printing them. Check registration counts.
4. Point only the Bark service at the new immutable image and policy. Recreate
   only Bark (`--no-deps`); preserve `/data` and `/certs`. Recheck health and
   counts. Then update the registration gate, validate Caddy and reload it.
5. For rollback, restore the recorded service/gate definitions and old image,
   recreate only Bark and reload the validated proxy config. Do not overwrite
   a newer live database with a stale backup. No schema change is required here.

An APNs 200 means Apple accepted the request; it does not prove the extension ran
or history persisted. Physical-device acceptance must separately cover app
registration, foreground/background/locked delivery, decryption, media failure,
restart and in-place update. Do not repeatedly retry `BadDeviceToken`,
`DeviceTokenNotForTopic` or `Unregistered`; diagnose environment/topic or refresh
registration. Push failures never overwrite a newly rotated database token.

## Installed deployment (2026-11-06)

Runtime source: `fb6a43e039078ed52dded5aadc438be992601af6`.
Image: `bark-server:zbark-fb6a43e03907`, image ID
`sha256:be67450ec590d9f4b6cc3b3d3686f74bdc596d96d4c0ef8f203bc15b6d10a62b`.
Binary SHA-256: `eda6ebe7442a747c45de1270c628c769cf432b14e638a957f7c6c87901f87640`.
Existing data, read-only credentials, non-root user, read-only filesystem,
resource limits and other services were preserved. The protected rollback files
and consistent bbolt snapshot are under
`/opt/notify-hub/backups/zbark-20261106-fb6a43e03907` on the server.

The Compose environment now includes
`BARK_SERVER_REGISTRATION_MODE=${BARK_SERVER_REGISTRATION_MODE:-existing}`.
The installed `/usr/local/bin/bark-register-window.sh` is the Python program
from `deploy/bark-register-window.py`; execute it directly with sudo:

```sh
sudo /usr/local/bin/bark-register-window.sh status
sudo /usr/local/bin/bark-register-window.sh close
# Only when deliberately pairing a new device / resetting a key:
sudo /usr/local/bin/bark-register-window.sh open 2
# Emergency: block both new registration and existing token refresh:
sudo /usr/local/bin/bark-register-window.sh disable
```

`close` now means **closed new enrollment with existing token refresh allowed**.
It does not reinstate the old unconditional Caddy 403. `open` schedules automatic
closure before changing the service, permits 1–30 minutes, and only recreates Bark.
If timer scheduling fails, it does not open enrollment. Old scheduled `close`
commands remain compatible. Keep the interpolation in Compose; otherwise the
helper refuses to change policy. Status and idempotent close were checked live;
open/timer failure ordering was tested with mocked commands. No public pairing
window was opened during this validation.

The canary replayed an existing registration against a **database copy**, retaining
its key/token and count. Public HTTPS health, closed enrollment, malformed JSON
and missing-key rejection passed after deployment. One production APNs request
with an intentionally invalid all-zero token returned `400 BadDeviceToken` using
the read-only credentials and production topic. This is a provider negative case,
not proof of delivery to a physical iPhone. Local build and deployment reports
are in the task worktree's ignored `build/` directory.

References: [Apple APNs responses](https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns),
[APNs request headers](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns).

## Continuing acceptance and remote delivery (2026-11-06)

The current session re-read the live image ID and binary hash above, confirmed
the production topic/Team/environment and `existing` policy, and observed the
updated physical client successfully POST-register without changing its key.
The phone received test notifications through this deployment; the operator
visually confirmed the uniquely identified locked-screen case. Client-side Realm
inspection and update evidence belong to the client delivery ledger; APNs 200
alone is still not physical acceptance.

The new regression assertion includes a base64 `iv` and `iv_encoding=base64`
alongside ciphertext. The existing deployed runtime already forwards these
parameters; this continuation changes tests, documentation and the fork workflow
guard, not server runtime behavior. A new source head must not be confused with
the deployed `fb6a43e` runtime. No production restart is needed just to publish
these changes. New client AES-256-GCM keys stay in the client and the sender's
owner-only configuration, never in Compose, APNs credentials or the server.

Remote maintenance now uses [prur/bark-server](https://github.com/prur/bark-server).
Keep Finb's `origin` read-only and use the separate `fork` remote. Only the
credential-free regression workflow should run for this fork. The inherited
registry/release workflow is guarded to the original Finb repository so a fork
tag cannot accidentally target upstream registry names or signing credentials.
