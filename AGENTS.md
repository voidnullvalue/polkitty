# Agent guide: polkitty

Use `polkitty.py` when an authorized task needs an SSH login or a command with `sudo` on a remote host. Run it from this directory. The user enters passwords in a desktop dialog; never ask for a password in chat or put one in a shell command, environment variable, file, or tool input.

## Before running a command

1. Identify the exact destination (`user@host`) and remote command from the user's request. If the destination or requested action is unclear, clarify it before connecting.
2. Use a host whose SSH key is already trusted in `~/.ssh/known_hosts`. The wrapper rejects unknown and changed host keys. If trust must be established, have the user verify the host fingerprint through a trusted channel before accepting it; do not bypass host key checking.
3. Check that this process has a graphical session (`DISPLAY` or `WAYLAND_DISPLAY`) and that the user can respond to its desktop dialog. The default askpass helper is `/usr/libexec/gcr4-ssh-askpass` on this machine.

## Invoke

For a normal remote command:

```bash
python3 ./polkitty.py ssh user@example.com -- id
```

For a command needing remote `sudo`:

```bash
python3 ./polkitty.py sudo user@example.com -- systemctl status sshd
```

Before a task needing several privileged commands, ask the user whether to keep one sudo session open for the task or use separate authentication for each command. Explain that a session keeps a privileged shell available until it exits or times out; use it only for the agreed host and task. If the user chooses separate authentication, use `sudo` mode for each command. Do not start a session without that choice.

For an agreed session, start the wrapper in an interactive local process and send reviewed commands to its standard input:

```bash
python3 ./polkitty.py session --idle-timeout 300 user@example.com
```

Close the session with `exit` or end of input when the task is done. The idle timeout counts time since the last command input, including time spent running a long command. Session commands run in one remote `sh` under sudo, with no remote TTY. Avoid commands that read standard input; they can consume later shell commands. Report each command's status explicitly, for example by sending `printf 'exit=%s\n' "$?"` immediately after it. If a session ends unexpectedly, ask before opening another privileged session.

Pass the remote program and each argument separately after `--`. The wrapper quotes them for the remote shell. Avoid wrapping the command in `sh -c` unless shell behavior is specifically needed and its text has been reviewed. A successful exit code is `0`; otherwise inspect the error or remote command output without exposing secrets.

SSH login and remote `sudo` may each produce a separate desktop prompt. A noninteractive sudo validation runs first; a sudo dialog appears only if that validation fails. The wrapper uses `ssh -T` and `sudo -S`, so the remote host must allow sudo without a TTY. In single-command `sudo` mode, the remote command gets end of input after the password; use it for commands that do not need interactive input.

## Secret handling

- Do not read, print, capture, log, or inspect the askpass output or its pipe. Do not replace the helper with a command that records input.
- Do not use `sshpass`, `echo PASSWORD | sudo`, `sudo -A` with a password-bearing environment variable, or shell history to pass a secret.
- Treat remote command output as visible to the agent. Do not run commands that print passwords, tokens, private keys, or other secrets.
- Never turn off `StrictHostKeyChecking` to make a connection succeed. Report an unknown or changed host key and let the user verify it.

For setup details and the limits of this mechanism, see [README.md](README.md).
