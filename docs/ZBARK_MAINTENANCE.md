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

References: [Apple APNs responses](https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns),
[APNs request headers](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns).
