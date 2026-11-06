#!/usr/bin/env python3
"""Consistent encrypted Notify Hub backups; restore drills never mount live data."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

HUB = Path("/opt/notify-hub")
PASSWORD = Path("/root/.notify-backup.pass")
DB_PROBE = Path("/usr/local/lib/zbark/db-probe")
ENVIRONMENT = {"BARK_APNS_KEY_PATH", "BARK_APNS_KEY_ID", "BARK_APNS_TEAM_ID",
               "BARK_APNS_TOPIC", "BARK_APNS_ENV"}
HOST_FILES = ("etc/nftables-notify-egress.nft", "etc/systemd/system/notify-egress.service",
              "etc/systemd/system/notify-backup.service", "etc/systemd/system/notify-backup.timer",
              "usr/local/bin/bark-register-window.sh", "usr/local/bin/zbark-backup")


def run(args, timeout=120):
    # Subprocess output may contain private configuration. Return it only to callers.
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode}); private output suppressed")
    return result.stdout


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def inspect(identifier):
    return json.loads(run(["docker", "inspect", identifier]))[0]


def info(identifier):
    return json.loads(run(["docker", "exec", identifier, "wget", "-qO-", "http://127.0.0.1:8080/info"], timeout=15))


def wait_info(identifier):
    for _ in range(40):
        try:
            return info(identifier)
        except RuntimeError:
            time.sleep(.25)
    raise RuntimeError("Bark did not become healthy after start")


def sqlite_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + 60
    def progress(*_):
        if time.monotonic() > deadline:
            raise RuntimeError("SQLite online backup exceeded 60 seconds")
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, \
         contextlib.closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256, progress=progress)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite backup integrity check failed")
    destination.chmod(0o600)


def copy_tree(source, destination, sqlite=False):
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("Required backup directory missing or linked")
    destination.mkdir(parents=True, mode=0o700)
    for path in sorted(source.rglob("*")):
        target = destination / path.relative_to(source)
        if path.is_symlink():
            raise RuntimeError("Linked backup inputs are not supported")
        if path.is_dir():
            target.mkdir(exist_ok=True, mode=0o700)
        elif path.is_file():
            if sqlite and path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if sqlite and path.suffix == ".db":
                sqlite_copy(path, target)
            else:
                shutil.copyfile(path, target)
                target.chmod(0o600)
        else:
            raise RuntimeError("Unsupported backup input type")


def stopped_copy(state, source, destination):
    """Restart the exact original container even if stop/copy raises or is interrupted."""
    identifier = state["Id"]
    started = time.monotonic()
    try:
        run(["docker", "stop", "--time", "20", identifier], timeout=45)
        if inspect(identifier)["State"]["Running"]:
            raise RuntimeError("Bark writer is still running")
        copy_tree(source, destination)
    finally:
        run(["docker", "start", identifier], timeout=45)
        wait_info(identifier)
    return round(time.monotonic() - started, 3)


def encrypt(stage, destination, password):
    with open(destination, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as output:
        process = subprocess.Popen(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                                    "-pass", "file:" + str(password)], stdin=subprocess.PIPE,
                                   stdout=output, stderr=subprocess.DEVNULL)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|gz") as archive:
                for path in sorted(stage.rglob("*")):
                    archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
            process.stdin.close()
            if process.wait(timeout=120):
                raise RuntimeError("Backup encryption failed")
            output.flush()
            os.fsync(output.fileno())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def unpack(archive, password, stage):
    # Decrypt only into an owner-only temporary directory, never into /opt or /etc.
    plain = stage / "archive.tar.gz"
    run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
         "-pass", "file:" + str(password), "-in", archive, "-out", plain])
    contents = stage / "restored"
    contents.mkdir(mode=0o700)
    seen, total = set(), 0
    with tarfile.open(plain, "r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or
                    member.name in seen or not (member.isfile() or member.isdir())):
                raise RuntimeError("Unsafe or duplicate archive member")
            seen.add(member.name)
            total += member.size
            if total > 4 * 1024**3:
                raise RuntimeError("Backup exceeds the 4 GiB restore limit")
            target = contents.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o700)
            else:
                with tar.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o600)
    plain.unlink()
    return contents


def verify_files(contents):
    manifest = json.loads((contents / "manifest.json").read_text())
    actual = {str(p.relative_to(contents)): digest(p) for p in contents.rglob("*")
              if p.is_file() and p != contents / "manifest.json"}
    if manifest.get("format") != 1 or actual != manifest.get("files"):
        raise RuntimeError("Backup manifest or file hashes do not match")
    required = {"hub/bark-data/bark.db", "hub/ntfy-cache/cache.db", "hub/ntfy-lib/user.db",
                "hub/docker-compose.yml", "hub/Caddyfile", "runtime-image.tar", "metadata.json", "db-probe"}
    if not required.issubset(actual) or not any(p.startswith("hub/bark-certs/") for p in actual):
        raise RuntimeError("Backup is missing required recovery assets")
    for path in [contents / "hub/ntfy-cache/cache.db", contents / "hub/ntfy-lib/user.db"]:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Restored SQLite integrity check failed")
    return json.loads((contents / "metadata.json").read_text())


def database_check(contents, image):
    return json.loads(run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                           "--security-opt", "no-new-privileges:true", "--user", "0:0", "--entrypoint", "/db-probe",
                           "-v", str(contents / "db-probe") + ":/db-probe:ro",
                           "-v", str(contents / "hub/bark-data/bark.db") + ":/copy/bark.db:ro",
                           image, "--db", "/copy/bark.db"]))


def drill(contents, metadata):
    # Load the exported recovery image; never pull a mutable tag or use live mounts.
    run(["docker", "image", "load", "--input", contents / "runtime-image.tar"])
    image = metadata["image_id"]
    if not image.startswith("sha256:"):
        raise RuntimeError("Recovery image must use an immutable ID")
    (contents / "db-probe").chmod(0o755)
    database = database_check(contents, image)
    if database != metadata["database"]:
        raise RuntimeError("Restored registration contents do not match the original snapshot")
    data, certs = contents / "hub/bark-data", contents / "hub/bark-certs"
    for path in [data, *data.rglob("*")]:
        os.chown(path, 1000, 1000)
    certs.chmod(0o755)
    for path in certs.rglob("*"):
        if path.is_dir(): path.chmod(0o755)
        else: os.chown(path, 1000, 1000)
    name = "zbark-restore-" + uuid.uuid4().hex[:12]
    args = ["docker", "run", "-d", "--name", name, "--network", "none", "--user", "1000:1000",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "512m", "--pids-limit", "100", "--tmpfs", "/tmp",
            "-v", str(data) + ":/data", "-v", str(certs) + ":/certs:ro",
            "-e", "BARK_SERVER_REGISTRATION_MODE=existing"]
    env = metadata["environment"]
    if set(env) != ENVIRONMENT or env["BARK_APNS_ENV"] != "production":
        raise RuntimeError("Recovery APNs configuration is incomplete")
    for key, value in sorted(env.items()):
        args += ["-e", key + "=" + value]
    args += [image, "--addr", "0.0.0.0:8080", "--data", "/data"]
    try:
        run(args)
        state = inspect(name)
        if state["HostConfig"]["NetworkMode"] != "none" or state["HostConfig"].get("PortBindings"):
            raise RuntimeError("Restore container is not isolated")
        restored = wait_info(name)
        if any(restored.get(key) != metadata["info"].get(key) for key in ["commit", "devices"]):
            raise RuntimeError("Restored source or registration count does not match")
        result = {"restored_commit": restored["commit"], "registrations": restored["devices"],
                "network": "none", "published_ports": False, "live_data_mounted": False,
                "apns_delivery": "not attempted", "sqlite_integrity": "passed", **database}
    finally:
        run(["docker", "rm", "-f", name])
    if database_check(contents, image) != database:
        raise RuntimeError("Starting the recovered app changed registration contents")
    return result


def verify(archive, password, restore=False):
    expected = archive.with_name(archive.name + ".sha256").read_text().split()[0]
    if digest(archive) != expected:
        raise RuntimeError("Encrypted backup checksum mismatch")
    with tempfile.TemporaryDirectory(prefix=".zbark-restore-", dir=archive.parent) as directory:
        contents = unpack(archive, password, Path(directory))
        metadata = verify_files(contents)
        result = drill(contents, metadata) if restore else {"sqlite_integrity": "passed"}
    return {"archive": archive.name, "sha256": expected, "verified": True,
            "restore_drill": restore, **result}


def create(hub, password):
    backups = hub / "backups"
    backups.mkdir(exist_ok=True, mode=0o700)
    state = inspect("bark-server")
    env = dict(value.split("=", 1) for value in state["Config"]["Env"] if "=" in value)
    if not state["State"]["Running"] or env.get("BARK_SERVER_REGISTRATION_MODE") != "existing":
        raise RuntimeError("Backup requires a running Bark with existing-only registration")
    if env.get("BARK_APNS_KEY") or not ENVIRONMENT.issubset(env):
        raise RuntimeError("Backup requires explicit file-based APNs configuration")
    if not DB_PROBE.is_file():
        raise RuntimeError("Install the offline database probe before taking a backup")
    mounts = {m["Destination"]: m for m in state["Mounts"]}
    if (Path(mounts["/data"]["Source"]).resolve() != (hub / "bark-data").resolve() or
            Path(mounts["/certs"]["Source"]).resolve() != (hub / "bark-certs").resolve() or mounts["/certs"]["RW"]):
        raise RuntimeError("Bark mounts do not match the protected backup root")
    before = info(state["Id"])
    name = "notify-data-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".tar.gz.enc"
    archive = backups / name
    pending = archive.with_name(archive.name + ".partial")
    try:
        with tempfile.TemporaryDirectory(prefix=".zbark-backup-", dir=backups) as directory:
            stage = Path(directory)
            seconds = stopped_copy(state, hub / "bark-data", stage / "hub/bark-data")
            for folder in ["ntfy-cache", "ntfy-lib", "bark-certs"]:
                copy_tree(hub / folder, stage / "hub" / folder, sqlite=folder.startswith("ntfy"))
            for name in ["docker-compose.yml", "Caddyfile", "backup.sh"]:
                shutil.copyfile(hub / name, stage / "hub" / name)
            for name in HOST_FILES:
                source = Path("/") / name
                if source.is_file():
                    target = stage / "host" / name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, target)
            shutil.copyfile(Path(__file__).resolve(), stage / "backup-tool.py")
            shutil.copyfile(DB_PROBE, stage / "db-probe")
            (stage / "db-probe").chmod(0o755)
            run(["docker", "image", "save", "--output", stage / "runtime-image.tar", state["Image"]])
            metadata = {"format": 1, "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "image_id": state["Image"], "environment": {k: env[k] for k in ENVIRONMENT},
                        "info": before, "database": database_check(stage, state["Image"]), "bark_stop_start_seconds": seconds,
                        "consistency": {"bbolt": "writer stopped during copy", "sqlite": "online backup API"}}
            write_json(stage / "metadata.json", metadata)
            files = {str(p.relative_to(stage)): digest(p) for p in stage.rglob("*") if p.is_file()}
            write_json(stage / "manifest.json", {"format": 1, "files": files})
            encrypt(stage, pending, password)
        # Publish only after decryption, integrity checks and isolated runtime restoration pass.
        checksum = pending.with_name(pending.name + ".sha256")
        checksum.write_text(digest(pending) + "\n")
        report = verify(pending, password, restore=True)
        pending.rename(archive)
        checksum.rename(archive.with_name(archive.name + ".sha256"))
        report.update({"archive": archive.name, "created_at_utc": metadata["created_at_utc"],
                       "bark_stop_start_seconds": seconds, "consistency": metadata["consistency"]})
        write_json(archive.with_name(archive.name + ".json"), report)
        # Prune only completed archives after a new recovery point has passed its drill.
        cutoff = time.time() - 7 * 86400
        for old in backups.glob("notify-data-*.tar.gz.enc"):
            if old != archive and old.stat().st_mtime < cutoff:
                for path in [old, old.with_name(old.name + ".sha256"), old.with_name(old.name + ".json")]:
                    path.unlink(missing_ok=True)
        return report
    finally:
        pending.unlink(missing_ok=True)
        pending.with_name(pending.name + ".sha256").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "verify", "drill", "status"])
    parser.add_argument("--hub", type=Path, default=HUB)
    parser.add_argument("--password-file", type=Path, default=PASSWORD)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0: parser.error("run with sudo")
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.password_file.is_symlink() or not args.password_file.is_file() or args.password_file.stat().st_mode & 0o077:
            raise RuntimeError("Backup password file must be owner-only")
        with open("/run/lock/zbark-registration.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == "create":
                result = create(args.hub.resolve(), args.password_file.resolve())
            else:
                archive = args.archive
                if archive is None:
                    archives = sorted((args.hub / "backups").glob("notify-data-*.tar.gz.enc.json"))
                    if not archives: raise RuntimeError("No verified backup report exists")
                    archive = Path(str(archives[-1])[:-5])
                if args.command == "status":
                    result = json.loads(archive.with_name(archive.name + ".json").read_text())
                    age = time.time() - dt.datetime.fromisoformat(result["created_at_utc"]).timestamp()
                    if age < -300 or age > 36 * 3600 or not result.get("restore_drill"):
                        raise RuntimeError("Latest drilled backup is stale or invalid")
                    if digest(archive) != result["sha256"]: raise RuntimeError("Latest backup checksum mismatch")
                    state = inspect("bark-server")
                    environment = dict(v.split("=", 1) for v in state["Config"]["Env"] if "=" in v)
                    if not state["State"]["Running"] or environment.get("BARK_SERVER_REGISTRATION_MODE") != "existing":
                        raise RuntimeError("Live Bark is unhealthy or new enrollment is not closed")
                    if run(["systemctl", "show", "notify-backup.service", "-p", "Result", "--value"]).strip() != b"success":
                        raise RuntimeError("The daily backup service last failed")
                    if run(["systemctl", "is-active", "notify-backup.timer"]).strip() != b"active":
                        raise RuntimeError("The daily backup timer is not active")
                    result["age_seconds"] = round(age)
                    result["live"] = {"commit": info(state["Id"])["commit"], "registration_mode": "existing",
                                      "backup_timer": "active", "backup_service_result": "success"}
                else:
                    result = verify(archive.resolve(), args.password_file.resolve(), args.command == "drill")
        print(json.dumps(result, indent=2))
        return 0
    except RuntimeError as error:
        print(f"Backup operation failed: {error}. Earlier recovery points retained.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError):
        print("Backup operation failed; retained earlier recovery points. Inspect protected local state; secrets suppressed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Backup interrupted; Bark restart was attempted if its writer was stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__": sys.exit(main())
