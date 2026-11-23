import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLIENT = Path(__file__).resolve().parents[1] / "skills/kimi/scripts/ask_kimi.py"
FAKE_KIMI = r'''#!/usr/bin/env python3
import json, os, sys
assert sys.argv[1:] == ['acp']
def send(**fields):
    print(json.dumps(dict(jsonrpc='2.0', **fields)), flush=True)
def update(session, kind, text):
    send(method='session/update', params={'sessionId': session, 'update': {
        'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
def selector(key, values):
    return {'id':key,'type':'select','currentValue':values[0],
            'options':[{'value':v,'name':v} for v in values]}
options = [selector('model',['kimi-code/k3','kimi-code/k3.1-pro']),selector('thinking',['high'])]
session = 'synthetic-session'
for line in sys.stdin:
    req = json.loads(line)
    with open(os.environ['RPC_LOG'], 'a') as log:
        log.write(line)
    method = req.get('method')
    if not method:
        continue
    params, result = req['params'], {}
    if method == 'initialize':
        result = {'protocolVersion':1,'agentCapabilities':{'sessionCapabilities':{'resume':{}}}}
    elif method in ('session/new','session/resume'):
        result = {'sessionId':session,'configOptions':options}
    elif method == 'session/set_config_option':
        if params['configId'] == 'model':
            options[1] = selector('thinking',['low','high','max'])
        next(o for o in options if o['id'] == params['configId'])['currentValue'] = params['value']
        result = {'configOptions':options}
    elif method == 'session/prompt':
        if params['prompt'][0]['text'] == 'needs permission':
            send(id=req['id'], method='session/request_permission',
                 params={'sessionId':session,'options':[],'toolCall':{'title':'edit file'}})
            response = sys.stdin.readline()
            if response:
                with open(os.environ['RPC_LOG'],'a') as log:
                    log.write(response)
            break
        update('foreign','agent_message_chunk','unrelated')
        update(session,'agent_thought_chunk','internal')
        update(session,'agent_message_chunk','one ')
        update(session,'agent_message_chunk','two')
        result = {'stopReason':'end_turn'}
    send(id=req['id'],result=result)
'''


class KimiDirectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name).resolve()
        self.log = self.directory / "requests"
        binary = self.directory / "kimi"
        binary.write_text(FAKE_KIMI)
        binary.chmod(0o755)

    def ask(self, *args, prompt="question", check=True):
        env = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
                   HOME=str(self.directory), RPC_LOG=str(self.log))
        return subprocess.run([sys.executable, str(CLIENT), *args], env=env, input=prompt,
                              text=True, capture_output=True, check=check, timeout=10)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_defaults_and_text_chunks(self):
        result = json.loads(self.ask(prompt="literal $HOME\n'quoted'").stdout)
        self.assertEqual((result["answer"], result["session_id"]), ("one two", "synthetic-session"))
        requests = self.requests()
        self.assertEqual([r["method"] for r in requests],
                         ["initialize", "session/new", "session/prompt", "session/close"])
        self.assertEqual(requests[2]["params"]["prompt"][0]["text"], "literal $HOME\n'quoted'")

    def test_resume_and_model_specific_effort(self):
        self.ask("--cwd", str(self.directory), "--resume", "synthetic-session",
                 "--model", "k3.1-pro", "--effort", "max")
        requests = self.requests()
        self.assertEqual(requests[1]["method"], "session/resume")
        self.assertEqual(requests[1]["params"]["sessionId"], "synthetic-session")
        self.assertEqual(requests[1]["params"]["cwd"], str(self.directory))
        picks = [(r["params"]["configId"], r["params"]["value"]) for r in requests
                 if r["method"] == "session/set_config_option"]
        self.assertEqual(picks, [("model", "kimi-code/k3.1-pro"), ("thinking", "max")])

    def test_unsupported_effort_does_not_send_prompt(self):
        result = self.ask("--effort", "unknown", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("high", result.stderr)
        self.assertNotIn("session/prompt", [r["method"] for r in self.requests()])

    def test_permission_request_is_never_approved(self):
        result = self.ask(prompt="needs permission", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("edit file", result.stderr)
        replies = [r for r in self.requests() if "result" in r]
        self.assertEqual(replies[0]["result"]["outcome"], {"outcome": "cancelled"})
