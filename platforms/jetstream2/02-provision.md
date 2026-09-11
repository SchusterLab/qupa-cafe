# 2. Provisioning the instance

> **Status:** Executed in full on the real allocation.
> `scripts/10-provision.sh` created the instance, its volume, its security groups and its floating IP, and a second run of it changed nothing.
> The Horizon form in 2.1 is the one part not driven by the script, so its field-by-field detail is reported rather than replayed.

Creates the host, its storage, its network access, and its hostname.
Ends at a machine you can SSH into, which is where `docs/02-host-setup.md` takes over.

`scripts/10-provision.sh` does all of this idempotently.
This document explains what it does and why, so that the script is readable rather than magic.

## 2.1 The CLI, and why not the web interface

Provisioning happens through `python-openstackclient`, driven by a script in this repository.

Two reasons.
A configuration produced by clicking is not reproducible, which is the same argument that makes `app.yml` hand-written.
And Exosphere — the friendly web interface — has a default security group that permits all inbound traffic and no documented way to edit groups, so the CLI is also the only way to get ingress right.

There are two web interfaces, and the difference matters below.
**Exosphere** is the simplified one, built for Jetstream2, and it is the one this section is declining to use.
**Horizon** is OpenStack's own dashboard, the full administrative interface, and it is where the CLI's credentials come from.
It is at <https://js2.jetstream-cloud.org>, which redirects to `/auth/login/` — a Django login route, and the quickest way to tell you have reached Horizon rather than Exosphere.

### Install it

Jetstream2 tells Windows users to run the client from WSL.
Git Bash is not a supported path for it.

```bash
sudo apt-get install -y python3-openstackclient python3-designateclient
openstack --version
```

`python3-designateclient` adds the DNS subcommands, which are not in the base client.

### Credentials

The CLI authenticates with an **application credential**: an id and secret you create to stand in for your login.

That indirection is not optional here.
The login page offers two methods, **ACCESS CILogon** and **Keystone Credentials**, and an ACCESS allocation uses the first.
A federated identity has no Keystone password, so there is nothing an openrc file could hold — hence the credential.

An application credential is also scoped and revocable on its own, which a login is not.

In Horizon, the OpenStack dashboard: **Identity** in the left-hand navigation, then **Application Credentials**, then **Create Application Credential**.

Fill it in like this:

| Field | Value |
|---|---|
| Name | `qupa-cafe` |
| Description | `OpenStack CLI for the Qupa Cafe forum host (CIS261848)` |
| Secret | blank |
| Expiration Date | blank |
| Expiration Time | blank |
| Roles | `member` and `reader` |
| Access Rules | blank |
| Unrestricted | unchecked |

Four of those are decisions rather than form-filling.

**Leave both expiration fields blank**, which means the credential does not expire.

The alternative is a date, and a date is what turns an ordinary authentication failure into a puzzle: the credential stops working one morning and the error says nothing about expiry.
Revocation covers the same ground on demand — one click in Horizon, and it takes effect immediately — so the expiry date buys a deadline rather than a control.

Revoke it when the allocation ends.
`01-access-allocation.md` has the date in a calendar already, for a different reason.

**Take `member` and `reader`, not everything offered.**
The form offers four: `reader`, `creator`, `member`, and `load-balancer_member`.
`scripts/10-provision.sh` uses compute, block storage, networking and DNS, all of which `member` covers, and `reader` is worth keeping beside it because some services want an explicit read role under newer RBAC.
`creator` is the key manager's role and `load-balancer_member` is the load balancer's; this deployment creates neither.

Selecting the extra two is not an escalation — a credential can only carry roles you already hold — but it widens what the secret can do if it leaks, and buys nothing.

**Leave the secret blank** and let Horizon generate one.
Choosing your own changes nothing — it arrives in the same downloaded file — and is one more secret to keep.

**Leave Unrestricted unchecked.**
Horizon labels it dangerous because it lets the credential mint further credentials and trusts, and provisioning needs neither.

**Leave Access Rules blank.**
Restricting the credential to named API calls is the appealing option, and this deployment does not take it: `scripts/10-provision.sh` spans compute, block storage, networking and DNS, so the list would be long and easy to get subtly wrong.
Role scope and one-click revocation are the security story instead.
If you want the restricted version, upstream documents the syntax — treat it as something to work out and test, not to copy.

On success Horizon shows the id and secret once, with a **Download openrc file** button.

The file it gives you is a shell script that exports the variables in [`openrc.sh.example`](openrc.sh.example), which is all `source` has to do with it.
Keep the name it arrives with and save it alongside that example.
Horizon derives the name from the credential, so with the table above it is `app-cred-qupa-cafe-openrc.sh`.
`.gitignore` matches `*openrc*.sh` anywhere in the tree for this reason: the one file in the repository that must never be committed should not depend on anybody remembering to rename it.
Jetstream2 documents the `openrc.sh` path only, not `clouds.yaml`.

Three traps:

> **If you do set an expiry, the two fields interact.**
> A *time* with no date means today, because Horizon fills the date in from the clock — so the credential expires the same day, possibly at a moment already past.
> A *date* with no time means 00:00 on that date, so the day you enter is the first day the credential does not work.
> Neither is stated on the form; both are in Horizon's `identity/application_credentials` form, in the branch on `expiration_date`.

> **The secret is shown once.**
> Download the file on the success screen; there is no way back to it, only a new credential.

> **Sourcing an openrc over a previous one fails**, with "Application credentials cannot request a scope".
> Use a fresh terminal.
> The error does not say that.

> **There is a second, wrong "download openrc" in Horizon** [unverified], under **API Access**, which produces a password-based file for a password you do not have.
> The one you want is the button beside the application credential you just created.

```bash
source app-cred-qupa-cafe-openrc.sh
openstack flavor list                                # smoke test
openstack application credential show qupa-cafe      # what was actually created
```

The second command is worth running once.
It reports the roles and the `expires_at` the server actually recorded, rather than what the form appeared to accept, and a blank expiry shows up there as `None` — the only place it is stated plainly.

The smoke test also reports the flavor:

| Flavor | RAM | Disk | VCPUs |
|---|---|---|---|
| `m3.quad` | 15360 MB | 20 GB | 4 |

Those are the two numbers the install depends on: `docs/03-install.md` computes `db_shared_buffers` from the RAM and `UNICORN_WORKERS` from the core count, so confirm them here rather than after the install.

The flavor's own disk is 20 GB and irrelevant: 2.5 boots from a volume, which overrides it.

Use `openstack flavor list` rather than `openstack flavor show` for this.
`flavor show` additionally queries flavor access, which the `member` role cannot read, so it prints a `403 ... os_compute_api:os-flavor-access` error above the values it did retrieve.
The error is harmless and the values below it are correct, but it looks like a credential problem and is not one.

## 2.2 SSH key

> **The keypair can only be attached at instance creation.**
> There is no way to add one afterwards.
> An instance created without a key is unreachable and has to be rebuilt.

That is why this step comes first.

Jetstream2 accepts **only RSA or ed25519** keys.
A machine whose only key is ECDSA needs a new one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/qupa-cafe -C "qupa-cafe project key"
openstack keypair create --public-key ~/.ssh/qupa-cafe.pub qupa-cafe
```

> **Generate the key in the same place you will run the client from.**
> On Windows the client runs in WSL, so `~/.ssh` means the WSL home directory, not `C:\Users\<you>\.ssh`.
> A key generated on the Windows side is a different key, and `scripts/10-provision.sh` looks for `$HOME/.ssh/qupa-cafe.pub` in WSL and stops if it is not there.
>
> If the key already exists on the Windows side, copy it in rather than generating a second one, because only one of the two can be attached and the mistake is not correctable afterwards:
>
> ```bash
> cp /mnt/c/Users/<you>/.ssh/qupa-cafe{,.pub} ~/.ssh/
> chmod 600 ~/.ssh/qupa-cafe
> ```
>
> `KEY_FILE=/path/to/key.pub` overrides the location if you would rather leave the key where it is.

## 2.3 Security groups

Groups are additive, and the OpenStack-provided `default` group should not be modified.

```bash
openstack security group create --description "ssh and icmp enabled" qupa-cafe-ssh
openstack security group rule create --protocol tcp --dst-port 22:22 --remote-ip 0.0.0.0/0 qupa-cafe-ssh
openstack security group rule create --protocol icmp qupa-cafe-ssh

openstack security group create --description "http and https" qupa-cafe-web
openstack security group rule create --protocol tcp --dst-port 80:80   --remote-ip 0.0.0.0/0 qupa-cafe-web
openstack security group rule create --protocol tcp --dst-port 443:443 --remote-ip 0.0.0.0/0 qupa-cafe-web
```

The port 22 and ICMP rules are verbatim from Jetstream2's documentation.
The 80 and 443 rules are not published there, but they take the same form and both were accepted as written.

The project also contains a pre-existing `exosphere` security group.
Leave it alone: it belongs to the web interface, it permits far more than this deployment needs, and nothing here references it.

**These groups are the real ingress control for the forum**, not the host firewall.
Docker bypasses `ufw` by writing its own iptables chain, so a published container port is reachable whatever `ufw status` says.
`docs/02-host-setup.md` says this too; it is worth knowing in both places.

## 2.4 Storage

```bash
openstack volume create --size 100 qupa-cafe-data
```

The instance boots from a 60 GB volume rather than the flavor's 20 GB default root, because Docker's images and layers live on the root filesystem and `app.yml`'s `volumes:` retargeting does not move them.

Both disks draw on the same free 1 TB quota, so 160 GB costs nothing extra.
Maximum 10 volumes per allocation.

## 2.5 The instance

```bash
openstack network list          # confirm a single auto_allocated_network

openstack server create qupa-cafe \
  --flavor m3.quad --image Featured-Ubuntu24 \
  --boot-from-volume 60 --key-name qupa-cafe \
  --security-group qupa-cafe-ssh --security-group qupa-cafe-web \
  --wait

openstack server add volume qupa-cafe qupa-cafe-data
```

**Omit `--nic` rather than naming the network.**
With a single network, OpenStack selects `auto_allocated_network` automatically.
That is what Jetstream2 recommends, and it is the network that carries the DNS domain.

Featured images are `Featured-Ubuntu24`, `Featured-Ubuntu22`, `Featured-Minimal-Ubuntu24`, and `Featured-RockyLinux9`.
**Docker is preinstalled** on Featured images.

The default login user is **`ubuntu`** for CLI-created instances.
`exouser` is the Exosphere convention and does not apply here.

## 2.6 Floating IP

```bash
openstack floating ip create public
openstack server add floating ip qupa-cafe <IP>
```

## 2.7 DNS

Two names, for two jobs.

### The automatic one

Jetstream2 runs OpenStack Designate and creates a public record when a floating IP is attached.

The zone is the allocation id, lowercased, under `projects.jetstream-cloud.org` — here `cis261848.projects.jetstream-cloud.org.`
It is `ACTIVE` before any instance exists, so the zone comes with the allocation rather than being created by the first instance.

The record appears as soon as the floating IP is attached, with no waiting:

```
qupa-cafe.cis261848.projects.jetstream-cloud.org.  ->  149.165.147.42
```

```bash
openstack zone list
openstack recordset list cis261848.projects.jetstream-cloud.org.
dig +short <name>
```

Instance names are lowercased and underscores become dashes, so confirm the actual record rather than predicting it.

This name goes in `DISCOURSE_HOSTNAME_ALIASES`.

### The real one

The forum is `forum.qupa-cafe.com`, on a domain under this project's control.

The automatic name would work for the forum itself.
It will not work as a **sending identity for email**, because publishing SPF, DKIM, and DMARC requires control of the zone.
Since the hostname and the mail identity have to be the same domain, the forum gets the owned domain.

Add an A record at the registrar pointing `forum.qupa-cafe.com` at the floating IP.
The domain is on the registrar's own nameservers rather than behind a proxying CDN, so the record is a plain record and nothing intercepts the HTTP-01 challenge.

> **Wait for DNS before installing Discourse, and check the address rather than the answer.**
>
> ```bash
> dig +short forum.qupa-cafe.com
> ```
>
> This must return the floating IP before `docs/03-install.md`, or the certificate request fails and leaves a half-configured container.
>
> "It resolves" is not the test, because a registrar parking a domain may leave a **wildcard record** on the zone:
>
> ```bash
> dig +short definitely-not-a-real-name.qupa-cafe.com
> ```
>
> An answer there means unclaimed names resolve to a parking host.
> A wildcard applies only to names that hold no records at all, so whether it reaches the forum's name depends on what else is published there.
> Where it does reach, the name looks ready while pointing at the registrar's web server, which then answers the HTTP-01 challenge and fails it.
>
> Compare the answer against the floating IP rather than checking that something came back.
> Set `EXPECTED_IP` in `.env` and `scripts/30-install-discourse.sh` makes that comparison for you, which is the reliable version of this check.
>
> `providers/porkbun/README.md` works the rule through for this zone.

`forum.qupa-cafe.com` becomes `DISCOURSE_HOSTNAME`, which is the single value the generic guide takes from this platform.

## 2.8 Verification

```bash
openstack server show qupa-cafe
openstack quota show --usage
ssh -i ~/.ssh/qupa-cafe ubuntu@<floating-ip>
lsblk                                         # the 100 GB volume is visible
```

`lsblk` should show a 60 GB `sda` carrying `/` and a 100 GB `sdb` with no filesystem and no mount point.
`sdb` stays that way until `docs/02-host-setup.md` formats it.

Then re-run the provisioning script and confirm it is a **no-op the second time**.
An idempotent script that has only been run once is an untested claim, and the existence checks that make it idempotent are easy to get wrong in a way only a second run reveals.

## 2.9 Volume device naming

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
sudo dmesg | grep sd                          # sudo is required; see below
```

The root device is always `/dev/sdaX`.
**The data volume's letter is not guaranteed** across reboots, which is why `docs/02-host-setup.md` mounts by UUID.

Prefer `lsblk`: it needs no privileges, and it shows size, filesystem and mount point together, which is what you are actually trying to establish.

`dmesg` needs `sudo` on Ubuntu 24.04, where `kernel.dmesg_restrict` is `1`.
Without it the command fails and prints nothing, which reads like an absent device rather than a denied read.

Resizing a volume later is a Horizon operation followed by `sudo resize2fs /dev/sdb` on the host.

## 2.10 After provisioning

Lock the instance against accidental deletion, then go to `docs/02-host-setup.md`.

```bash
openstack server lock qupa-cafe
openstack server show qupa-cafe -f value -c locked    # True
```

> **A locked instance refuses API power actions too, not just deletion.**
> `openstack server reboot` returns `409 ... is locked`, and so do stop, start and resize.
> Reboot from inside the operating system with `sudo reboot`, which the lock does not affect, or unlock, act, and lock again.
>
> This matters because `docs/02-host-setup.md` ends in a deliberate reboot test.

The lock does not protect the filesystem, and any other allocation member can unlock it.
It guards against mistakes, not against intent.
