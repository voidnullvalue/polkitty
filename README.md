# polkitty

Licensed under the MIT License. See [LICENSE](LICENSE).

`polkitty` runs a command on a trusted SSH host and asks for passwords in a desktop dialog. The password is never passed as a command argument, environment variable, chat message, or file. SSH invokes the graphical askpass helper for login. In `sudo` mode, the helper sends the remote sudo password through an anonymous pipe to SSH's standard input.

## Use

```bash
python3 polkitty.py ssh user@example.com -- id
python3 polkitty.py sudo user@example.com -- systemctl status sshd
python3 polkitty.py session --idle-timeout 300 user@example.com
```

`session` keeps one remote sudo `sh` running and forwards subsequent local standard input to it. Use it only after agreeing on the host, task, and session choice. Send one reviewed command at a time, check its output and exit status (for example, send `printf 'exit=%s\n' "$?"` after each command), and end with `exit` or end of input. It closes after 300 seconds without command input by default; change this with `--idle-timeout SECONDS`. The timeout also runs while a long remote command is executing. No remote TTY is allocated, and commands that consume standard input can consume later shell input.

The remote host must already have a trusted SSH host key. The wrapper refuses unknown or changed host keys. Before adding a new key, verify its fingerprint through a trusted channel.

The default desktop prompt uses `/usr/libexec/gcr4-ssh-askpass` on this machine. To use another graphical helper, set `POLKITTY_ASKPASS` to its absolute executable path. A helper must accept the prompt as its first argument and write the answer followed by a newline to standard output. A desktop session with `DISPLAY` or `WAYLAND_DISPLAY` is required.

`sudo` and `session` modes first try `sudo -n -v` on the remote host. If that succeeds, they run without a sudo password dialog. If it fails because authentication is needed, the helper asks for the password; an unreachable host fails before that dialog. Validation may still fail under command-specific sudo policy, so a dialog is possible even for a passwordless command. The preflight opens a separate SSH connection, which may cause an extra SSH login prompt. The remote host must allow sudo without a TTY (`requiretty` must be off). Single-command `sudo` mode gives the remote command end of input after the password. For routine access, SSH keys and narrowly scoped passwordless sudo are preferable where policy permits.

The secret exists briefly in the askpass process, SSH process, remote SSH server, and kernel pipe. The wrapper does not store it. As with any remote command, its output is visible to Codex; do not run commands that print secrets. The host and command still appear in process arguments and tool history.
