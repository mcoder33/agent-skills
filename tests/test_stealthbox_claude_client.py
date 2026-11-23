import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1] / "skills/orchestrate/scripts"


class SSHClientTest(unittest.TestCase):
    def test_prompt_and_arguments_reach_remote_helper_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = root / "stealthbox_claude.py"
            shutil.copy(SKILL / client.name, client)
            (root / "claude_task.py").write_text(
                "import sys,json\nprint(json.dumps([sys.argv[1:], sys.stdin.read()]))\n"
            )
            profile = root / "profile.json"
            profile.write_text(json.dumps({
                "workspace": {"host": "example-vm", "remote_dir": "/some dir/.stealthbox"}
            }))
            ssh = root / "ssh"
            ssh.write_text(
                "#!/bin/sh\nwhile [ \"$1\" != example-vm ]; do shift || exit 1; done\n"
                "shift\nexec /bin/sh -c \"$1\"\n"
            )
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       HOME=tmp, XDG_DATA_HOME=str(root / "data"))
            prompt = "Кавычки ' и \"\n$(echo should-stay-text)\n"
            result = subprocess.run(
                [sys.executable, str(client), "--profile", str(profile), "start", "test",
                 "--project", "folder with space"],
                input=prompt, text=True, capture_output=True, env=env, check=True)
            args, received = json.loads(result.stdout)
            self.assertEqual(args, ["--config", "/some dir/.stealthbox/config.json",
                                    "start", "test", "--project", "folder with space"])
            self.assertEqual(received, prompt)


if __name__ == "__main__":
    unittest.main()
