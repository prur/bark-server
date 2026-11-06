#!/usr/bin/env python3
"""Closed enrollment permits existing-token refresh; timed open permits pairing."""
import argparse
import fcntl
import os
import subprocess
import sys
import time

COMPOSE = "/opt/notify-hub/docker-compose.yml"
SCRIPT = "/usr/local/bin/bark-register-window.sh"
LOCK = "/run/lock/zbark-registration.lock"


def execute(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def current():
    output = subprocess.check_output(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", "bark-server"], text=True)
    for line in output.splitlines():
        if line.startswith("BARK_SERVER_REGISTRATION_MODE="):
            return line.partition("=")[2]
    raise RuntimeError("server has no explicit registration policy; refusing to change enrollment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "open", "close", "disable", "autoclose"], nargs="?", default="status")
    parser.add_argument("minutes", nargs="?", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.minutes <= 30:
        parser.error("pairing window must be 1..30 minutes")
    if os.geteuid() != 0:
        parser.error("run with sudo")
    # Serialize timer/operator transitions so a stale callback cannot race disable.
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        apply(args)


def apply(args):
    mode = current()
    if args.command == "status":
        print(f"registration: {mode} (existing = new pairing closed; token refresh allowed)")
        return
    if args.command == "autoclose" and mode != "open":
        print(f"registration: {mode} (timer made no change)")
        return
    desired = {"open": "open", "close": "existing", "autoclose": "existing", "disable": "disabled"}[args.command]
    if desired == "open":
        # Schedule closure before exposing enrollment. If scheduling fails,
        # leave the current policy in place. Earlier timers remain fail-safe.
        execute(["systemd-run", f"--on-active={args.minutes}min", f"--unit=bark-register-autoclose-{time.time_ns()}",
                 "--collect", SCRIPT, "autoclose"], stdout=subprocess.DEVNULL)
    if mode != desired:
        environment = {**os.environ, "BARK_SERVER_REGISTRATION_MODE": desired}
        # Compose must declare ${BARK_SERVER_REGISTRATION_MODE:-existing}.
        config = subprocess.check_output(["docker", "compose", "-f", COMPOSE, "config", "--format", "json"], env=environment)
        import json
        if json.loads(config)["services"]["bark-server"]["environment"].get("BARK_SERVER_REGISTRATION_MODE") != desired:
            raise RuntimeError("Compose does not support the explicit policy override")
        execute(["docker", "compose", "-f", COMPOSE, "up", "-d", "--no-deps", "bark-server"], env=environment)
    if current() != desired:
        raise RuntimeError("container policy verification failed")
    print(f"registration: {desired}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Registration policy unchanged or update failed: {error}", file=sys.stderr)
        sys.exit(1)
