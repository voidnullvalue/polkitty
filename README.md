# polkitty

Licensed under the MIT License. See [LICENSE](LICENSE).

`polkitty` runs a command on a trusted SSH host and asks for passwords in a desktop dialog. The password is never passed as a command argument, environment variable, chat message, or file. SSH invokes the graphical askpass helper for login. In `sudo` mode, the helper sends the remote sudo password through an anonymous pipe to SSH's standard input.

## Use

```bash
python3 polkitty.py ssh user@example.com -- id
python3 polkitty.py sudo user@example.com -- systemctl status sshd
```

The remote host must already be in `~/.ssh/known_hosts`. The wrapper refuses unknown or changed host keys. Run a normal, interactive `ssh user@example.com` first if you need to inspect and accept a host key.

The default desktop prompt uses `/usr/libexec/gcr4-ssh-askpass` on this machine. To use another graphical helper, set `POLKITTY_ASKPASS` to its absolute executable path. A helper must accept the prompt as its first argument and write the answer followed by a newline to standard output. A desktop session with `DISPLAY` or `WAYLAND_DISPLAY` is required.

`sudo` mode uses `sudo -S` without a remote terminal. The remote host must allow sudo without a TTY (`requiretty` must be off). The remote command receives end of input after the password. Separate dialogs may appear if SSH login also needs a password. For routine access, SSH keys and narrowly scoped passwordless sudo are preferable where policy permits.

The secret exists briefly in the askpass process, SSH process, remote SSH server, and kernel pipe. The wrapper does not store it. As with any remote command, its output is visible to Codex; do not run commands that print secrets. The host and command still appear in process arguments and tool history.
