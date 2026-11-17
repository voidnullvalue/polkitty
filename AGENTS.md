# Agent guide: polkitty

Use `polkitty.py` for authorized SSH access. Never ask for or transmit passwords in chat, command arguments, environment variables, or files. The local GTK askpass dialog owns authentication.

## Default: one sudo authentication per job

The user explicitly prefers **persistent job-scoped sudo sessions**. Do **not** ask whether to use the session for each job.

1. Identify the authorized remote `user@host` and task. Assign a new unique job ID for that job; do not reuse IDs between unrelated tasks.
2. For **privileged** commands, call `run --job JOB host -- command ...` with the same job and host each time. The first call authenticates and opens one root shell; subsequent independent invocations reuse it without new authentication.
3. Use `ssh host -- command ...` when the command doesn't require privilege.
4. Review each command and inspect its output and actual exit status. The persistent shell remains available, but each command receives closed stdin and is isolated by a child `sh`.
5. **Always** call `close --job JOB host` when the job ends, succeeds, fails, or is abandoned. Idle timeout is a fallback only.

```bash
python3 ./polkitty.py run --job repair-speaker-20261117 --idle-timeout 1800 void@192.168.88.236 -- id
python3 ./polkitty.py run --job repair-speaker-20261117 void@192.168.88.236 -- systemctl status sshd
python3 ./polkitty.py close --job repair-speaker-20261117 void@192.168.88.236
```

SSH and remote sudo are separate authentication layers, so the initial call may prompt once for SSH and once for sudo. Do not restart closed/expired sessions silently if reauthentication is unexpected. The default idle timeout is 1800 seconds *between completed commands*, not during a running command. Remote commands do not get a TTY or stdin. `run` preserves each command's exit status but does not preserve child-process shell state like `cd`.

## Security

- Require a previously verified SSH host key. Never disable `StrictHostKeyChecking` or bypass changed/unknown key warnings.
- Never capture, log, read, store, or print the askpass secret; do not use `sshpass`, password files, or environment variables containing credentials.
- Never change sudoers or turn on blanket passwordless sudo.
- The local job socket is user-owned and intended solely for the authorized job. Never pass its path to another user or keep it open after completion.
- Don't run commands that expose secrets in remote stdout or stderr.

Legacy `sudo` is an isolated one-off command. Legacy `session` is a raw stdin shell; it cannot be reused by separate CLI invocations. Prefer `run` for multi-command agent jobs. Read README.md for limitations.
