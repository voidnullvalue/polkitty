"""Regression tests for persistent sudo jobs without contacting a real SSH host."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).with_name("polkitty.py")


class JobSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        askpass = root / "askpass"
        ssh = root / "ssh"
        self.askpass_log = root / "prompts"
        self.ssh_log = root / "connections"
        askpass.write_text(
            "#!/bin/sh\nprintf 'asked\\n' >> \"$FAKE_ASKPASS_LOG\"\n"
            "printf '%s\\n' 'synthetic-password'\n"
        )
        ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, re, shlex, sys, time\n"
            "assert 'StrictHostKeyChecking=yes' in sys.argv\n"
            "with open(os.environ['FAKE_SSH_LOG'],'a') as f: f.write('connected\\n')\n"
            "cmd=sys.argv[-1]\n"
            "n=re.search(r'POLKITTY_CONNECT_([0-9a-f]+)',cmd)\n"
            "assert n is not None, cmd\n"
            "nonce=n.group(1)\n"
            "print('POLKITTY_CONNECT_'+nonce,flush=True)\n"
            "if os.getenv('FAKE_NOPASSWD'):\n"
            "    print('POLKITTY_NOPASS_'+nonce,flush=True)\n"
            "else:\n"
            "    print('POLKITTY_PASS_'+nonce,flush=True)\n"
            "    if sys.stdin.readline()!='synthetic-password\\n': sys.exit(1)\n"
            "if os.getenv('FAKE_DENIED'): sys.exit(1)\n"
            "print('POLKITTY_READY_'+nonce,flush=True)\n"
            "for line in sys.stdin:\n"
            "    if line.strip()=='exit': break\n"
            "    if not line.startswith('sh -c '): continue\n"
            "    rc_line=sys.stdin.readline()\n"
            "    marker_line=sys.stdin.readline()\n"
            "    assert rc_line == 'polkitty_rc=$?\\n'\n"
            "    m=re.search(r'POLKITTY_EXIT_[0-9a-f]+',marker_line)\n"
            "    assert m\n"
            "    remote=shlex.split(line)[2]\n"
            "    if 'slow-operation' in remote: time.sleep(0.4)\n"
            "    status=7 if 'fail-operation' in remote else 0\n"
            "    print('remote:'+remote,flush=True)\n"
            "    print('\\x1e'+m.group()+':'+str(status),flush=True)\n"
        )
        askpass.chmod(0o700)
        ssh.chmod(0o700)
        self.env = dict(os.environ, POLKITTY_ASKPASS=str(askpass),
                        POLKITTY_SSH_BIN=str(ssh), FAKE_ASKPASS_LOG=str(self.askpass_log),
                        FAKE_SSH_LOG=str(self.ssh_log), XDG_RUNTIME_DIR=str(root),
                        DISPLAY=":test")
        self.job = "test-session"
        self.addCleanup(self.close)

    def call(self, *arguments, timeout=15, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments],
                              env=env or self.env, capture_output=True, text=True, timeout=timeout)

    def close(self):
        self.call("close", "--job", self.job, "user@example.com")

    def run_command(self, command, env=None, timeout=15, idle=None):
        args = ["run", "--job", self.job]
        if idle is not None:
            args.extend(["--idle-timeout", str(idle)])
        return self.call(*args, "user@example.com", "--", command, env=env, timeout=timeout)

    def test_cross_invocation_reuses_one_authentication_and_connection(self):
        first = self.run_command("id")
        second = self.run_command("whoami")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("remote:id", first.stdout)
        self.assertIn("remote:whoami", second.stdout)
        self.assertEqual(self.ssh_log.read_text(), "connected\n")
        self.assertEqual(self.askpass_log.read_text(), "asked\n")
        self.assertNotIn("synthetic-password", first.stdout+second.stdout+first.stderr+second.stderr)

    def test_preserves_remote_status_and_session(self):
        failed = self.run_command("fail-operation")
        good = self.run_command("id")
        self.assertEqual(failed.returncode, 7, failed.stderr)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(self.askpass_log.read_text(), "asked\n")

    def test_long_command_not_terminated_by_idle_timer(self):
        slow = self.run_command("slow-operation", idle=0.15)
        self.assertEqual(slow.returncode, 0, slow.stderr)
        time.sleep(0.3)
        status = self.call("status", "--job", self.job, "user@example.com")
        self.assertEqual(status.returncode, 1)

    def test_close_discards_credentials_and_session(self):
        self.assertEqual(self.run_command("id").returncode, 0)
        self.close()
        self.assertEqual(self.run_command("id").returncode, 0)
        self.assertEqual(self.askpass_log.read_text(), "asked\nasked\n")

    def test_passwordless_sudo_never_calls_askpass(self):
        env = dict(self.env, FAKE_NOPASSWD="1")
        response = self.run_command("id", env=env)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertFalse(self.askpass_log.exists())

    def test_failed_sudo_does_not_run_command(self):
        env = dict(self.env, FAKE_DENIED="1")
        response = self.run_command("id", env=env)
        self.assertEqual(response.returncode, 2, response.stderr)
        self.assertNotIn("remote:id", response.stdout)

    def test_job_id_required(self):
        response = self.call("run", "user@example.com", "--", "id")
        self.assertEqual(response.returncode, 2)
        self.assertIn("--job", response.stderr)


if __name__ == "__main__":
    unittest.main()
