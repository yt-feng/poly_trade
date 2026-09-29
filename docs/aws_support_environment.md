# AWS support environment

This is a connection and file roundtrip check for cloud research support. It does
not activate trading, execute research, upload local records, or establish that a
strategy, experiment, reference price or canary has passed. The earlier research
handoff remains partial; S01 still needs its exact input and separate receipt.

The dedicated remote user is `poly-support`. Its workspace is
`/srv/poly-chatgpt-support`, owned by that user with mode `0700`. The user must be
unprivileged, cannot use passwordless sudo, and has a dedicated SSH authorized key
with port forwarding and PTY allocation disabled. The preflight requires Python
3.10 or newer and the standard library only. It does not install dependencies.

The `aws-support` GitHub environment in `yt-feng/poly_trade` holds four Secrets:

| Secret name | Purpose |
| --- | --- |
| `AWS_SUPPORT_HOST` | Existing support server hostname or IP; default SSH port |
| `AWS_SUPPORT_USER` | Dedicated user, exactly `poly-support` |
| `AWS_SUPPORT_SSH_KEY` | Dedicated, noninteractive SSH private key |
| `AWS_SUPPORT_KNOWN_HOSTS` | Independently verified server host-key pin |

The known-hosts content uses one or more ordinary, unhashed lines with the exact
host from `AWS_SUPPORT_HOST`, a supported key type and the public host key. Each
line must refer only to that host. The client does not discover or refresh pins.
Supported pin types are `ssh-ed25519`, `ecdsa-sha2-nistp256` and `ssh-rsa`.

The GitHub environment permits only `codex/aws-support-environment-20260929` and
`main`. The current workflow's job condition additionally limits execution to
the support branch. No trading credential, wallet private key, AWS access
key, account configuration, PIN or recovery file is part of this setup. This
existing-server check does not use the AWS control plane.

## Running the check

A push that changes the workflow, preflight client, its unit tests or this document
on `codex/aws-support-environment-20260929` runs the preflight. Other branches are
excluded by both the push trigger and job condition. `workflow_dispatch` is also
declared, but GitHub's manual-run UI will not appear until the workflow exists on
the default branch; this setup does not merge it into that branch.

The job checks out the code with persisted Git credentials disabled, runs the
standard-library unit tests and attempts SSH once. Its GitHub token has only
`contents: read`. The job is limited to three minutes, the SSH subprocess to
45 seconds, and its connection attempt to 15 seconds. There is no automatic retry.

SSH uses `BatchMode`, `IdentitiesOnly`, `StrictHostKeyChecking` and an explicit
`UserKnownHostsFile`. Key and pinned-host files are stored only in a temporary
directory with mode `0700`; each file has mode `0600` and is removed when the
client finishes. Existing SSH/proxy/network configuration is not changed.

The remote probe checks the effective username, real/effective UID, Python
version, support workspace ownership/mode, and denial of `sudo -n true`. Only
after those checks does it create, read and delete a random 48-byte fixture in
the support workspace. The client independently verifies the returned bytes and
SHA256 digest. It reads no production file, log or configuration and changes no
service. A timed-out process can leave a small fixture for later local cleanup;
no cleanup of unrelated files is attempted.

All remote stdout and stderr are captured privately. Logs contain only check
labels, passed/failed labels and JSON booleans. The workflow publishes no artifact.
Failed labels mean that a check was not established, including when an earlier
failure prevented it from running; raw remote output is never printed.

For a local run with the four values already supplied in the environment:

```sh
python3 -I -B tools/aws_support_preflight.py --receipt /absolute/new/receipt.json
```

The optional receipt contains only the schema, overall boolean and named boolean
checks. Its parent directory must exist and its destination must not already
exist. A successful exit means only that this support preflight passed. It does
not authorize or demonstrate production trading or completion of research.

Offline test command (no credentials or SSH connection required):

```sh
python3 -I -B -m unittest discover -s tests -p test_aws_support_preflight.py
```
