#!/usr/bin/env python3
"""Run one Claude task operation on the VM of a local StealthBox profile."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys


# The hook needs a stable script on the VM after this SSH connection closes.
# Publish an immutable copy so updating the skill cannot replace a running hook.
REMOTE = """
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

request = json.load(sys.stdin)
source = request["helper"].encode()
directory = Path(os.environ.get("XDG_DATA_HOME") or Path.home()/".local/share")/"codex-orchestrate"
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
helper = directory / ("claude-task-" + hashlib.sha256(source).hexdigest() + ".py")
if not helper.exists():
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
        tmp.write(source)
    os.replace(tmp.name, helper)
result = subprocess.run([sys.executable, str(helper), *request["args"]],
                        input=request["prompt"].encode())
sys.exit(result.returncode)
"""


def default_profile():
    if os.environ.get("STEALTHBOX_CONFIG"):
        return Path(os.environ["STEALTHBOX_CONFIG"])
    config_home = Path.home() / ".config"
    selected = config_home / "orchestrate/stealthbox.json"
    return selected if selected.exists() or selected.is_symlink() else config_home / "stealthbox/config.json"


class InvalidWorkspace(ValueError):
    pass


def read_workspace(profile):
    workspace = json.loads(profile.expanduser().read_text())["workspace"]
    host = workspace["host"]
    if (not isinstance(host, str) or not host or host.startswith("-")
            or not PurePosixPath(workspace["remote_dir"]).is_absolute()):
        raise InvalidWorkspace("profile requires a VM SSH host and an absolute remote_dir")
    return workspace


def ssh_command(workspace, remote_command):
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "--", workspace["host"], remote_command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=default_profile(),
                        help="Local StealthBox profile JSON")
    parser.add_argument("action", choices=("start", "status", "reply", "finish", "resume"))
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Task ID and operation arguments")
    args = parser.parse_args()
    try:
        workspace = read_workspace(args.profile)
        directory = PurePosixPath(workspace["remote_dir"])
        source = Path(__file__).with_name("claude_task.py").read_text(encoding="utf-8")
        request = {
            "args": ["--config", str(directory / "config.json"), args.action, *args.args],
            "helper": source,
            "prompt": sys.stdin.read() if args.action in ("start", "reply", "resume") else "",
        }
        remote_command = "python3 -c " + shlex.quote(REMOTE)
        result = subprocess.run(
            ssh_command(workspace, remote_command),
            input=json.dumps(request).encode())
        return result.returncode
    except InvalidWorkspace as exc:
        parser.error(str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"stealthbox_claude: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
