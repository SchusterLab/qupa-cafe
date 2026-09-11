# 2. Provisioning the instance

> **Status:** Not yet executed.
> Blocked on the Credits exchange in [`01-access-allocation.md`](01-access-allocation.md).
> Commands marked **[extrapolated]** follow a documented pattern but were not found verbatim in Jetstream2's documentation.
> The dashboard URL and the two login methods in 2.1 were checked against the live login page on 2026-09-11; nothing behind the login has been.
> The expiration behaviour in 2.1 was read from Horizon's source rather than guessed at, after the earlier claim about it turned out to be wrong.

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

Jetstream2 tells Windows users to use WSL, and that is what we did.
Git Bash is not a supported path for the client.

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

What we filled in [unverified]:

| Field | Value |
|---|---|
| Name | `qupa-cafe` |
| Description | `OpenStack CLI for the Qupa Cafe forum host (CIS261848)` |
| Secret | blank |
| Expiration Date | `2027-09-10` |
| Expiration Time | blank |
| Roles | `member` and `reader` |
| Access Rules | blank |
| Unrestricted | unchecked |

Four of those are decisions rather than form-filling.

**The expiration date is a decision, and the two fields interact.**
A date with no time means **00:00 on that date**, so the day you enter is the first day the credential does not work.
`2027-09-10` is therefore how you cover an allocation that ends on the 9th.

Tying it to the allocation's end means the credential cannot outlive the project it exists for.
A shorter life is better security practice, but the failure it invites is a credential that expires mid-year and produces an authentication error that never mentions expiry.

**Take `member` and `reader`, not everything offered.**
Four roles were on offer here: `reader`, `creator`, `member`, and `load-balancer_member`.
`scripts/10-provision.sh` uses compute, block storage, networking and DNS, all of which `member` covers, and `reader` is worth keeping beside it because some services want an explicit read role under newer RBAC.
`creator` is the key manager's role and `load-balancer_member` is the load balancer's; this deployment creates neither.

Selecting the extra two is not an escalation — a credential can only carry roles you already hold — but it widens what the secret can do if it leaks, and buys nothing.

**Leave the secret blank** and let Horizon generate one.
Choosing your own changes nothing — it arrives in the same downloaded file — and is one more secret to keep.

**Leave Unrestricted unchecked.**
Horizon labels it dangerous because it lets the credential mint further credentials and trusts, and provisioning needs neither.

**Leave Access Rules blank.**
Restricting the credential to named API calls is the appealing option, and we did not take it: `scripts/10-provision.sh` spans compute, block storage, networking and DNS, so the list would be long and easy to get subtly wrong.
A dated expiry and one-click revocation are the security story instead.
If you want the restricted version, upstream documents the syntax — treat it as something to work out and test, not to copy.

On success Horizon shows the id and secret once, with a **Download openrc file** button.

The file it gives you is a shell script that exports the variables in [`openrc.sh.example`](openrc.sh.example), which is all `source` has to do with it.
Save it alongside that example as `openrc-CIS261848.sh`; `.gitignore` covers that name and not the downloaded one.
Jetstream2 documents the `openrc.sh` path only, not `clouds.yaml`.

Three traps:

> **An expiration *time* with no date means today.**
> Horizon fills the date in from the clock, so a credential given only a time expires that same day — possibly at a time that has already passed.
> Leaving *both* fields blank does the opposite: the credential never expires.
> Neither is stated on the form; both are in Horizon's `identity/application_credentials` form, in the branch on `expiration_date`.

> **The secret is shown once.**
> Download the file on the success screen; there is no way back to it, only a new credential.

> **Sourcing an openrc over a previous one fails**, with "Application credentials cannot request a scope".
> Use a fresh terminal.
> The error does not say that.

> **There is a second, wrong "download openrc" in Horizon** [unverified], under **API Access**, which produces a password-based file for a password you do not have.
> The one you want is the button beside the application credential you just created.

```bash
source openrc-CIS261848.sh
openstack flavor list                                # smoke test
openstack application credential show qupa-cafe      # what was actually created
```

The second command is worth running once.
It reports the roles and the `expires_at` the server recorded, rather than what the form appeared to accept — and a blank expiry shows up there as `None`, which is the only place it is stated plainly.

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

## 2.3 Security groups

Groups are additive, and the OpenStack-provided `default` group should not be modified.

```bash
openstack security group create --description "ssh and icmp enabled" qupa-cafe-ssh
openstack security group rule create --protocol tcp --dst-port 22:22 --remote-ip 0.0.0.0/0 qupa-cafe-ssh
openstack security group rule create --protocol icmp qupa-cafe-ssh

openstack security group create --description "http and https" qupa-cafe-web
openstack security group rule create --protocol tcp --dst-port 80:80   --remote-ip 0.0.0.0/0 qupa-cafe-web   # [extrapolated]
openstack security group rule create --protocol tcp --dst-port 443:443 --remote-ip 0.0.0.0/0 qupa-cafe-web   # [extrapolated]
```

The port 22 and ICMP rules are verbatim from Jetstream2's documentation.
The 80 and 443 rules follow the same pattern but were not published anywhere we could find, so they are marked.

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

```bash
openstack zone list
openstack recordset list <project>.projects.jetstream-cloud.org.
dig +short <name>
```

Instance names are lowercased and underscores become dashes, so confirm the actual record rather than predicting it.

This name goes in `DISCOURSE_HOSTNAME_ALIASES`.

### The real one

The forum is `forum.qupa-cafe.com`, on a domain we control.

The automatic name would work for the forum itself.
It will not work as a **sending identity for email**, because publishing SPF, DKIM, and DMARC requires control of the zone.
Since the hostname and the mail identity have to be the same domain, the forum gets the domain we own.

Add an A record at the registrar pointing `forum.qupa-cafe.com` at the floating IP.
The domain is on the registrar's own nameservers rather than behind a proxying CDN, so the record is a plain record and nothing intercepts the HTTP-01 challenge.

> **Wait for DNS before installing Discourse.**
>
> ```bash
> dig +short forum.qupa-cafe.com
> ```
>
> This must return the floating IP before `docs/03-install.md`, or the certificate request fails and leaves a half-configured container.

`forum.qupa-cafe.com` becomes `DISCOURSE_HOSTNAME`, which is the single value the generic guide takes from this platform.

## 2.8 Verification

```bash
openstack server show qupa-cafe
openstack quota show --usage
ssh -i ~/.ssh/qupa-cafe ubuntu@<floating-ip>
lsblk                                         # the 100 GB volume is visible
```

Then re-run the provisioning script and confirm it is a **no-op the second time**.
An idempotent script that has only been run once is an untested claim.

## 2.9 Volume device naming

```bash
dmesg | grep sd
```

The root device is always `/dev/sdaX`.
**The data volume's letter is not guaranteed** across reboots, which is why `docs/02-host-setup.md` mounts by UUID.

Resizing a volume later is a Horizon operation followed by `sudo resize2fs /dev/sdb` on the host.

## 2.10 After provisioning

Use the **Lock** action on the instance to prevent accidental deletion, and go to `docs/02-host-setup.md`.
