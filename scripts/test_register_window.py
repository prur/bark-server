import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("register_window", Path(__file__).resolve().parents[1] / "deploy/bark-register-window.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class RegisterWindowTests(unittest.TestCase):
    def test_timer_failure_never_opens_enrollment(self):
        with patch.object(sys, "argv", ["policy", "open", "2"]), patch.object(policy.os, "geteuid", return_value=0), \
             patch.object(policy, "current", return_value="existing"), \
             patch.object(policy, "execute", side_effect=subprocess.CalledProcessError(1, "systemd-run")) as execute, \
             patch.object(policy.subprocess, "check_output") as output:
            with self.assertRaises(subprocess.CalledProcessError): policy.main()
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0][0], "systemd-run")
            output.assert_not_called()

    def test_open_schedules_close_before_service_update(self):
        calls = []
        config = json.dumps({"services": {"bark-server": {"environment": {"BARK_SERVER_REGISTRATION_MODE": "open"}}}}).encode()
        with patch.object(sys, "argv", ["policy", "open", "2"]), patch.object(policy.os, "geteuid", return_value=0), \
             patch.object(policy, "current", side_effect=["existing", "open"]), \
             patch.object(policy, "execute", side_effect=lambda args, **kw: calls.append(args)), \
             patch.object(policy.subprocess, "check_output", return_value=config):
            policy.main()
        self.assertEqual(calls[0][0], "systemd-run")
        self.assertIn("close", calls[0])
        self.assertEqual(calls[1][:3], ["docker", "compose", "-f"])
        self.assertEqual(calls[1][-1], "bark-server")
        self.assertIn("--no-deps", calls[1])

    def test_close_is_idempotent_and_preserves_existing_refresh(self):
        with patch.object(sys, "argv", ["policy", "close"]), patch.object(policy.os, "geteuid", return_value=0), \
             patch.object(policy, "current", return_value="existing"), patch.object(policy, "execute") as execute:
            policy.main()
            execute.assert_not_called()

    def test_unbounded_window_rejected_before_docker(self):
        with patch.object(sys, "argv", ["policy", "open", "1000"]), patch.object(policy, "current") as current:
            with self.assertRaises(SystemExit) as result: policy.main()
            self.assertEqual(result.exception.code, 2)
            current.assert_not_called()


if __name__ == "__main__": unittest.main()
