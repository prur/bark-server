import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("zbark_build", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def test_relative_output_is_one_location_for_binary_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            original = Path.cwd()
            def compile(args, **kwargs):
                binary = Path(args[args.index("-o") + 1])
                self.assertTrue(binary.is_absolute())
                self.assertEqual(binary, root / "build/release/bark-server")
                binary.write_bytes(b"offline-build-fixture")
            try:
                os.chdir(root / "scripts")
                with patch.object(sys, "argv", ["build", "--output", "../build/release"]), \
                     patch.object(build, "ROOT", root), \
                     patch.object(build, "output", side_effect=["", "a" * 40, "2026-11-06T00:00:00Z", "go fixture"]), \
                     patch.object(build.subprocess, "run", side_effect=compile):
                    build.main()
                report = json.loads((root / "build/release/build.json").read_text())
                self.assertEqual(report["binary"], str(root / "build/release/bark-server"))
            finally:
                os.chdir(original)
