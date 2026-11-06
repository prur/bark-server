"""Backup recovery tests use temporary SQLite databases and an ephemeral password."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("backup", Path(__file__).resolve().parents[1] / "deploy/zbark_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def test_sqlite_online_copy_includes_wal_and_leaves_live_database_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.closing(sqlite3.connect(root / "live.db")) as live:
                live.execute("PRAGMA journal_mode=WAL")
                live.execute("CREATE TABLE events (body TEXT)")
                live.execute("INSERT INTO events VALUES ('fixture-private-content')")
                live.commit()
                self.assertTrue((root / "live.db-wal").is_file())
                backup.sqlite_copy(root / "live.db", root / "copy.db")
                with contextlib.closing(sqlite3.connect(root / "copy.db")) as restored:
                    self.assertEqual(restored.execute("SELECT body FROM events").fetchall(),
                                     [("fixture-private-content",)])
                self.assertEqual(live.execute("SELECT count(*) FROM events").fetchone(), (1,))

    def test_copy_failure_and_stop_failure_restart_the_exact_original_container(self):
        for stop_fails in [False, True]:
            calls = []
            def run(args, **_):
                calls.append(args)
                if stop_fails and args[1] == "stop": raise RuntimeError("stop timeout")
                return b""
            with patch.object(backup, "run", side_effect=run), \
                 patch.object(backup, "inspect", return_value={"State": {"Running": False}}), \
                 patch.object(backup, "copy_tree", side_effect=OSError("disk full")), \
                 patch.object(backup, "wait_info") as health:
                with self.assertRaises((OSError, RuntimeError)):
                    backup.stopped_copy({"Id": "original-container-id"}, Path("source"), Path("copy"))
                self.assertEqual(calls[-1], ["docker", "start", "original-container-id"])
                health.assert_called_once_with("original-container-id")

    def fixture(self, root):
        stage = root / "stage"
        stage.mkdir()
        for name in ["hub/bark-data/bark.db", "hub/docker-compose.yml", "hub/Caddyfile",
                     "hub/bark-certs/fixture.p8", "runtime-image.tar", "db-probe"]:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private-fixture-content")
        for name in ["hub/ntfy-cache/cache.db", "hub/ntfy-lib/user.db"]:
            path = stage / name
            path.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE events (id INTEGER)")
                db.execute("INSERT INTO events VALUES (1)")
                db.commit()
        backup.write_json(stage / "metadata.json", {"info": {"devices": 3}})
        files = {str(p.relative_to(stage)): backup.digest(p) for p in stage.rglob("*") if p.is_file()}
        backup.write_json(stage / "manifest.json", {"format": 1, "files": files})
        password = root / "password"
        password.write_text("ephemeral-fixture-password")
        password.chmod(0o600)
        archive = root / "fixture.enc"
        backup.encrypt(stage, archive, password)
        archive.with_name(archive.name + ".sha256").write_text(backup.digest(archive) + "\n")
        return stage, password, archive

    def test_real_encrypted_round_trip_rejects_corruption_and_wrong_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, password, archive = self.fixture(root)
            self.assertNotIn(b"private-fixture-content", archive.read_bytes())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            report = backup.verify(archive, password)
            self.assertTrue(report["verified"])
            wrong = root / "wrong"
            wrong.write_text("wrong-fixture-password")
            with self.assertRaises(RuntimeError): backup.verify(archive, wrong)
            encrypted = bytearray(archive.read_bytes())
            encrypted[len(encrypted) // 2] ^= 1
            archive.write_bytes(encrypted)
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                backup.verify(archive, password)
            self.assertFalse(list(root.glob(".zbark-restore-*")))

    def test_manifest_detects_modified_and_missing_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, _, _ = self.fixture(Path(directory))
            (stage / "hub/bark-data/bark.db").write_bytes(b"altered")
            with self.assertRaisesRegex(RuntimeError, "hashes"):
                backup.verify_files(stage)

    def test_archive_rejects_traversal_symlinks_and_duplicate_files(self):
        for kind in ["traversal", "symlink", "duplicate"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "unsafe.enc"
                plain = root / "unsafe.tar.gz"
                with tarfile.open(plain, "w:gz") as tar:
                    member = tarfile.TarInfo("../outside" if kind == "traversal" else "inside")
                    if kind == "symlink":
                        member.type, member.linkname = tarfile.SYMTYPE, "../outside"
                    tar.addfile(member, io.BytesIO())
                    if kind == "duplicate": tar.addfile(member, io.BytesIO())
                password = root / "password"
                password.write_text("fixture-password")
                backup.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                            "-pass", "file:" + str(password), "-in", plain, "-out", archive])
                target = root / "unpack"
                target.mkdir()
                with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                    backup.unpack(archive, password, target)
                self.assertFalse((root / "outside").exists())

    def test_drill_uses_only_restored_mounts_no_network_and_always_removes_container(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = Path(directory)
            for name in ["hub/bark-data", "hub/bark-certs"]:
                (contents / name).mkdir(parents=True)
            (contents / "db-probe").write_bytes(b"fixture")
            metadata = {"image_id": "sha256:fixture-image", "info": {"commit": "fixture", "devices": 3}, "database": {"fixture": "same"},
                        "environment": {k: "production" if k == "BARK_APNS_ENV" else "fixture" for k in backup.ENVIRONMENT}}
            calls = []
            with patch.object(backup, "run", side_effect=lambda args: calls.append(args) or b""), \
                 patch.object(backup.os, "chown"), \
                 patch.object(backup, "database_check", return_value=metadata["database"]), \
                 patch.object(backup, "inspect", return_value={"HostConfig": {"NetworkMode": "none", "PortBindings": {}}}), \
                 patch.object(backup, "wait_info", side_effect=RuntimeError("fixture startup failure")):
                with self.assertRaises(RuntimeError): backup.drill(contents, metadata)
            create = next(a for a in calls if a[1] == "run")
            self.assertEqual(create[create.index("--network") + 1], "none")
            mounts = [create[i + 1] for i, value in enumerate(create) if value == "-v"]
            self.assertTrue(all(str(contents) in mount for mount in mounts))
            self.assertNotIn("-p", create)
            self.assertEqual(calls[-1][:3], ["docker", "rm", "-f"])

    def test_failed_drill_never_publishes_or_prunes_previous_recovery_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, password, old = self.fixture(root)
            hub = stage / "hub"
            (hub / "backup.sh").write_text("fixture backup wrapper")
            backups = hub / "backups"
            backups.mkdir()
            previous = backups / "notify-data-20000101_000000.tar.gz.enc"
            previous.write_bytes(old.read_bytes())
            backup.os.utime(previous, (1, 1))
            state = {"Id": "fixture-container", "Image": "sha256:fixture", "State": {"Running": True},
                     "Config": {"Env": ["BARK_SERVER_REGISTRATION_MODE=existing"] +
                                [k + "=production" for k in backup.ENVIRONMENT]},
                     "Mounts": [{"Destination": "/data", "Source": str(hub / "bark-data"), "RW": True},
                                {"Destination": "/certs", "Source": str(hub / "bark-certs"), "RW": False}]}
            def copied(_, source, destination):
                backup.copy_tree(source, destination)
                return .1
            def run(args, **_):
                if args[:3] == ["docker", "image", "save"]: Path(args[4]).write_bytes(b"fixture-image")
                return b""
            with patch.object(backup, "inspect", return_value=state), \
                 patch.object(backup, "info", return_value={"commit": "fixture", "devices": 3}), \
                 patch.object(backup, "DB_PROBE", stage / "db-probe"), \
                 patch.object(backup, "stopped_copy", side_effect=copied), \
                 patch.object(backup, "database_check", return_value={"fixture": "same"}), \
                 patch.object(backup, "run", side_effect=run), \
                 patch.object(backup, "verify", side_effect=RuntimeError("drill failed")):
                with self.assertRaisesRegex(RuntimeError, "drill failed"):
                    backup.create(hub, password)
            self.assertEqual(list(backups.iterdir()), [previous])

    def test_offline_probe_uses_file_owner_without_dac_override_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = Path(directory)
            database = contents / "hub/bark-data/bark.db"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"fixture")
            owner = database.stat()
            with patch.object(backup, "run", return_value=b'{"registration_count":3}') as run:
                backup.database_check(contents, "sha256:fixture")
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("--user") + 1], f"{owner.st_uid}:{owner.st_gid}")
            self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")


if __name__ == "__main__": unittest.main()
