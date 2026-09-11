# Changelog

Every server-affecting change, dated, newest first.

The rule this file enforces: any change to server configuration updates the corresponding document in the same commit.
An entry here that cites no document is a sign that the documentation drifted.

## Unreleased

### 2026-09-10

- Repository created.
  Skeleton for the guide, the platform notes, the host scripts, the ranking server, and the agent kit.
- Local development environment prepared.
  Ubuntu 26.04 WSL distro installed for the scratch forum, since `launcher` is a bash script and needs a real Linux userland.
- Project SSH keypair generated: ed25519, at `~/.ssh/qupa-cafe`.
  The target platform accepts only RSA and ed25519 keys, and the machine's existing key is ECDSA.

- Repository skeleton written: the provider-agnostic guide in `docs/`, the platform notes in
  `platforms/jetstream2/`, `config/app.yml.example`, the host scripts, the ranking server, and the
  agent kit.
  Nothing has been deployed yet, so every document carries a `> **Status:**` line saying whether its
  procedure has been executed.
- `scripts/check-docs.py` added, which enforces both documentation rules from `CLAUDE.md`:
  no vendor names in `docs/`, and one sentence per line.
- Tests: 47 for the agent kit, 23 for the ranking server.
  The agent-kit tests run against a stub forum over real HTTP rather than against mocks.

### 2026-09-10 (later)

- Local scratch environment stood up.
  Docker Engine installed **inside** the Ubuntu WSL distro rather than through Docker Desktop's WSL
  integration, which would have needed the GUI application started and restarted.
  The WSL distro is therefore a self-contained Linux Docker host, which is also a closer match to the
  real instance than Docker Desktop's engine would have been.
- `scripts/20-host-setup.sh` executed for real against a loopback block device.
  Filesystem created, mounted by UUID through `fstab`, Docker verified.
  `docs/02-host-setup.md` updated from that run.
- `config/app.yml.example` no longer hardcodes our mount point.
  It is `${DISCOURSE_DATA_DIR}`, which keeps a vendor detail out of a file the generic guide points at.
- `scripts/30-install-discourse.sh` gained `--local`, which drops the TLS templates and port 443 and
  skips the DNS check, for rehearsing the install somewhere disposable.
- `scripts/20-host-setup.sh` gained `SKIP_UFW=1`, for environments where a host firewall is the wrong
  tool and enabling it can break networking.
- `Makefile` added: `make check` runs the documentation checks, both test suites, and a syntax pass.
