"""Exercise the public task CLI against an isolated tmux and a terminal agent."""
import json
import os
import pty
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "skills/orchestrate/scripts/claude_task.py"
FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, termios, tty, uuid
args = sys.argv[1:]
settings_value = args[args.index('--settings') + 1]
settings = json.loads(settings_value) if settings_value.startswith('{') else json.loads(pathlib.Path(settings_value).read_text())
resume = '--resume' in args
flag = '--resume' if resume else '--session-id'
session = args[args.index(flag) + 1] if flag in args else str(uuid.uuid4())
prompt = args[-1]
log = pathlib.Path(os.environ['FAKE_AGENT_LOG'])
registry = pathlib.Path(os.environ['FAKE_AGENT_REGISTRY'])
def set_status(status):
    temporary = registry.with_suffix('.tmp')
    temporary.write_text(json.dumps([{'sessionId': session, 'kind': 'interactive', 'status': status}]))
    temporary.replace(registry)
def answer(text):
    set_status('busy')
    with log.open('a') as out:
        out.write(json.dumps({'session_id': session, 'resume': resume, 'prompt': text, 'args': args}) + '\n')
    payload = {'hook_event_name': 'Stop', 'session_id': session,
               'last_assistant_message': 'answer:' + text, 'stop_hook_active': False,
               'cwd': os.getcwd(), 'transcript_path': str(log)}
    payload['error'] = 'rate_limit'
    for event in ('UserPromptSubmit', 'StopFailure' if text == 'FAIL' else 'Stop'):
        payload['hook_event_name'] = event
        payload['prompt'] = text
        for matcher in settings.get('hooks', {}).get(event, []):
            for hook in matcher['hooks']:
                subprocess.run(hook['command'], shell=True, input=json.dumps(payload), text=True, check=True)
    display = {'hook_event_name': 'MessageDisplay', 'session_id': session,
               'cwd': os.getcwd(), 'transcript_path': str(log),
               'turn_id': str(uuid.uuid4()), 'message_id': str(uuid.uuid4()),
               'index': 0, 'final': False, 'delta': 'answer:' + text + '\n'}
    for index, final, delta in ((0, False, 'answer:' + text + '\n'), (1, True, '')):
        display.update(index=index, final=final, delta=delta)
        for matcher in settings.get('hooks', {}).get('MessageDisplay', []):
            for hook in matcher['hooks']:
                subprocess.run(hook['command'], shell=True, input=json.dumps(display), text=True, check=True)
    set_status('idle')
saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    os.write(1, b'\x1b[?2004h')
    answer(prompt)
    text = b''
    paste = False
    while True:
        byte = os.read(0, 1)
        if not byte:
            break
        if byte == b'\x1b':
            sequence = byte + os.read(0, 5)
            if sequence == b'\x1b[200~':
                paste = True
            elif sequence == b'\x1b[201~':
                paste = False
            else:
                text += sequence
        elif byte in (b'\r', b'\n') and not paste:
            if text:
                if text == b'/exit':
                    break
                answer(text.decode())
                text = b''
        else:
            text += b'\n' if paste and byte == b'\r' else byte
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
    registry.unlink(missing_ok=True)
'''


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for lifecycle integration")
class ClaudeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oct-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.socket_dir = self.root / "tmux"
        self.socket_dir.mkdir(mode=0o700)
        self.remote = self.home / ".stealthbox"
        (self.remote / "bin").mkdir(parents=True)
        launcher = self.remote / "bin/stealthbox"
        launcher.write_text(FAKE_AGENT.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        launcher.chmod(0o700)
        self.projects = self.root / "projects"
        (self.projects / "team/repo").mkdir(parents=True)
        self.config = self.remote / "config.json"
        self.config.write_text(json.dumps({"workspace": {
            "name": "tests", "remote_dir": str(self.remote),
            "vm_root": str(self.projects), "host": "", "runner": "vm"
        }, "projects": {}}))
        self.log = self.root / "agent.jsonl"
        self.registry = self.root / "agents.json"
        (self.home / ".local/bin").mkdir(parents=True)
        probe = self.home / ".local/bin/claude"
        probe.write_text("#!" + sys.executable + "\n"
                         "import os,pathlib,sys\n"
                         "p=pathlib.Path(os.environ['FAKE_AGENT_REGISTRY'])\n"
                         "assert sys.argv[1:]==['agents','--json']\n"
                         "print(p.read_text() if p.exists() else '[]')\n")
        probe.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home),
                        XDG_STATE_HOME=str(self.root / "state"),
                        TMUX_TMPDIR=str(self.socket_dir), FAKE_AGENT_LOG=str(self.log),
                        FAKE_AGENT_REGISTRY=str(self.registry),
                        PATH=str(self.home / ".local/bin") + os.pathsep + os.environ.get("PATH", ""))
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)

    def tearDown(self):
        self.tmux("kill-server", check=False)
        self.temp.cleanup()

    def tmux(self, *args, check=True):
        return subprocess.run(["tmux", "-L", "stealthbox", *args],
                              env=self.env, text=True, capture_output=True, check=check)

    def command(self, *args, prompt=None, check=True):
        if check and args[0] in {"reply", "finish"}:
            flags = self.tmux("list-panes", "-a", "-F", "#{bracket_paste_flag}", check=False)
            if flags.returncode == 0 and not flags.stdout.strip():
                self.skipTest("tmux lacks bracket_paste_flag required for verified terminal input")
        result = subprocess.run([sys.executable, str(HELPER), "--config", str(self.config), *args],
                                input=prompt, env=self.env, text=True, capture_output=True,
                                timeout=12)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)
        return result

    def wait_state(self, task, expected):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.command("status", task)
            if state["status"] == expected:
                return state
            time.sleep(0.05)
        self.fail("agent did not reach " + expected + ": " + json.dumps(state))

    def answered(self, task):
        return self.wait_state(task, "answered")

    def entries(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def deliver_stop(self, session_id, answer):
        args = self.entries()[0]["args"]
        settings = json.loads(args[args.index("--settings") + 1])
        event = {"hook_event_name": "Stop", "session_id": session_id,
                 "last_assistant_message": answer, "stop_hook_active": False}
        for matcher in settings["hooks"]["Stop"]:
            for hook in matcher["hooks"]:
                subprocess.run(hook["command"], shell=True, input=json.dumps(event),
                               env=self.env, text=True, capture_output=True, check=True)

    def test_display_after_stop_preserves_final_answer(self):
        self.command("start", "review", "--project", "team/repo", prompt="final answer")
        state = self.answered("review")
        self.assertEqual(state["answer"], "answer:final answer")
        self.assertEqual(json.loads(self.registry.read_text())[0]["status"], "idle")

    def test_answer_reply_finish_and_resume_preserve_conversation(self):
        self.command("start", "review", "--project", "team/repo", prompt="first question")
        first = self.answered("review")
        self.assertEqual(first["answer"], "answer:first question")
        prompt = "literal $HOME and $(touch accidental)\nsecond line; 'quoted'"
        self.command("reply", "review", "--request-id", "reply-one", prompt=prompt)
        second = self.answered("review")
        self.assertEqual(second["answer"], "answer:" + prompt)
        self.assertFalse((self.projects / "team/repo/accidental").exists())
        self.assertNotEqual(first["turn"], second["turn"])
        self.assertNotEqual(self.command("finish", "review", "--turn", first["turn"], check=False).returncode, 0)
        self.command("finish", "review", "--turn", second["turn"])
        self.wait_state("review", "closed")
        self.command("resume", "review", "--request-id", "resume-one", prompt="continue")
        resumed = self.answered("review")
        self.assertEqual(resumed["answer"], "answer:continue")
        self.assertEqual(first["session_id"], resumed["session_id"])
        self.assertTrue(self.entries()[-1]["resume"])
        self.command("finish", "review", "--turn", resumed["turn"])
        self.assertEqual(len(self.entries()), 3)
        for entry in self.entries():
            self.assertNotIn("-p", entry["args"])
            self.assertNotIn("--dangerously-skip-permissions", entry["args"])

    def test_workspace_root_query_and_resume_keep_public_project_selector(self):
        linked_root = self.root / "linked-projects"
        linked_root.symlink_to(self.projects, target_is_directory=True)
        config = json.loads(self.config.read_text())
        config["workspace"]["vm_root"] = str(linked_root)
        self.config.write_text(json.dumps(config))
        self.command("start", "general", "--project", ".", prompt="general question")
        first = self.answered("general")
        retried = self.command("start", "general", "--project", ".", prompt="general question")
        self.assertEqual(retried["session_id"], first["session_id"])
        self.assertEqual(first["project"], ".")
        self.assertEqual(len(self.entries()), 1)
        self.command("finish", "general", "--turn", first["turn"])
        self.wait_state("general", "closed")
        self.command("resume", "general", "--request-id", "root-resume", prompt="continue")
        resumed = self.answered("general")
        self.assertEqual(resumed["project"], ".")
        self.assertEqual(resumed["session_id"], first["session_id"])
        for entry in self.entries():
            args = entry["args"]
            self.assertEqual(args[args.index("--path") + 1], str(self.projects.resolve()))
        self.command("finish", "general", "--turn", resumed["turn"])

    def test_retried_submission_does_not_send_prompt_twice(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        self.answered("review")
        self.command("start", "review", "--project", "team/repo", prompt="first")
        self.command("reply", "review", "--request-id", "reply-one", prompt="next")
        second = self.answered("review")
        self.command("reply", "review", "--request-id", "reply-one", prompt="next")
        self.assertEqual(self.command("status", "review")["turn"], second["turn"])
        self.assertEqual(len(self.entries()), 2)
        changed = self.command("reply", "review", "--request-id", "reply-one", prompt="different", check=False)
        self.assertNotEqual(changed.returncode, 0)

    def test_finish_preserves_foreign_window_and_extra_pane(self):
        self.tmux("new-session", "-d", "-s", "tests", "-n", "foreign", "sleep 60")
        foreign = self.tmux("display-message", "-p", "-t", "tests:foreign", "#{pane_id}").stdout.strip()
        self.command("start", "review", "--project", "team/repo", prompt="first")
        state = self.answered("review")
        self.assertEqual(self.tmux("display-message", "-p", "-t", "tests", "#{window_name}").stdout.strip(), "foreign")
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.splitlines()
        owned = next(pane for pane in panes if pane != foreign)
        extra = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", owned, "sleep 60").stdout.strip()
        self.command("finish", "review", "--turn", state["turn"])
        remaining = self.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.splitlines()
        self.assertIn(foreign, remaining)
        self.assertIn(extra, remaining)
        self.assertNotIn(owned, remaining)

    def test_finish_preserves_replacement_process_in_same_pane(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        state = self.answered("review")
        pane = state["pane_id"]
        self.tmux("respawn-pane", "-k", "-t", pane, "sleep 60")
        self.command("finish", "review", "--turn", state["turn"], check=False)
        remaining = self.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_dead}").stdout.splitlines()
        self.assertIn(pane + ":0", remaining)

    def test_live_failure_can_be_followed_by_another_turn(self):
        self.command("start", "review", "--project", "team/repo", prompt="FAIL")
        failed = self.wait_state("review", "failed")
        self.assertEqual(failed["error"], "rate_limit")
        self.assertEqual(failed["answer"], "answer:FAIL")
        self.command("reply", "review", "--request-id", "recover", prompt="retry")
        recovered = self.answered("review")
        self.assertEqual(recovered["answer"], "answer:retry")
        self.assertNotEqual(failed["turn"], recovered["turn"])
        self.command("finish", "review", "--turn", recovered["turn"])
        self.wait_state("review", "closed")

    def test_accepted_failure_can_be_closed(self):
        self.command("start", "review", "--project", "team/repo", prompt="FAIL")
        failed = self.wait_state("review", "failed")
        self.command("finish", "review", "--turn", failed["turn"])
        self.wait_state("review", "closed")

    def test_new_stop_receipt_invalidates_previous_finish_token(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        first = self.answered("review")
        self.deliver_stop(first["session_id"], "later answer")
        second = self.answered("review")
        self.assertEqual(second["answer"], "later answer")
        self.assertNotEqual(first["turn"], second["turn"])
        stale = self.command("finish", "review", "--turn", first["turn"], check=False)
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(self.command("status", "review")["status"], "answered")
        self.command("finish", "review", "--turn", second["turn"])
        self.wait_state("review", "closed")

    def test_stop_result_can_be_accepted_only_after_runtime_becomes_idle(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        first = self.answered("review")
        registry = [{"sessionId": first["session_id"], "kind": "interactive", "status": "busy"}]
        self.registry.write_text(json.dumps(registry))
        self.deliver_stop(first["session_id"], "answer received while busy")
        settling = self.command("status", "review")
        self.assertEqual(settling["status"], "settling")
        self.assertEqual(settling["answer"], "answer received while busy")
        self.assertNotEqual(self.command("finish", "review", "--turn", settling["turn"], check=False).returncode, 0)
        registry[0]["status"] = "idle"
        self.registry.write_text(json.dumps(registry))
        ready = self.answered("review")
        self.assertEqual(ready["turn"], settling["turn"])
        self.command("finish", "review", "--turn", ready["turn"])
        self.wait_state("review", "closed")

    def test_writable_client_must_return_control_before_reply(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        first = self.answered("review")
        client, terminal = pty.fork()
        if client == 0:
            env = dict(self.env, TERM="xterm-256color")
            os.execvpe("tmux", ["tmux", "-L", "stealthbox", "attach-session", "-t", "tests"], env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                clients = self.tmux("list-clients", "-F", "#{pane_id}").stdout.splitlines()
                if first["pane_id"] in clients:
                    break
                time.sleep(0.05)
            self.assertIn(first["pane_id"], clients, "test client did not attach")
            blocked = self.command("reply", "review", "--request-id", "reply-one", prompt="next", check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual(self.command("status", "review")["turn"], first["turn"])
            self.assertEqual(len(self.entries()), 1)
        finally:
            os.kill(client, signal.SIGTERM)
            os.waitpid(client, 0)
            os.close(terminal)
        self.command("reply", "review", "--request-id", "reply-one", prompt="next")
        self.assertEqual(self.answered("review")["answer"], "answer:next")

    def test_missing_bracket_paste_capability_blocks_input(self):
        self.command("start", "review", "--project", "team/repo", prompt="first")
        first = self.answered("review")
        real_tmux = shutil.which("tmux")
        shim = self.home / ".local/bin/tmux"
        shim.write_text("#!" + sys.executable + "\n"
                        "import os,sys\n"
                        "args=sys.argv[1:]\n"
                        "if 'display-message' in args and any('bracket_paste_flag' in a for a in args):\n"
                        "    print('\\t0\\t0'); sys.exit(0)\n"
                        "os.execv(" + repr(real_tmux) + ", [" + repr(real_tmux) + ", *args])\n")
        shim.chmod(0o700)
        blocked = self.command("reply", "review", "--request-id", "unsupported", prompt="next", check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("bracket_paste_flag", blocked.stderr)
        self.assertEqual(self.command("status", "review")["turn"], first["turn"])
        self.assertEqual(len(self.entries()), 1)

    def test_rejects_project_outside_configured_workspace(self):
        result = self.command("start", "review", "--project", "../outside", prompt="first", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
