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
If timer scheduling fails, it does not open enrollment. The helper serializes operator/timer transitions. Timer callbacks use
`autoclose`, which changes only `open` to `existing` and cannot undo emergency
`disabled`. Cancel legacy `bark-register-autoclose-*.timer` units when upgrading
from the earlier helper that scheduled unconditional `close` callbacks. Keep the interpolation in Compose; otherwise the
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
parameters; the initial wire assertion needed no runtime change. Subsequent review
found that old bbolt records may retain keys with empty tokens. Registration
now checks key existence independently of token usability, so a valid existing
client can recover without opening enrollment. Database lookup errors fail
closed with 503. A new source head must not be confused with the historical
`fb6a43e` runtime; the new deployed revision is recorded separately below. New client AES-256-GCM keys stay in the client and the sender's
owner-only configuration, never in Compose, APNs credentials or the server.

Remote maintenance now uses [prur/bark-server](https://github.com/prur/bark-server).
Keep Finb's `origin` read-only and use the separate `fork` remote. Only the
credential-free regression workflow should run for this fork. The inherited
registry/release workflow is guarded to the original Finb repository so a fork
tag cannot accidentally target upstream registry names or signing credentials.

Review follow-up also resolves build output paths before changing subprocess cwd,
creates `/data` for UID/GID 1000 in fresh images, and covers empty-token recovery
after bbolt reopen plus timer behavior after emergency disable. Existing mounts,
keys and database schema remain unchanged.

## Current installed revision after review fixes

Runtime source: `6e19b8a6acb486645960226a1dfcdfd06effd8f5`.
Image: `bark-server:zbark-6e19b8a6acb4`, image ID
`sha256:2f93658f6ddf211b3bbbdc74c1afbf8b3c074b6ae7657ddb0e661c4db04f9f83`.
Binary SHA-256: `e3420cb046aef79f187666e846ea6bdf4f87beb1f7417d9e12d31cd2fa682e02`.
Protected consistent snapshot and rollback definitions:
`/opt/notify-hub/backups/zbark-20261106-6e19b8a6acb4`.

The database-copy canary deliberately invalidated one copied token, then restored
that registration through the closed-enrollment endpoint without changing its
key or the count of 3 registrations. A separate container with no network, no
published port and no production data mount initialized its own database as UID
1000. Current public health/rejection checks follow deployment. Production
switching took 1.29 seconds; the earlier backup stop/start interval was not
retained and is not presented as a measured duration. Other services retained
their start times, live data/credential mounts and hardening. No old timer units
were pending; new timer callbacks cannot override `disabled`.

Local Go race/vet checks and 7 Python regressions pass; the exact runtime source
passed both push and PR GitHub Actions runs. PR: [server delivery](https://github.com/prur/bark-server/pull/1).
The deployed source and subsequent documentation-only submission head may differ.


## Consistent daily backups and isolated recovery

`deploy/zbark_backup.py` replaces the former live `tar` of the bbolt and SQLite
databases. It requires Python 3.11+, OpenSSL, Docker, the existing owner-only
`/root/.notify-backup.pass`, and a Linux build of the offline database probe:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -mod=readonly -trimpath -buildvcs=false \
  -o build/db-probe scripts/db_probe.go
# On the notification host, preserve the old backup.sh first:
sudo install -d -m 700 /usr/local/lib/zbark
sudo install -m 755 db-probe /usr/local/lib/zbark/db-probe
sudo install -m 755 zbark_backup.py /usr/local/bin/zbark-backup
# /opt/notify-hub/backup.sh becomes a two-line exec wrapper:
# #!/bin/sh
# exec /usr/local/bin/zbark-backup create
sudo /usr/local/bin/zbark-backup create
sudo /usr/local/bin/zbark-backup status
sudo /usr/local/bin/zbark-backup drill --archive /protected/notify-data-....tar.gz.enc
```

Creation serializes with enrollment-policy operations. It stops only the exact
Bark container while copying bbolt, then restarts it in `finally`, including
copy errors, stop timeouts and normal termination signals. A forced kill or host
failure still needs an operator to check `docker start bark-server`. SQLite uses
its online backup API, including committed WAL data, and passes integrity checks.
The databases are individually consistent; this is not a cross-service transaction.
No schema, APNs credential, registration policy, image or proxy change is required.

Each encrypted archive includes the databases, configuration, APNs certificate
directory, maintenance helpers/units, exact Bark runtime image and an offline
bbolt integrity/fingerprint probe. The existing backup password is deliberately
excluded. Temporary plaintext exists only in owner-only directories, removed
on normal completion/error. Encryption retains the existing OpenSSL PBKDF2/AES
format; SHA-256 and an inner manifest detect accidental damage, not an adversary
who can replace both the archive and checksum. Keep the archive and password in
separate protected locations. Never publish decrypted archives or keys.

Before publication, the archive is decrypted and all manifest hashes checked.
SQLite integrity and bbolt page consistency are checked. A disposable container
loads the exported image and starts from only restored data/certificates, with
`--network none`, no published port and no live data mount. Its revision, count
and full key/token fingerprint must match; the fingerprint remains unchanged
after startup. It cannot contact APNs. The signed client encryption key belongs
to a separate sender/client backup and is not expected on this server.

Only after the drill passes does the archive gain its final name and safe JSON
report, and only then can the existing seven-day archive retention run. A failed
run retains earlier completed recovery points. `status` fails on a backup older
than 36 hours, checksum drift, unhealthy Bark, open registration, a failed last
backup service, or an inactive timer. Existing legacy archives remain readable
with the old OpenSSL command, but do not have the new manifest/drill evidence.

Rollback the helper by restoring the preserved `backup.sh`; this does not change
the live database. Runtime rollback still follows the earlier image/config
procedure. A backup drill is not a production failover or full-host/ntfy restore.
Keep independent encrypted off-host copies and keep their password separately.

References: [SQLite online backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup),
[bbolt backups](https://pkg.go.dev/go.etcd.io/bbolt#hdr-Database_backups),
[Docker network isolation](https://docs.docker.com/engine/network/drivers/none/).


## Installed backup and recovery evidence (2026-11-06)

Backup helper source: `f17001adb9ca8e5cb982d4f1a8c9089163f34462`.
Runtime source/image are unchanged from `6e19b8a` above.
The old backup entry is retained at
`/opt/notify-hub/backups/maintenance-helper-33595fc/backup.sh`.
The current entry is `#!/bin/sh` followed by
`exec /usr/local/bin/zbark-backup create`; the existing daily timer remains active.

Both manual creation and the actual `notify-backup.service` completed the full
drill. The unit recovery point is
`notify-data-20261106_015421-520eabc3.tar.gz.enc`, SHA-256
`0c34b1bac8505a0dd3577304e6f1a7722c887701c0ea6c3d83e9dbd8970e383a`.
All 3 registrations and the complete key/token fingerprint matched before/after
isolated runtime startup. bbolt page and SQLite integrity checks passed.
The successful unit run's Bark stop/copy/start interval was 0.505 seconds; the
combined trial downtime was not measured. Caddy and ntfy start times were unchanged.

The first trial failed safely because a capability-free root probe could not
read a restored file after ownership changed to UID 1000. It published no new
backup and restarted Bark. The probe now uses the copied database's owner; this
has a dedicated regression. 15 Python tests, Go race/vet and the source's remote
CI pass. Code and security reviews completed for that source. PR:
[backup delivery](https://github.com/prur/bark-server/pull/2).

The encrypted archive and checksum were copied to an owner-only Mac recovery
directory using `ssh ... sudo cat` (the server backup root is deliberately not
readable by unprivileged scp). Local decryption verified every manifest hash and
SQLite database. The original password was separately preserved in an owner-only
Mac configuration file, without printing it or adding it to an archive.
Retain that separation when repeating the off-host copy. No existing credential
was regenerated, moved or overwritten. Third-location/password-vault recovery
and full-host/ntfy failover remain unverified.

Local/SSH maintenance completion notifications use the Mac's installed
`zbark-run`; notification encryption keys remain on the Mac/phone. Failure,
explicit replay and restored sender configuration were accepted by the server
and independently found in the physical Realm. The operator confirmed new-format
lock-screen display, wrong-key failure and correct-key recovery. The client
ledger contains the distinct display/history evidence. Existing business/Discord
senders were not changed.
