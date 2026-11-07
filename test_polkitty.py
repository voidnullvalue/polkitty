import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("polkitty.py")


class PolkittyTests(unittest.TestCase):
    def test_sudo_password_stays_out_of_output_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            askpass = root / "askpass"
            ssh = root / "ssh"
            askpass.write_text("#!/bin/sh\nprintf '%s\\n' 'synthetic-password'\n")
            ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "assert os.environ['SSH_ASKPASS'] == os.environ['POLKITTY_ASKPASS']\n"
                "assert os.environ['SSH_ASKPASS_REQUIRE'] == 'force'\n"
                "assert sys.stdin.readline() == 'synthetic-password\\n'\n"
                "assert 'synthetic-password' not in repr(sys.argv)\n"
                "assert sys.argv[-1] == \"sudo -S -p '' -- printf %s 'hello world'\"\n"
                "print('remote-ok')\n"
            )
            askpass.chmod(0o700)
            ssh.chmod(0o700)
            env = os.environ.copy()
            env.update(POLKITTY_ASKPASS=str(askpass), POLKITTY_SSH_BIN=str(ssh), DISPLAY=":test")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "sudo", "user@example.com", "--",
                 "printf", "%s", "hello world"],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "remote-ok\n")
            self.assertNotIn("synthetic-password", result.stdout + result.stderr)

    def test_ssh_mode_has_no_input_and_requires_known_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            askpass = root / "askpass"
            ssh = root / "ssh"
            askpass.write_text("#!/bin/sh\nexit 1\n")
            ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "assert sys.stdin.read() == ''\n"
                "assert 'StrictHostKeyChecking=yes' in sys.argv\n"
                "assert sys.argv[-1] == 'id'\n"
            )
            askpass.chmod(0o700)
            ssh.chmod(0o700)
            env = os.environ.copy()
            env.update(POLKITTY_ASKPASS=str(askpass), POLKITTY_SSH_BIN=str(ssh), DISPLAY=":test")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "ssh", "user@example.com", "--", "id"],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
