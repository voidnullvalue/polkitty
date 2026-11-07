#!/usr/bin/env python3
"""Run SSH commands with graphical password prompts and no credential files."""

import argparse
import os
import resource
import shlex
import subprocess
import sys


ASKPASS_CANDIDATES = (
    "/usr/libexec/gcr4-ssh-askpass",
    "/usr/lib/ssh/ssh-askpass",
    "/usr/bin/ssh-askpass",
)


def askpass_path():
    configured = os.environ.get("POLKITTY_ASKPASS")
    if configured:
        if not os.path.isfile(configured) or not os.access(configured, os.X_OK):
            raise ValueError("POLKITTY_ASKPASS must name an executable file")
        return configured
    for candidate in ASKPASS_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError("No graphical askpass helper found; set POLKITTY_ASKPASS")


def remote_command(words, sudo):
    if not words:
        raise ValueError("A remote command is required after --")
    if sudo:
        return "sudo -S -p '' -- " + shlex.join(words)
    return shlex.join(words)


def run(args):
    # Inherited by SSH and the prompt helper; avoid credential-bearing core dumps.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if args.host.startswith("-") or not args.host or any(c.isspace() for c in args.host):
        raise ValueError("Invalid SSH destination")
    helper = askpass_path()
    ssh = os.environ.get("POLKITTY_SSH_BIN", "/usr/bin/ssh")
    if not os.path.isfile(ssh) or not os.access(ssh, os.X_OK):
        raise ValueError("SSH executable is unavailable")

    env = os.environ.copy()
    env["SSH_ASKPASS"] = helper
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.pop("SSH_ASKPASS_PROMPT", None)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        raise ValueError("A graphical session is required for the password prompt")

    cmd = [
        ssh, "-T", "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
        "--", args.host, remote_command(args.command, args.mode == "sudo"),
    ]

    if args.mode == "ssh":
        return subprocess.call(cmd, stdin=subprocess.DEVNULL, env=env)

    # The helper writes into a kernel pipe. Python never reads the password.
    prompt = f"Remote sudo password for {args.host}"
    with subprocess.Popen(
        [helper, prompt], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env,
    ) as askpass:
        if askpass.wait() != 0:
            raise ValueError("Password prompt was cancelled or failed")
        try:
            return subprocess.call(cmd, stdin=askpass.stdout, env=env)
        finally:
            askpass.stdout.close()


def main():
    parser = argparse.ArgumentParser(
        description="Run a remote command with graphical SSH and sudo password prompts."
    )
    parser.add_argument("mode", choices=("ssh", "sudo"))
    parser.add_argument("host", help="SSH destination, such as user@example.com")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command.pop(0)
    try:
        return run(args)
    except (OSError, ValueError) as error:
        print(f"polkitty: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
