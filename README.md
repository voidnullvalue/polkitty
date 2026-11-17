# polkitty

Licensed under the MIT License. See [LICENSE](LICENSE).

`polkitty` runs authorized commands over SSH and prompts for SSH and sudo passwords through a desktop graphical askpass dialog. It never places passwords in command arguments, environment variables, agent chats, or files.

## Usage

```bash
# Unprivileged single command
python3 polkitty.py ssh user@example.com -- id

# Preferred: persistent, job-scoped sudo shell shared across separate CLI calls
python3 polkitty.py run --job maintenance-001 --idle-timeout 1800 user@example.com -- id
python3 polkitty.py run --job maintenance-001 user@example.com -- systemctl status sshd
python3 polkitty.py status --job maintenance-001 user@example.com
python3 polkitty.py close --job maintenance-001 user@example.com

# Deliberately isolated sudo command
python3 polkitty.py sudo user@example.com -- id

# Legacy stdin-forwarding session (not reusable across calls)
python3 polkitty.py session --idle-timeout 300 user@example.com
```

## Persistent sudo sessions

`run` starts a local, detached manager for each unique host/job pair. Its Unix-domain socket lives in a private user-owned directory (mode 0700), and requests require the same local user ID. The manager opens **one SSH connection**, validates sudo policy within that connection, asks for the remote sudo password only if needed, then keeps one authenticated remote `sh` alive. Separate `run` invocations with the same job ID and host reuse it; each command runs in a child shell with `/dev/null` stdin and an individually reported exit status.

The daemon exits on `close` or after an idle timeout (default 1800 seconds) **between completed commands**. A running command is not interrupted by this timeout. Closing the job closes the privileged shell. Give independent tasks different job IDs, always close when done, and never treat a session as a general-purpose permanently privileged daemon.

**Limits:** SSH authentication and sudo authentication are distinct, so the first call may show up to two dialogs. Remote commands cannot interact with a TTY or read stdin. Each command runs in its own child shell, so `cd` and exported variables don't persist across commands. A lost local daemon, expired job, system restart, changed SSH credentials, or broken connection may require reauthentication; no persistent credentials are stored.

## Security

Strict SSH host-key checking is mandatory; unknown and changed host keys are rejected. Verify fingerprints out of band before establishing trust. The graphical askpass helper is selected from `POLKITTY_ASKPASS`, a supported system askpass, or bundled `gtk-askpass.py` (requires GTK 3/PyGObject), and needs `DISPLAY` or `WAYLAND_DISPLAY`. The sudo secret flows from the helper to the SSH process through an anonymous pipe, using Linux `splice`; Python does not read the credential bytes.

The remote host must permit non-TTY sudo (no `requiretty`). Commands and hostnames are visible in process arguments, and command output is visible to the invoking agent. Do not output secrets. The socket is local and only intended for the same authenticated Unix user; local processes running as that user can operate the privileged shell until it is closed. This is why job scoping, idle timeout, and explicit close are essential.

Single-command `sudo` and legacy `session` remain available for compatibility. Single-command `sudo` now validates and authenticates within one SSH connection; legacy `session` retains its separate preflight connection and raw forwarded stdin rather than framed commands.
