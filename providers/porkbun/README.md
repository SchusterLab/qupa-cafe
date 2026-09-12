# Porkbun

The registrar and DNS host for `qupa-cafe.com`.

The domain sits on Porkbun's own nameservers rather than behind a proxying CDN. That is the arrangement the rest of the guide assumes: records are plain records, and nothing intercepts the Let's Encrypt HTTP-01 challenge during the install.

Nameservers are `curitiba`, `salvador`, `fortaleza` and `maceio`, each under `.ns.porkbun.com`.

## Two things about this zone that affect other chapters

**The host field is relative to the zone.** A record for `forum.qupa-cafe.com` is entered with a host of `forum`, and the apex is the empty string. Entering the fully qualified name produces `forum.qupa-cafe.com.qupa-cafe.com`, which resolves to nothing and looks like a propagation delay.

`providers/brevo/README.md` depends on this for the DKIM hosts, which are the longest names in the zone and the easiest to get wrong.

**A parked domain carries a wildcard.**

```bash
dig +short definitely-not-a-real-name.qupa-cafe.com
```

Porkbun's default zone answers that with `uixie.porkbun.com`, because it includes a `*` CNAME pointing at a parking host.

The rule that makes this safe is worth stating exactly, because guessing it wrong in either direction causes trouble. **A wildcard answers only for a name that has no records of its own.** Once any record exists at a name, the wildcard stops applying to that whole name, for every record type.

So `forum` was already out of the wildcard's scope before its address record existed, because `providers/brevo/README.md` publishes a TXT record there. A query for its address returns an empty answer rather than the parking host:

```bash
dig +short A forum.qupa-cafe.com          # empty until the A record is added
dig +short TXT forum.qupa-cafe.com        # the Brevo ownership record
```

That ordering is the convenient one. The mail records went in first, which took the name out of the wildcard's reach, so no parking answer for `forum` was ever cached and the certificate challenge cannot be answered by Porkbun's web server.

The hazard is real for anyone publishing the address record first, on a name with nothing else on it. Then the name does resolve — to the parking host — and the install's DNS check passes while pointing at the wrong server. This is why `platforms/jetstream2/02-provision.md` compares the answer against the floating IP rather than merely confirming that something came back.

The wildcard does not have to be removed either way. Removing it is tidier if nothing depends on it, and it makes a genuinely missing record fail loudly instead of quietly.

## Add the forum's A record

Do this after the floating IP exists and before installing Discourse.

**Get the IP first.** It is the floating IP attached to the instance, and there are three places to read it, in order of how much you should trust them:

```bash
openstack floating ip list --long                      # every floating IP in the project
openstack server show qupa-cafe -f value -c addresses  # the one actually attached
grep EXPECTED_IP .env                                  # what this repository expects
```

`platforms/jetstream2/02-provision.md` is where that address comes from. If the first two disagree, `.env` is stale and `scripts/30-install-discourse.sh` will refuse to install, which is the point of `EXPECTED_IP`.

1. Sign in at <https://porkbun.com> and open **Domain Management**.
2. Find `qupa-cafe.com` and open its DNS records editor.
3. Add a record:

   | Field | Value |
   |---|---|
   | Type | `A` |
   | Host | `forum` |
   | Answer | the floating IP. The panel labels this field **IPv4 Address** once `A` is selected. |
   | TTL | `600` |

4. Save, then confirm from somewhere outside your own resolver cache:

   ```bash
   dig +short @1.1.1.1 A forum.qupa-cafe.com
   ```

   The answer must be the floating IP. An empty answer means the record has not propagated yet; a `207.207.210.x` answer means it was entered on a name the wildcard still covers, so check the host field.

A short TTL is worth setting before the first install rather than after. If the instance is ever rebuilt onto a new floating IP, the forum is unreachable for one TTL, and 600 seconds is a better number to discover that with than 3600.

## What else lives in this zone

| Name | Type | Purpose | Chapter |
|---|---|---|---|
| `forum` | A | The forum host | `platforms/jetstream2/02-provision.md` |
| `forum` | TXT | Brevo domain ownership | `providers/brevo/README.md` |
| `brevo1._domainkey.forum` | CNAME | DKIM | `providers/brevo/README.md` |
| `brevo2._domainkey.forum` | CNAME | DKIM | `providers/brevo/README.md` |
| `_dmarc.forum` | TXT | DMARC at `p=none` | `providers/brevo/README.md` |

The apex carries Porkbun's own default SPF record, which this deployment does not use and does not need. Mail is sent as `forum.qupa-cafe.com`, and SPF is checked against that name, which publishes no SPF record of its own. `providers/brevo/README.md` explains why `spf=none` is the expected result rather than a fault.

## Renewal

The forum's hostname and its mail identity both depend on this registration. Losing the domain takes out the forum and account recovery together.

> **Keep the registration on auto-renew, and keep a card on file that has not expired.**

`platforms/jetstream2/01-access-allocation.md` already puts the allocation's renewal deadline in a calendar. The domain's expiry is a separate date and needs its own.
