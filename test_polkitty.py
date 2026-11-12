import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("polkitty.py")


class PolkittyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.askpass = root / "askpass"
        self.ssh = root / "ssh"
        self.askpass.write_text("#!/bin/sh\nprintf '%s\\n' 'synthetic-password'\n")
        self.ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, re, sys\n"
            "assert os.environ['SSH_ASKPASS'] == os.environ['POLKITTY_ASKPASS']\n"
            "assert os.environ['SSH_ASKPASS_REQUIRE'] == 'force'\n"
            "assert 'StrictHostKeyChecking=yes' in sys.argv\n"
            "command = sys.argv[-1]\n"
            "if command == 'sudo -n -v':\n"
            "    if os.environ.get('FAKE_OFFLINE'): print('host unreachable', file=sys.stderr); sys.exit(255)\n"
            "    sys.exit(0 if os.environ.get('FAKE_NOPASSWD') else 1)\n"
            "if command == 'id':\n"
            "    assert sys.stdin.read() == ''\n"
            "    print('remote-id')\n"
            "elif 'exec sh -s' in command:\n"
            "    if '-S' in command: assert sys.stdin.readline() == 'synthetic-password\\n'\n"
            "    if os.environ.get('FAKE_BAD_SUDO'): print('sudo authentication failed', file=sys.stderr); sys.exit(1)\n"
            "    marker = re.search(r'POLKITTY_READY_[0-9a-f]+', command).group()\n"
            "    print(marker, flush=True)\n"
            "    data = sys.stdin.read()\n"
            "    assert 'synthetic-password' not in data\n"
            "    print('session:' + data.replace('\\n', ','))\n"
            "else:\n"
            "    if '-S' in command: assert sys.stdin.readline() == 'synthetic-password\\n'\n"
            "    else: assert sys.stdin.read() == ''\n"
            "    assert 'synthetic-password' not in repr(sys.argv)\n"
            "    if os.environ.get('FAKE_REMOTE_FAIL'): sys.exit(7)\n"
            "    print('remote-ok')\n"
        )
        self.askpass.chmod(0o700)
        self.ssh.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(POLKITTY_ASKPASS=str(self.askpass), POLKITTY_SSH_BIN=str(self.ssh), DISPLAY=":test")

    def call(self, *args, input=None, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=input,
            env=env or self.env, text=True, capture_output=True, timeout=10,
        )

    def test_sudo_password_stays_out_of_output_and_arguments(self):
        result = self.call("sudo", "user@example.com", "--", "printf", "%s", "hello world")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "remote-ok\n")
        self.assertNotIn("synthetic-password", result.stdout + result.stderr)

    def test_passwordless_sudo_skips_dialog(self):
        self.askpass.write_text("#!/bin/sh\necho called >&2\nexit 1\n")
        env = dict(self.env, FAKE_NOPASSWD="1")
        result = self.call("sudo", "user@example.com", "--", "id", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "remote-ok\n")

    def test_unreachable_host_does_not_prompt_for_sudo(self):
        env = dict(self.env, FAKE_OFFLINE="1")
        result = self.call("sudo", "user@example.com", "--", "id", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("SSH connection failed: host unreachable", result.stderr)

    def test_cancelled_dialog(self):
        self.askpass.write_text("#!/bin/sh\nexit 1\n")
        result = self.call("sudo", "user@example.com", "--", "id")
        self.assertEqual(result.returncode, 2)
        self.assertIn("dialog was cancelled or failed", result.stderr)

    def test_remote_failure_reports_status(self):
        env = dict(self.env, FAKE_REMOTE_FAIL="1")
        result = self.call("sudo", "user@example.com", "--", "id", env=env)
        self.assertEqual(result.returncode, 7)
        self.assertIn("remote sudo command exited with status 7", result.stderr)

    def test_session_uses_one_password_for_multiple_commands(self):
        self.askpass.write_text(
            "#!/bin/sh\nprintf 'asked\\n' >> \"$FAKE_ASKPASS_LOG\"\n"
            "printf '%s\\n' 'synthetic-password'\n"
        )
        log = Path(self.directory.name) / "askpass.log"
        env = dict(self.env, FAKE_ASKPASS_LOG=str(log))
        result = self.call("session", "user@example.com", input="id\nwhoami\n", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "session:id,whoami,\n")
        self.assertIn("sudo session open", result.stderr)
        self.assertNotIn("synthetic-password", result.stdout + result.stderr)
        self.assertEqual(log.read_text(), "asked\n")

    def test_passwordless_session_skips_dialog(self):
        self.askpass.write_text("#!/bin/sh\nexit 1\n")
        env = dict(self.env, FAKE_NOPASSWD="1")
        result = self.call("session", "user@example.com", input="id\n", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "session:id,\n")

    def test_session_idle_timeout(self):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "session", "--idle-timeout", "0.2", "user@example.com"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env, text=True,
        )
        try:
            code = process.wait(timeout=10)
            self.assertEqual(code, 124)
            self.assertIn("sudo session idle timeout", process.stderr.read())
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_commands_before_sudo_authentication(self):
        env = dict(self.env, FAKE_BAD_SUDO="1")
        result = self.call("session", "user@example.com", input="id\n", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Sudo session failed before opening", result.stderr)
        self.assertNotIn("session:id", result.stdout)

    def test_ssh_mode_has_no_input_and_requires_known_host(self):
        result = self.call("ssh", "user@example.com", "--", "id")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "remote-id\n")


if __name__ == "__main__":
    unittest.main()
