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

Pass the remote program and each argument separately after `--`. The wrapper quotes them for the remote shell. Avoid wrapping the command in `sh -c` unless shell behavior is specifically needed and its text has been reviewed. A successful exit code is `0`; otherwise inspect the error or remote command output without exposing secrets.

SSH login and remote `sudo` may each produce a separate desktop prompt. The sudo prompt appears first. The wrapper uses `ssh -T` and `sudo -S`, so the remote host must allow sudo without a TTY. The remote command gets end of input after the sudo password; use this wrapper for commands that do not need interactive input.

## Secret handling

- Do not read, print, capture, log, or inspect the askpass output or its pipe. Do not replace the helper with a command that records input.
- Do not use `sshpass`, `echo PASSWORD | sudo`, `sudo -A` with a password-bearing environment variable, or shell history to pass a secret.
- Treat remote command output as visible to the agent. Do not run commands that print passwords, tokens, private keys, or other secrets.
- Never turn off `StrictHostKeyChecking` to make a connection succeed. Report an unknown or changed host key and let the user verify it.

For setup details and the limits of this mechanism, see [README.md](README.md).
