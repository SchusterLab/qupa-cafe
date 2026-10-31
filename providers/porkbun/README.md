# Porkbun

The registrar and DNS host for `qupa-cafe.com`. The domain uses Porkbun's own nameservers, so records are plain and nothing intercepts the HTTP-01 challenge.

**The host field is relative to the zone.** `forum.qupa-cafe.com` is entered as `forum`. Entering the full name produces `forum.qupa-cafe.com.qupa-cafe.com`.

**A parked domain carries a wildcard** `*` CNAME to a parking host (`uixie.porkbun.com`). The wildcard answers only for names with no records of their own. So `forum`, which holds Brevo's TXT record, is outside it. A name with no records resolves to the parking host until its A record is published. That is why the install compares the answer with `EXPECTED_IP`.

## The forum's A record

Add this record after the floating IP exists and before installing. Go to **Domain Management → qupa-cafe.com → DNS**, and add:

| Type | Host | Answer | TTL |
|---|---|---|---|
| `A` | `forum` | the floating IP, 149.165.153.151 | `600` |

```bash
dig +short @1.1.1.1 A forum.qupa-cafe.com    # must be the floating IP
```

A `207.207.210.x` answer means the wildcard still covers the name. The short TTL keeps a rebuild onto a new IP short.

## The zone

| Name | Type | Purpose |
|---|---|---|
| `forum` | A | The forum host |
| `forum` | TXT | Brevo domain ownership |
| `brevo1._domainkey.forum`, `brevo2._domainkey.forum` | CNAME | DKIM |
| `_dmarc.forum` | TXT | DMARC, `p=none` |

The apex's default SPF record is unused. Mail is sent as `forum.qupa-cafe.com`, which has no SPF record, so `spf=none` is expected.

**Keep the domain on auto-renew with a valid card**: the forum's hostname and its mail identity both depend on it.
