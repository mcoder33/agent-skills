#!/usr/bin/env python3
"""Read Claude and Kimi Code subscription quotas for an orchestration decision."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import select
import shlex
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from stealthbox_claude import default_profile, read_workspace, ssh_command


CLAUDE_USAGE = """
import json, subprocess, sys
from pathlib import Path

directory = Path(sys.argv[1])
config = directory / "config.json"
project_root = json.loads(config.read_text())["workspace"]["vm_root"]
command = [str(directory / "bin/stealthbox"), "agent", "claude",
           "--config", str(config), "--path", project_root,
           "--", "--safe-mode", "--print", "/usage"]
sys.exit(subprocess.run(command, stdin=subprocess.DEVNULL, timeout=18).returncode)
"""
CLAUDE_WINDOW = re.compile(
    r"^Current (session|week)(?: \(([^)]+)\))?: (\d{1,3})% used(?: · resets (.+))?$")
KIMI_WINDOWS = {"limit5h": "5h", "limit7d": "7d",
                "monthTotal": "month_total", "monthCode": "month_code"}
KIMI_PROVIDER = "managed:kimi-code"


class QuotaUnavailable(Exception):
    pass


def quota_window(name, used, reset):
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise QuotaUnavailable("invalid usage percentage")
    return {"window": name, "remaining_pct": round(100 - used, 4), "reset": reset}


def parse_claude_usage(report):
    lower = report.lower()
    if any(marker in lower for marker in (
            "last-known usage", "couldn't load usage data", "failed to load usage data",
            "rate limited", "temporarily limiting requests")):
        raise QuotaUnavailable("Claude /usage is stale or unavailable")
    windows = []
    for line in report.splitlines():
        match = CLAUDE_WINDOW.fullmatch(line.strip())
        if not match:
            continue
        period, scope, used, reset = match.groups()
        name = "5h" if period == "session" else "7d"
        if scope and scope.lower() != "all models":
            name += ":" + scope
        windows.append(quota_window(name, int(used), reset))
    if not {"5h", "7d"} <= {window["window"] for window in windows}:
        raise QuotaUnavailable("Claude /usage did not report both shared plan windows")
    return windows


def claude_usage(profile):
    workspace = read_workspace(profile)
    command = shlex.join(["python3", "-c", CLAUDE_USAGE, workspace["remote_dir"]])
    result = subprocess.run(ssh_command(workspace, command), capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=30)
    return parse_claude_usage(result.stdout)


def parse_kimi_usage(data):
    if data.get("kind") != "ok":
        raise QuotaUnavailable("Kimi did not return subscription usage")
    usages = data["quota"]["usages"]
    windows = []
    for key, name in KIMI_WINDOWS.items():
        if key in usages:
            entry = usages[key]
            ratio = entry["usedRatio"]
            if type(ratio) not in (int, float) or not 0 <= ratio <= 1:
                raise QuotaUnavailable("invalid Kimi usage ratio")
            windows.append(quota_window(name, ratio * 100, entry.get("resetAt")))
    if not windows:
        raise QuotaUnavailable("Kimi reported no plan windows")
    return windows


def kimi_windows(request):
    config = request("config")
    model = config["models"][config["default_model"]]
    if model.get("provider") != KIMI_PROVIDER:
        raise QuotaUnavailable("Kimi's default model does not use the Kimi Code subscription")
    return parse_kimi_usage(request("oauth/usage"))


def kimi_usage(directory):
    with tempfile.TemporaryDirectory(prefix="orchestrate-kimi-quota-") as scratch:
        with subprocess.Popen(["kimi", "web", "--json", "--host", "127.0.0.1", "--port", "0"],
                              cwd=directory or scratch, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as server:
            try:
                if not select.select([server.stdout], [], [], 8)[0]:
                    raise QuotaUnavailable("Kimi local usage API did not start")
                ready = json.loads(server.stdout.readline())
                url = urlsplit(ready["origin"])
                if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
                    raise QuotaUnavailable("Kimi local usage API is not bound to loopback")
                origin = f"http://127.0.0.1:{url.port}"
                opener = build_opener(ProxyHandler({}))
                headers = {"Authorization": "Bearer " + ready["token"]}

                def request(path):
                    with opener.open(Request(f"{origin}/api/v1/{path}", headers=headers), timeout=8) as response:
                        return json.load(response)["data"]

                return kimi_windows(request)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def probe(read, argument):
    try:
        windows = read(argument)
    except QuotaUnavailable as error:
        return {"status": "unknown", "reason": str(error)}
    except FileNotFoundError:
        return {"status": "unknown", "reason": "client or profile not found"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return {"status": "unknown", "reason": "client or usage service did not return a usable result"}
    return {"status": "ok", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "windows": windows}


def read_quotas(profile, directory):
    with ThreadPoolExecutor(max_workers=2) as executor:
        claude = executor.submit(probe, claude_usage, profile)
        kimi = executor.submit(probe, kimi_usage, directory)
        return {"claude": claude.result(), "kimi": kimi.result()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-profile", type=Path, default=default_profile())
    parser.add_argument("--kimi-cwd", type=Path, help="Working copy Kimi will use")
    args = parser.parse_args()
    print(json.dumps(read_quotas(args.claude_profile, args.kimi_cwd), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
