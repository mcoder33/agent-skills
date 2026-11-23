#!/usr/bin/env python3
"""Manage interactive Claude tasks in the VM's StealthBox tmux server."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


class TaskError(Exception):
    pass


def tmux(*args, check=True, input=None):
    result = subprocess.run(["tmux", "-L", "stealthbox", *args], input=input,
                            text=True, capture_output=True)
    if check and result.returncode:
        raise TaskError(result.stderr.strip() or "tmux command failed")
    return result


def save(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(state, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, path)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (path.parent / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read(path):
    if not path.exists():
        raise TaskError("unknown task")
    return json.loads(path.read_text())


def owns_pane(state):
    pane = state.get("pane_id")
    if not pane:
        return False
    result = tmux("display-message", "-p", "-t", pane,
                  "#{pid}\t#{pane_pid}\t#{@orchestrate_owner}", check=False)
    return (result.returncode == 0 and result.stdout.strip()
            == str(state.get("server_pid")) + "\t" + str(state.get("pane_pid"))
            + "\t" + state["generation"])


def refresh(state):
    if state["status"] in {"running", "answered", "failed", "closing"} and not owns_pane(state):
        state["status"] = "lost"
    return state


def prompt_text():
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise TaskError("provide the task prompt on stdin")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in prompt):
        raise TaskError("prompt contains terminal control characters")
    return prompt


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def new_turn(state, prompt, request_id):
    state.pop("error", None)
    state.update(turn=str(uuid.uuid4()), prompt=prompt, submitted=False,
                 answer=None, background_tasks=[], session_crons=[], status="running")
    state.setdefault("requests", {})[request_id] = {"prompt_sha256": digest(prompt), "turn": state["turn"]}


def repeated(state, request_id, prompt):
    request = state.get("requests", {}).get(request_id)
    if request and request["prompt_sha256"] != digest(prompt):
        raise TaskError("request-id already belongs to a different prompt")
    return request is not None


def hook_settings(args, generation):
    base = [sys.executable, str(Path(__file__).resolve()), "--config", str(args.config),
            "hook", args.task, "--generation", generation]
    return {"hooks": {
        event: [{"hooks": [{"type": "command", "command": shlex.join(base + ["--event", kind])}]}]
        for event, kind in (("UserPromptSubmit", "prompt"), ("PreToolUse", "activity"),
                            ("Stop", "stop"),
                            ("StopFailure", "failure"))
    }}


def create_window(args, path, state):
    state.update(generation=str(uuid.uuid4()), status="launching", pane_id=None,
                 window_id=None, server_pid=None, pane_pid=None)
    save(path, state)
    wrapper = "exec " + shlex.join(["env", "XDG_STATE_HOME=" + str(path.parents[3]),
                         sys.executable, str(Path(__file__).resolve()),
                         "--config", str(args.config), "run", args.task,
                         "--generation", state["generation"]])
    session = state["tmux_session"]
    exists = tmux("has-session", "-t", "=" + session, check=False).returncode == 0
    command = ["new-window", "-t", session] if exists else ["new-session", "-s", session]
    try:
        result = tmux(*command, "-d", "-P", "-F", "#{pane_id}\t#{window_id}\t#{pid}",
                      "-n", "claude-" + state["generation"][:8], wrapper)
        pane, window, server = result.stdout.strip().split("\t")
        state.update(pane_id=pane, window_id=window, server_pid=server)
        save(path, state)
    except (TaskError, ValueError):
        state["status"] = "launch_failed"
        save(path, state)
        raise


def require_paste_ready(pane):
    clients = tmux("list-clients", "-F", "#{client_flags}\t#{pane_id}").stdout.splitlines()
    for client in clients:
        flags, selected = client.split("\t", 1)
        if selected == pane and "read-only" not in flags.split(","):
            raise TaskError("task pane is selected by an interactive client; return control before sending input")
    flags = tmux("display-message", "-p", "-t", pane,
                 "#{bracket_paste_flag}\t#{pane_in_mode}\t#{pane_input_off}").stdout.rstrip("\n")
    if not flags.split("\t")[0]:
        raise TaskError("this tmux installation must expose bracket_paste_flag for safe input")
    if flags != "1\t0\t0":
        raise TaskError("pane is not ready for bracketed paste; inspect its terminal")


def paste(pane, text):
    buffer = "orchestrate-" + uuid.uuid4().hex
    tmux("load-buffer", "-b", buffer, "-", input=text)
    try:
        tmux("paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", pane)
        tmux("send-keys", "-t", pane, "Enter")
    finally:
        tmux("delete-buffer", "-b", buffer, check=False)


def launch_agent(args, path):
    with locked(path):
        state = read(path)
        if state["generation"] != args.generation or os.environ.get("TMUX_PANE") != state["pane_id"]:
            raise TaskError("stale pane launcher")
        tmux("set-option", "-p", "-t", state["pane_id"], "@orchestrate_owner", state["generation"])
        state.update(status="running", pane_pid=str(os.getpid()))
        save(path, state)
        command = [state["launcher"], "agent", "claude", "--config", str(args.config),
                   "--path", state["project"], "--"]
        command += ["--resume" if state.get("resume") else "--session-id", state["session_id"]]
        command += ["--settings", json.dumps(hook_settings(args, state["generation"])), "--", state["prompt"]]
    try:
        result = subprocess.run(command)
    except OSError as error:
        with locked(path):
            state = read(path)
            if state["generation"] == args.generation:
                state.update(status="launch_failed", error=str(error))
                save(path, state)
        raise
    with locked(path):
        state = read(path)
        if state["generation"] == args.generation:
            state.update(status="closed" if state["status"] == "closing" else "exited",
                         exit_code=result.returncode)
            save(path, state)
            if owns_pane(state):
                tmux("kill-pane", "-t", state["pane_id"], check=False)


def receive_hook(args, path):
    event = json.load(sys.stdin)
    with locked(path):
        state = read(path)
        if state["generation"] != args.generation or event.get("session_id") != state["session_id"]:
            return
        if args.event == "prompt":
            prompt = event.get("prompt", "")
            if state["status"] == "closing" and prompt.strip() == "/exit":
                return
            if state["submitted"] or prompt.strip() != state["prompt"].strip():
                new_turn(state, prompt, "manual-" + uuid.uuid4().hex)
            state.update(submitted=True, has_history=True, status="running")
        elif args.event == "activity":
            if state["status"] in {"answered", "failed", "closing"}:
                state.update(turn=str(uuid.uuid4()), answer=None)
            state["status"] = "running"
        elif args.event == "failure":
            state.update(status="failed", turn=str(uuid.uuid4()), error=event.get("error", "unknown"),
                         answer=event.get("last_assistant_message"))
        elif state["submitted"]:
            state.update(status="answered", has_history=True, turn=str(uuid.uuid4()),
                         answer=event.get("last_assistant_message", ""),
                         background_tasks=event.get("background_tasks", []),
                         session_crons=event.get("session_crons", []))
        save(path, state)


def public(state):
    keys = ("task", "status", "project", "session_id", "turn", "answer", "pane_id",
            "window_id", "background_tasks", "session_crons", "error")
    return {key: state[key] for key in keys if key in state}


def runtime_status(state):
    executable = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    try:
        result = subprocess.run([executable, "agents", "--json"], text=True,
                                capture_output=True, timeout=10)
        agents = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise TaskError("cannot verify Claude readiness with 'claude agents --json': " + str(error)) from error
    if not isinstance(agents, list):
        raise TaskError("this Claude installation must support 'claude agents --json'")
    for agent in agents:
        if not isinstance(agent, dict):
            raise TaskError("Claude returned malformed agent status")
        if agent.get("sessionId") == state["session_id"] and agent.get("kind") == "interactive":
            return agent.get("status", "unknown")
    return "unknown"


def ready_snapshot(args, path):
    if args.command not in {"reply", "finish"}:
        return None
    with locked(path):
        state = refresh(read(path))
    if state["status"] not in {"answered", "failed"}:
        return None
    if runtime_status(state) != "idle":
        raise TaskError("Claude is still active; check status before sending input")
    return state["generation"], state["turn"]


def expose(state, path):
    if state["status"] not in {"answered", "failed"}:
        return state
    try:
        ready = runtime_status(state) == "idle"
    except TaskError as error:
        return dict(state, status="blocked", readiness_error=str(error))
    with locked(path):
        current = public(refresh(read(path)))
    if current["turn"] != state["turn"]:
        return dict(current, status="settling") if current["status"] == "answered" else current
    if current["status"] in {"answered", "failed"} and not ready:
        current["status"] = "settling"
    return current


def manage(args, path):
    prompt = prompt_text() if args.command in {"start", "reply", "resume"} else None
    ready = ready_snapshot(args, path)
    with locked(path):
        if args.command == "start" and not path.exists():
            config = json.loads(args.config.read_text())
            workspace = config["workspace"]
            project = Path(args.project)
            if project.is_absolute() or ".." in project.parts:
                raise TaskError("project must be a relative path beneath the VM workspace")
            state = {"task": args.task, "project": args.project,
                     "launcher": str(Path(workspace["remote_dir"]) / "bin" / "stealthbox"),
                     "tmux_session": workspace.get("name") or "stealthbox",
                     "session_id": str(uuid.uuid4()), "resume": False, "has_history": False}
            new_turn(state, prompt, "start")
            create_window(args, path, state)
            return public(state)
        state = refresh(read(path))
        if ready is not None and ready != (state["generation"], state["turn"]):
            raise TaskError("task changed while checking readiness; read its current state")
        if args.command == "start":
            if args.project != state["project"] or not repeated(state, "start", prompt):
                raise TaskError("task already exists with different launch parameters")
        elif args.command in {"reply", "resume"}:
            if not repeated(state, args.request_id, prompt):
                if args.command == "reply":
                    if state["status"] not in {"answered", "failed"} or not owns_pane(state):
                        raise TaskError("reply needs an answered or failed task with a live owned pane")
                    if ready is None:
                        raise TaskError("task changed before readiness was checked; read its current state")
                    require_paste_ready(state["pane_id"])
                    new_turn(state, prompt, args.request_id)
                    save(path, state)
                    paste(state["pane_id"], prompt)
                else:
                    if state["status"] not in {"closed", "lost", "exited", "launch_failed"}:
                        raise TaskError("resume needs a closed, exited, lost, or failed launch")
                    state["resume"] = state.get("has_history", state["submitted"] or len(state["requests"]) > 1)
                    new_turn(state, prompt, args.request_id)
                    create_window(args, path, state)
        elif args.command == "finish":
            if args.turn != state["turn"]:
                raise TaskError("stale turn; read the current answer before finishing")
            if state["status"] in {"closed", "exited", "launch_failed"}:
                state["status"] = "closed"
                save(path, state)
                return public(state)
            if state["status"] == "closing":
                return public(state)
            if state["status"] not in {"answered", "failed"}:
                raise TaskError("finish needs the current answered or failed turn")
            if ready is None:
                raise TaskError("task changed before readiness was checked; read its current state")
            if state["background_tasks"] or state["session_crons"]:
                raise TaskError("Claude still has background work")
            if not owns_pane(state):
                raise TaskError("owned pane is no longer present")
            require_paste_ready(state["pane_id"])
            state["status"] = "closing"
            save(path, state)
            paste(state["pane_id"], "/exit")
        save(path, state)
        return public(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".stealthbox/config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "reply", "resume", "finish", "run", "hook"):
        command = commands.add_parser(name)
        command.add_argument("task")
        if name == "start":
            command.add_argument("--project", required=True)
        if name in {"reply", "resume"}:
            command.add_argument("--request-id", required=True)
        if name == "finish":
            command.add_argument("--turn", required=True)
        if name in {"run", "hook"}:
            command.add_argument("--generation", required=True)
        if name == "hook":
            command.add_argument("--event", choices=("prompt", "stop", "activity", "failure"), required=True)
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    key = digest(str(args.config) + "\0" + args.task)
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = state_root / "orchestrate" / "claude-tasks" / key / "state.json"
    try:
        if args.command == "run":
            launch_agent(args, path)
        elif args.command == "hook":
            receive_hook(args, path)
        else:
            state = manage(args, path)
            if args.command == "finish" and state["status"] == "closing":
                for attempt in range(30):
                    time.sleep(0.1)
                    with locked(path):
                        state = public(refresh(read(path)))
                    if state["status"] != "closing":
                        break
            print(json.dumps(expose(state, path), ensure_ascii=False))
    except (TaskError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
