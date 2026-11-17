#!/usr/bin/env python3
"""Run SSH commands with graphical password prompts and no credential files."""

import argparse
import base64
import errno
import fcntl
import hashlib
import json
from pathlib import Path
import socket
import stat
import struct
import tempfile
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

    if args.mode == "sudo":
        process, pending = job_bootstrap(ssh, helper, args.host, env)
        try:
            return report_exit("sudo command", job_command(
                process, pending, args.command,
                lambda data: os.write(sys.stdout.fileno(), data)))
        finally:
            job_close_shell(process)

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



# A job owns a single root shell. The local session socket is accessible only to
# the current uid; no password, password hash, or authentication token is stored.
JOB_IDLE_TIMEOUT = 1800
MAX_FRAME = 65536


def job_socket(host, job):
    if not job or len(job) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in job):
        raise ValueError("--job must be 1-128 printable, non-whitespace ASCII characters")
    parent = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    directory = parent / ("polkitty-" + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    mode = directory.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Unsafe polkitty session directory")
    digest = hashlib.sha256((host + "\0" + job).encode()).hexdigest()[:32]
    return directory / ("s-" + digest + ".sock")


def job_send(sock, frame):
    sock.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")


def job_receive(sock):
    value = bytearray()
    while True:
        block = sock.recv(4096)
        if not block:
            raise ValueError("Session manager disconnected")
        value.extend(block)
        if len(value) > MAX_FRAME:
            raise ValueError("Oversized session frame")
        if b"\n" in value:
            if value[-1:] != b"\n" or value.count(b"\n") != 1:
                raise ValueError("Malformed session frame")
            return json.loads(value.decode())


def job_connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(str(path))
        sock.settimeout(None)
        return sock
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    except OSError:
        sock.close()
        raise


def job_bootstrap(ssh, helper, host, env):
    """One SSH connection for connectivity check and sudo authentication."""
    nonce = os.urandom(16).hex()
    connect, no_pass, password, ready = [
        "POLKITTY_" + label + "_" + nonce
        for label in ("CONNECT", "NOPASS", "PASS", "READY")
    ]
    inner = "printf '%s\\n' " + shlex.quote(ready) + "; exec sh -s"
    wrapper = "sh -c " + shlex.quote(inner)
    script = (
        "printf '%s\\n' " + shlex.quote(connect) + "; "
        "if sudo -n -- sh -c : </dev/null >/dev/null 2>&1; then "
        "printf '%s\\n' " + shlex.quote(no_pass) + "; exec sudo -n -- " + wrapper + "; "
        "else printf '%s\\n' " + shlex.quote(password) +
        "; exec sudo -S -p '' -- " + wrapper + "; fi"
    )
    proc = subprocess.Popen(ssh_command(ssh, host, "sh -c " + shlex.quote(script)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
    pending = bytearray()
    deadline = time.monotonic() + 120

    def line():
        while True:
            pos = pending.find(b"\n")
            if pos >= 0:
                result = bytes(pending[:pos]).rstrip(b"\r")
                del pending[:pos+1]
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Timed out waiting for SSH/sudo authentication")
            ready_fds, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            if ready_fds:
                part = os.read(proc.stdout.fileno(), 4096)
                if not part:
                    raise ValueError("SSH/sudo disconnected before privileged shell started")
                pending.extend(part)
                if len(pending) > MAX_FRAME:
                    raise ValueError("Excessive bootstrap output")
    try:
        if line() != connect.encode():
            raise ValueError("SSH failed (verify connectivity and trusted host key)")
        response = line()
        if response == password.encode():
            with sudo_password_pipe(helper, host, env) as password_pipe:
                while os.splice(password_pipe.fileno(), proc.stdin.fileno(), 4096):
                    pass
        elif response != no_pass.encode():
            raise ValueError("Unexpected sudo authentication response")
        if line() != ready.encode():
            raise ValueError("Sudo authentication failed")
        return proc, pending
    except (OSError, ValueError):
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()
        raise


def job_command(proc, pending, words, emit):
    """Execute isolated, non-interactive command and report its real exit status."""
    marker = b"\x1ePOLKITTY_EXIT_" + os.urandom(16).hex().encode() + b":"
    snippet = (
        "sh -c " + shlex.quote(shlex.join(words)) + " </dev/null 2>&1\n"
        "polkitty_rc=$?\n"
        "printf '\\036%s:%s\\n' " + shlex.quote(marker[1:-1].decode()) + ' "$polkitty_rc"\n'
    )
    proc.stdin.write(snippet.encode())
    proc.stdin.flush()
    while True:
        start = pending.find(marker)
        if start >= 0:
            end = pending.find(b"\n", start + len(marker))
            if end >= 0:
                emit(bytes(pending[:start]))
                status = bytes(pending[start + len(marker):end])
                del pending[:end+1]
                if not status.isdigit():
                    raise ValueError("Malformed remote exit status")
                return int(status)
        if start < 0 and len(pending) > len(marker):
            keep = len(marker)-1
            emit(bytes(pending[:-keep]))
            del pending[:-keep]
        part = os.read(proc.stdout.fileno(), 8192)
        if not part:
            if pending:
                emit(bytes(pending))
                pending.clear()
            raise ValueError("Privileged SSH shell terminated unexpectedly")
        pending.extend(part)


def job_close_shell(proc):
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.stdin.write(b"exit\n")
            proc.stdin.flush()
            proc.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    proc.stdin.close()
    proc.stdout.close()


def job_server(args):
    path = job_socket(args.host, args.job)
    os.umask(0o077)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(8)
    proc = None
    pending = bytearray()
    last_request = time.monotonic()
    try:
        while True:
            remaining = args.idle_timeout - (time.monotonic() - last_request)
            if remaining <= 0:
                break
            server.settimeout(remaining)
            try:
                connection, _ = server.accept()
            except socket.timeout:
                break
            with connection:
                action = None
                try:
                    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, uid, _gid = struct.unpack("3i", raw)
                    if uid != os.getuid():
                        raise ValueError("Unauthorized local client")
                    connection.settimeout(30)
                    req = job_receive(connection)
                    connection.settimeout(None)
                    if req.get("job") != args.job or req.get("host") != args.host:
                        raise ValueError("Incorrect session identity")
                    action = req.get("action")
                    if action == "status":
                        job_send(connection, {"type":"status", "active":proc is not None,
                                              "timeout":args.idle_timeout})
                    elif action == "close":
                        job_close_shell(proc)
                        proc = None
                        server.close()
                        path.unlink()
                        job_send(connection, {"type":"done", "status":0})
                        return 0
                    elif action == "run":
                        words = req.get("command")
                        if not isinstance(words, list) or not words or not all(isinstance(w,str) for w in words):
                            raise ValueError("Invalid command")
                        if proc is None:
                            helper = askpass_path()
                            ssh = os.environ.get("POLKITTY_SSH_BIN", "/usr/bin/ssh")
                            if not os.path.isfile(ssh) or not os.access(ssh, os.X_OK):
                                raise ValueError("SSH executable unavailable")
                            env = os.environ.copy()
                            env["SSH_ASKPASS"] = helper
                            env["SSH_ASKPASS_REQUIRE"] = "force"
                            env.pop("SSH_ASKPASS_PROMPT", None)
                            if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
                                raise ValueError("A graphical session is required")
                            proc, pending = job_bootstrap(ssh, helper, args.host, env)
                        def output(data):
                            if not data:
                                return
                            try:
                                job_send(connection, {"type":"output","data":base64.b64encode(data).decode()})
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                        code = job_command(proc, pending, words, output)
                        job_send(connection, {"type":"done","status":code})
                    else:
                        raise ValueError("Invalid action")
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    try:
                        job_send(connection, {"type":"error","message":str(error)})
                    except OSError:
                        pass
                    if action == "run" and (proc is None or proc.poll() is not None):
                        break
                finally:
                    # Idle clock starts AFTER command completion; never kill running jobs.
                    last_request = time.monotonic()
    finally:
        job_close_shell(proc)
        server.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return 0


def job_start(path, args):
    # Lock covers the entire startup; simultaneous clients don't double-prompt.
    fd = os.open(str(path)+".lock", os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = job_connect(path)
        if existing is not None:
            return existing
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        command = [sys.executable, os.path.abspath(__file__), "_serve", "--job", args.job,
                   "--idle-timeout", str(args.idle_timeout), args.host]
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            connection = job_connect(path)
            if connection is not None:
                return connection
            time.sleep(0.05)
        raise ValueError("Session manager failed to start")
    finally:
        os.close(fd)


def job_request(args):
    path = job_socket(args.host, args.job)
    conn = job_connect(path)
    if conn is None:
        if args.mode != "run":
            print("polkitty: no active session")
            return 1 if args.mode == "status" else 0
        conn = job_start(path, args)
    try:
        job_send(conn, {"action":args.mode,"host":args.host,"job":args.job,"command":args.command})
        # Output frames are newline-delimited, and command output is base64.
        buf = bytearray()
        while True:
            part = conn.recv(8192)
            if not part:
                raise ValueError("Session manager disconnected unexpectedly")
            buf.extend(part)
            while b"\n" in buf:
                frame, _, remainder = buf.partition(b"\n")
                buf = bytearray(remainder)
                data = json.loads(frame)
                if data["type"] == "output":
                    os.write(sys.stdout.fileno(), base64.b64decode(data["data"], validate=True))
                elif data["type"] == "error":
                    raise ValueError(data["message"])
                elif data["type"] == "status":
                    print("polkitty: session "+("authenticated" if data["active"] else "pending")+
                          f" on {args.host} (job {args.job}, idle timeout {data['timeout']:g}s)")
                    return 0
                elif data["type"] == "done":
                    return int(data["status"])
                else:
                    raise ValueError("Unknown session response")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Run a remote command with graphical SSH and sudo password prompts."
    )
    parser.add_argument("mode", choices=("ssh", "sudo", "session", "run", "close", "status", "_serve"))
    parser.add_argument("--job", help="Unique job ID for persistent privilege sessions")
    parser.add_argument("host", help="SSH destination, such as user@example.com")
    parser.add_argument("--idle-timeout", type=float, default=JOB_IDLE_TIMEOUT,
                        help="Session idle timeout in seconds (default: 300)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command.pop(0)
    try:
        if args.mode in ("run", "close", "status", "_serve"):
            job_socket(args.host, args.job)
            if args.idle_timeout <= 0 or args.idle_timeout == float("inf"):
                raise ValueError("Idle timeout must be positive and finite")
            if args.mode in ("close", "status", "_serve") and args.command:
                raise ValueError("Unexpected command")
            if args.mode == "run" and not args.command:
                raise ValueError("Remote command required after --")
            return job_server(args) if args.mode == "_serve" else job_request(args)
        return run(args)
    except (OSError, ValueError) as error:
        print(f"polkitty: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
