#!/usr/bin/env python3
"""Run SSH commands with graphical password prompts and no credential files."""

import argparse
import os
import resource
import select
import shlex
import subprocess
import sys
import threading
import time


ASKPASS_CANDIDATES = (
    "/usr/lib/ssh/ssh-askpass",
    "/usr/bin/ssh-askpass",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtk-askpass.py"),
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


def ssh_command(ssh, host, command):
    return [
        ssh, "-T", "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
        "--", host, command,
    ]


def sudo_preflight(ssh, host, env):
    """Check connectivity and whether sudo can validate without a password."""
    result = subprocess.run(
        ssh_command(ssh, host, "sudo -n -v"), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
    )
    if result.returncode == 255:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValueError(f"SSH connection failed: {detail or 'exit status 255'}")
    return result.returncode == 0


def sudo_password_pipe(helper, host, env):
    """Run askpass without reading its output in Python."""
    prompt = f"Remote sudo password for {host}"
    askpass = subprocess.Popen(
        [helper, prompt], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env,
    )
    if askpass.wait() != 0:
        askpass.stdout.close()
        raise ValueError("Sudo password dialog was cancelled or failed")
    return askpass.stdout


def relay_session(ssh_process, idle_timeout):
    """Forward command input to a single remote shell until EOF or timeout."""
    last_input = time.monotonic()
    try:
        while ssh_process.poll() is None:
            remaining = idle_timeout - (time.monotonic() - last_input)
            if remaining <= 0:
                ssh_process.terminate()
                try:
                    ssh_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ssh_process.kill()
                    ssh_process.wait()
                print("polkitty: sudo session idle timeout", file=sys.stderr)
                return 124
            readable, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.5))
            if not readable:
                continue
            data = os.read(sys.stdin.fileno(), 8192)
            if not data:
                ssh_process.stdin.close()
                return ssh_process.wait()
            last_input = time.monotonic()
            view = memoryview(data)
            while view:
                try:
                    sent = os.write(ssh_process.stdin.fileno(), view)
                except BrokenPipeError:
                    return ssh_process.wait()
                view = view[sent:]
        return ssh_process.returncode
    finally:
        if ssh_process.stdin and not ssh_process.stdin.closed:
            ssh_process.stdin.close()


def wait_for_session_ready(ssh_process, marker, timeout=120):
    """Do not forward commands until sudo has authenticated and started sh."""
    deadline = time.monotonic() + timeout
    line = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Timed out waiting for sudo session authentication")
        readable, _, _ = select.select([ssh_process.stdout], [], [], min(remaining, 0.5))
        if not readable:
            if ssh_process.poll() is not None:
                raise ValueError(f"Sudo session failed before opening (exit {ssh_process.returncode})")
            continue
        byte = os.read(ssh_process.stdout.fileno(), 1)
        if not byte:
            raise ValueError("Sudo session failed before opening; check sudo authentication and policy")
        line.extend(byte)
        if byte == b"\n":
            if line == marker:
                return
            os.write(sys.stdout.fileno(), line)
            line.clear()
        elif len(line) > 4096:
            os.write(sys.stdout.fileno(), line)
            line.clear()


def pump_session_output(ssh_process):
    while True:
        data = os.read(ssh_process.stdout.fileno(), 8192)
        if not data:
            return
        os.write(sys.stdout.fileno(), data)


def report_exit(mode, code):
    if code != 0:
        print(f"polkitty: remote {mode} exited with status {code}", file=sys.stderr)
    return code


def run_session(cmd, helper, host, env, password_required, idle_timeout, marker):
    password_pipe = sudo_password_pipe(helper, host, env) if password_required else None
    try:
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env) as ssh_process:
            if password_pipe is not None:
                # Linux splice transfers the secret between pipes in the kernel.
                # Python never reads or stores its bytes.
                while True:
                    try:
                        moved = os.splice(password_pipe.fileno(), ssh_process.stdin.fileno(), 4096)
                    except BrokenPipeError:
                        return ssh_process.wait()
                    if moved == 0:
                        break
            try:
                wait_for_session_ready(ssh_process, marker)
            except ValueError:
                ssh_process.terminate()
                raise
            output_thread = threading.Thread(target=pump_session_output, args=(ssh_process,), daemon=True)
            output_thread.start()
            print(f"polkitty: sudo session open on {host}; idle timeout {idle_timeout:g}s", file=sys.stderr)
            result = relay_session(ssh_process, idle_timeout)
            output_thread.join(timeout=2)
            return result
    finally:
        if password_pipe is not None:
            password_pipe.close()


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

    if args.mode == "ssh":
        cmd = ssh_command(ssh, args.host, remote_command(args.command, False))
        return report_exit("SSH command", subprocess.call(cmd, stdin=subprocess.DEVNULL, env=env))

    if args.mode == "session":
        if args.command:
            raise ValueError("Session mode takes commands from standard input, not arguments")
        if args.idle_timeout <= 0:
            raise ValueError("Idle timeout must be positive")
        marker = f"POLKITTY_READY_{os.urandom(8).hex()}\n".encode()
        shell = f"printf '%s\\n' {shlex.quote(marker.decode().strip())}; exec sh -s"
        command = "sudo -S -p '' -- sh -c " + shlex.quote(shell)
    else:
        command = remote_command(args.command, True)

    passwordless = sudo_preflight(ssh, args.host, env)
    if passwordless:
        command = command.replace("sudo -S -p ''", "sudo -n", 1)
    cmd = ssh_command(ssh, args.host, command)
    if args.mode == "session":
        return report_exit("sudo session", run_session(
            cmd, helper, args.host, env, not passwordless, args.idle_timeout, marker,
        ))
    if passwordless:
        return report_exit("sudo command", subprocess.call(cmd, stdin=subprocess.DEVNULL, env=env))
    with sudo_password_pipe(helper, args.host, env) as password_pipe:
        return report_exit("sudo command", subprocess.call(cmd, stdin=password_pipe, env=env))


def main():
    parser = argparse.ArgumentParser(
        description="Run a remote command with graphical SSH and sudo password prompts."
    )
    parser.add_argument("mode", choices=("ssh", "sudo", "session"))
    parser.add_argument("host", help="SSH destination, such as user@example.com")
    parser.add_argument("--idle-timeout", type=float, default=300,
                        help="Session idle timeout in seconds (default: 300)")
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
