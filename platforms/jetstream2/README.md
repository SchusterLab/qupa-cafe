# Jetstream2

An NSF-funded academic OpenStack cloud, reached through an ACCESS-CI allocation.

What differs from an ordinary VPS:

- **The award is in Credits.** Before any machine exists, the PI exchanges them for service units on a resource ([`01-access-allocation.md`](01-access-allocation.md)).
- **Allocation expiry destroys instances and volumes**, 30 days after expiry.
- **The only free instance state is shelved, and a shelved instance is unreachable.** So the forum host is always active.
- **Designate creates a public DNS record** when a floating IP is attached. The forum still uses its own domain, because the mail identity needs a zone you control.

| File | What it is |
|---|---|
| [`01-access-allocation.md`](01-access-allocation.md) | Credits, the exchange, costs, expiry, collaborators |
| [`02-provision.md`](02-provision.md) | Creating the host with the OpenStack CLI |
| [`03-object-storage.md`](03-object-storage.md) | The upload bucket |
| [`scripts/10-provision.sh`](scripts/10-provision.sh) | Provisioning, idempotent |
| [`openrc.sh.example`](openrc.sh.example) | Application credential template; the real one is gitignored |

`02-provision.md` ends with a host you can SSH into. Continue from there at `docs/02-host-setup.md`.

## This deployment

| | |
|---|---|
| Allocation | CIS261848, 2026-09-10 to 2027-09-09 |
| Flavor | `m3.quad`, 4 vCPU, 15 GB RAM, 4 SU/hr |
| Image | `Featured-Ubuntu24` |
| Root | 60 GB, boot from volume |
| Data volume | 100 GB, `qupa-cafe-data` |
| Instance | `qupa-cafe`, locked |
| Floating IP | 149.165.153.151 |
| Security groups | `qupa-cafe-ssh`, `qupa-cafe-web` |
| Keypair | `qupa-cafe`, ed25519 |
| Bucket | `qupa-cafe-uploads` |
