# 2. Provisioning the instance

This part creates the host, its storage, its network access and its hostname, and ends at a machine you can SSH into. `scripts/10-provision.sh` does all of it idempotently, and this page explains what it does.

## 2.1 The CLI and its credential

Provision with the CLI, not Exosphere. Exosphere's default security group admits all inbound traffic, and Exosphere cannot edit groups. On Windows, run the client in WSL.

```bash
sudo apt-get install -y python3-openstackclient python3-designateclient
```

The login is federated (ACCESS CILogon), so there is no password for an openrc file to hold. Instead, the CLI uses an **application credential**. To create one, open Horizon at <https://js2.jetstream-cloud.org> (not Exosphere) and go to **Identity → Application Credentials → Create Application Credential**:

| Field | Value |
|---|---|
| Name | `qupa-cafe` |
| Secret, Expiration Date, Expiration Time, Access Rules | blank |
| Roles | `member` and `reader` |
| Unrestricted | unchecked |

A blank expiry means the credential never expires, so revoke it in Horizon when the allocation ends. If you do set an expiry, a time with no date means today, and a date with no time means 00:00 on that date.

**The secret is shown once.** On the success screen, use the **Download openrc file** button beside the credential, not the password-based one under API Access. Save the file under the name it arrives with, `app-cred-qupa-cafe-openrc.sh`, next to [`openrc.sh.example`](openrc.sh.example). `.gitignore` matches `*openrc*.sh`.

```bash
source app-cred-qupa-cafe-openrc.sh                  # in a fresh terminal
openstack flavor list                                # m3.quad: 15360 MB, 4 VCPUs
openstack application credential show qupa-cafe      # expires_at None
```

Sourcing it over an earlier openrc fails with "Application credentials cannot request a scope", so use a new terminal. Use `flavor list` rather than `flavor show`, which prints a harmless 403 on flavor access. `docs/03-install.md` sizes `db_shared_buffers` and `UNICORN_WORKERS` from the flavor's RAM and cores.

## 2.2 SSH key

A keypair can be attached only when the instance is created, and Jetstream2 accepts only RSA or ed25519 keys. Generate the key where the client runs, which on Windows is WSL. Alternatively, copy an existing Windows-side key into `~/.ssh` in WSL:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/qupa-cafe -C "qupa-cafe project key"
openstack keypair create --public-key ~/.ssh/qupa-cafe.pub qupa-cafe
```

`KEY_FILE=/path/to/key.pub` overrides the location the script reads.

## 2.3 Security groups

Leave `default` and `exosphere` alone.

```bash
openstack security group create --description "ssh and icmp enabled" qupa-cafe-ssh
openstack security group rule create --protocol tcp --dst-port 22:22 --remote-ip 0.0.0.0/0 qupa-cafe-ssh
openstack security group rule create --protocol icmp qupa-cafe-ssh

openstack security group create --description "http and https" qupa-cafe-web
openstack security group rule create --protocol tcp --dst-port 80:80   --remote-ip 0.0.0.0/0 qupa-cafe-web
openstack security group rule create --protocol tcp --dst-port 443:443 --remote-ip 0.0.0.0/0 qupa-cafe-web
```

These groups are the forum's real ingress control, because Docker bypasses `ufw`.

## 2.4 Storage and the instance

```bash
openstack volume create --size 100 qupa-cafe-data

openstack server create qupa-cafe \
  --flavor m3.quad --image Featured-Ubuntu24 \
  --boot-from-volume 60 --key-name qupa-cafe \
  --security-group qupa-cafe-ssh --security-group qupa-cafe-web \
  --wait
openstack server add volume qupa-cafe qupa-cafe-data

openstack floating ip create public
openstack server add floating ip qupa-cafe <IP>
```

The 60 GB boot volume replaces the flavor's 20 GB root, because Docker's images stay on root. Omit `--nic`, and the single `auto_allocated_network` is chosen. That network carries the DNS domain. Featured images have Docker preinstalled, and the login user is `ubuntu`.

## 2.5 DNS

Attaching the floating IP creates a Designate record in the allocation's zone, `qupa-cafe.cis261848.projects.jetstream-cloud.org`. That record is not used, because the certificate covers only `forum.qupa-cafe.com`.

The forum's name is an A record for `forum.qupa-cafe.com` at the registrar, pointing at the floating IP. `providers/porkbun/README.md` has the steps. Check that the answer is the floating IP, not merely that the name resolves, because a parked zone's wildcard also resolves:

```bash
dig +short forum.qupa-cafe.com                         # must be the floating IP
dig +short definitely-not-a-real-name.qupa-cafe.com    # an answer means a wildcard
```

Set `EXPECTED_IP` in `.env`, and `scripts/30-install-discourse.sh` refuses to install when the name resolves to anything else.

## 2.6 Verify and lock

```bash
openstack server show qupa-cafe
ssh -i ~/.ssh/qupa-cafe ubuntu@<floating-ip> lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
```

`lsblk` shows a 60 GB `sda` carrying `/` and a bare 100 GB `sdb`. Device letters are not stable across reboots, which is why `docs/02-host-setup.md` mounts by UUID.

Re-run `scripts/10-provision.sh` and confirm it changes nothing, then lock the instance against deletion:

```bash
openstack server lock qupa-cafe
openstack server show qupa-cafe -f value -c locked    # True
```

A locked instance also refuses a reboot, stop or resize through the API (`409 ... is locked`). To reboot it, run `sudo reboot` inside it. Any allocation member can unlock it.

## 2.7 Rebuilding

A rebuild gets a new floating IP, so update the A record and `EXPECTED_IP`. Each rebuild with a fresh data volume also requests a new certificate, and Let's Encrypt's weekly limit then applies (`docs/03-install.md` 3.5).
