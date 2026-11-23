#!/usr/bin/env python3
"""Ask the installed Kimi Code CLI one question through its local ACP endpoint."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys


class KimiError(Exception):
    pass


class Connection:
    def __init__(self, process):
        self.process = process
        self.sequence = 0

    def send(self, **fields):
        stream = self.process.stdin
        stream.write(json.dumps({"jsonrpc": "2.0", **fields}) + "\n")
        stream.flush()

    def request(self, method, params, on_update=None):
        self.sequence += 1
        self.send(id=self.sequence, method=method, params=params)
        for line in self.process.stdout:
            message = json.loads(line)
            if "method" in message:
                if "id" in message:
                    self.handle_request(message)
                elif message["method"] == "session/update" and on_update:
                    on_update(message["params"])
            elif message.get("id") == self.sequence:
                if "error" in message:
                    raise KimiError(f"{method}: {message['error']['message']}")
                return message["result"]
        raise KimiError(f"Kimi closed the connection during {method}")

    def handle_request(self, message):
        if message["method"] == "session/request_permission":
            self.send(id=message["id"], result={"outcome": {"outcome": "cancelled"}})
            title = message["params"].get("toolCall", {}).get("title", "tool call")
            raise KimiError(f"Kimi requested permission: {title}")
        self.send(id=message["id"], error={"code": -32601, "message": "Unsupported client method"})


@contextmanager
def connect(directory):
    process = subprocess.Popen(["kimi", "acp"], cwd=directory, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        yield Connection(process)
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def select_option(connection, session, options, key, requested):
    selector = next((item for item in options if item["id"] == key), {})
    choices = [item for group in selector.get("options", []) for item in group.get("options", [group])]
    values = [item["value"] for item in choices]
    if requested in values:
        value = requested
    else:
        matches = [item["value"] for item in choices
                   if requested.casefold() == item.get("name", "").casefold()
                   or key == "model" and requested == item["value"].rsplit("/", 1)[-1]]
        if len(matches) != 1:
            raise KimiError(f"Unknown or ambiguous {key} {requested!r}. Available: {', '.join(values) or 'none'}")
        value = matches[0]
    if selector.get("currentValue") == value:
        return options
    changed = connection.request("session/set_config_option",
                                 {"sessionId": session, "configId": key, "value": value})
    return changed["configOptions"]


def ask(connection, args, directory, prompt):
    hello = connection.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    if hello.get("protocolVersion") != 1:
        raise KimiError("This client requires ACP protocol version 1")
    params = {"cwd": str(directory), "mcpServers": []}
    if args.resume:
        params["sessionId"] = args.resume
    opened = connection.request("session/resume" if args.resume else "session/new", params)
    session = args.resume or opened["sessionId"]
    print(f"Kimi session {session}", file=sys.stderr, flush=True)
    options = opened.get("configOptions", [])
    for key, requested in (("model", args.model), ("thinking", args.effort)):
        if requested is not None:
            options = select_option(connection, session, options, key, requested)

    chunks = []

    def collect(event):
        update = event["update"]
        if event.get("sessionId") == session and update.get("sessionUpdate") == "agent_message_chunk":
            content = update["content"]
            if content.get("type") == "text":
                chunks.append(content["text"])

    turn = connection.request("session/prompt",
                              {"sessionId": session, "prompt": [{"type": "text", "text": prompt}]}, collect)
    answer = "".join(chunks)
    if turn.get("stopReason") != "end_turn" or not answer.strip():
        raise KimiError(f"Turn ended without a complete text answer: {turn.get('stopReason')}")
    try:
        connection.request("session/close", {"sessionId": session})
    except (KimiError, OSError) as error:
        print(f"Answer received. Session close: {error}", file=sys.stderr)
    chosen = {item["id"]: item.get("currentValue") for item in options}
    return {"session_id": session, "cwd": str(directory), "model": chosen.get("model"),
            "effort": chosen.get("thinking"), "answer": answer}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, help="Local project directory")
    parser.add_argument("--resume", help="Continue this Kimi session")
    parser.add_argument("--model", help="Configured model alias or its unique short name")
    parser.add_argument("--effort", help="Thinking level offered by the selected model")
    args = parser.parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip():
        parser.error("supply the question on stdin")
    try:
        if args.cwd is None:
            directory = Path.home() / ".local/share/codex-kimi/chat"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            directory = args.cwd.expanduser().resolve(strict=True)
        with connect(directory) as connection:
            result = ask(connection, args, directory, prompt)
        print(json.dumps(result, ensure_ascii=False))
    except (KimiError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"kimi: {error}\n")


if __name__ == "__main__":
    main()
