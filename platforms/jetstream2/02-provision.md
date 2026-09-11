# 2. Provisioning the instance

> **Status:** Not yet executed.
> Blocked on the Credits exchange in [`01-access-allocation.md`](01-access-allocation.md).
> Commands marked **[extrapolated]** follow a documented pattern but were not found verbatim in Jetstream2's documentation.

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
Jetstream2 signs you in through federated single sign-on, so there is no password that could go into a configuration file.
An application credential is also scoped and revocable on its own, which a login is not.

In Horizon, the OpenStack dashboard: **Identity** in the left-hand navigation, then **Application Credentials**, then **Create Application Credential**.
On success it shows the id and secret once, with a **Download openrc file** button.

The file it gives you is a shell script that exports the variables in [`openrc.sh.example`](openrc.sh.example), which is all `source` has to do with it.
Save it alongside that example as `openrc-CIS261848.sh`; `.gitignore` covers that name and not the downloaded one.
Jetstream2 documents the `openrc.sh` path only, not `clouds.yaml`.

Three traps:

> **A blank expiration field defaults to today.**
> Set one explicitly, or the credential expires the same day you made it.

> **The secret is shown once.**
> Download the file on the success screen; there is no way back to it, only a new credential.

> **Sourcing an openrc over a previous one fails**, with "Application credentials cannot request a scope".
> Use a fresh terminal.
> The error does not say that.

> **There is a second, wrong "download openrc" in Horizon** [unverified], under **API Access**, which produces a password-based file for a password you do not have.
> The one you want is the button beside the application credential you just created.

```bash
source openrc-CIS261848.sh
openstack flavor list          # smoke test
```

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
