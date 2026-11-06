#!/usr/bin/env python3
"""Build a traceable static Z Bark server binary from committed source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def output(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os", choices=["linux", "darwin"], default="linux")
    parser.add_argument("--arch", choices=["amd64", "arm64"], default="amd64")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if output("git", "-C", str(ROOT), "status", "--porcelain"):
        parser.error("commit source before building a release binary")
    commit = output("git", "-C", str(ROOT), "rev-parse", "HEAD")
    date = output("git", "-C", str(ROOT), "show", "-s", "--format=%cI", "HEAD")
    destination = (args.output or ROOT / "build" / f"{commit[:12]}-{args.os}-{args.arch}").resolve()
    destination.mkdir(parents=True, exist_ok=False)
    binary = destination / "bark-server"
    flags = f"-s -w -X=main.version=zbark-{commit[:12]} -X=main.commitID={commit} -X=main.buildDate={date}"
    env = {**os.environ, "CGO_ENABLED": "0", "GOOS": args.os, "GOARCH": args.arch}
    subprocess.run(["go", "build", "-mod=readonly", "-trimpath", "-buildvcs=false", "-ldflags", flags, "-o", str(binary), "."], cwd=ROOT, env=env, check=True)
    manifest = {"commit": commit, "build_date": date, "go": output("go", "version"), "os": args.os, "arch": args.arch,
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "binary": str(binary.resolve())}
    (destination / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
