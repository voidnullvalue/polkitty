"""Unit tests for legacy CLI helpers, alongside test_job_sessions.py."""
import unittest
from pathlib import Path
import importlib.util

MODULE_PATH = Path(__file__).with_name("polkitty.py")
spec = importlib.util.spec_from_file_location("polkitty", MODULE_PATH)
polkitty = importlib.util.module_from_spec(spec)
spec.loader.exec_module(polkitty)


class PolkittyHelpersTest(unittest.TestCase):
    def test_strict_known_host_check(self):
        cmd = polkitty.ssh_command("/usr/bin/ssh", "user@example.com", "id")
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn("-T", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)

    def test_shell_arguments_quoted(self):
        cmd = polkitty.remote_command(["printf", "%s", "a b; whoami"], sudo=True)
        self.assertIn("sudo -S", cmd)
        self.assertIn("'a b; whoami'", cmd)

    def test_rejects_bad_job_identifier(self):
        for job in ("", "space in job", "a\nb", "x" * 129):
            with self.subTest(job=repr(job)):
                with self.assertRaises(ValueError):
                    polkitty.job_socket("user@example.com", job)

    def test_job_socket_separates_host_and_job(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            prior = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = directory
            try:
                a = polkitty.job_socket("user@host1", "task")
                b = polkitty.job_socket("user@host2", "task")
                c = polkitty.job_socket("user@host1", "other")
                self.assertNotEqual(a, b)
                self.assertNotEqual(a, c)
            finally:
                if prior is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = prior

    def test_bootstrap_contains_sudo_no_password_probe_and_fallback(self):
        # This is a single remote SSH shell script rather than two connections.
        import inspect
        source = inspect.getsource(polkitty.job_bootstrap)
        self.assertIn("sudo -n -- sh -c :", source)
        self.assertIn("sudo -S -p", source)


if __name__ == "__main__":
    unittest.main()
