# Porkbun

The registrar and DNS host for `qupa-cafe.com`, on Porkbun's own nameservers, so records are plain and nothing intercepts the HTTP-01 challenge.

**The host field is relative to the zone.** `forum.qupa-cafe.com` is entered as `forum`; the full name produces `forum.qupa-cafe.com.qupa-cafe.com`.

**A parked domain carries a wildcard** `*` CNAME to a parking host (`uixie.porkbun.com`). It answers only for names with no records of their own, so `forum`, which holds Brevo's TXT record, is outside it. On a name with nothing else on it, an A record published late would resolve to the parking host meanwhile, which is why the install compares the answer with `EXPECTED_IP`.

## The forum's A record

After the floating IP exists and before installing: **Domain Management → qupa-cafe.com → DNS**, and add

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

The apex's default SPF record is unused; mail is sent as `forum.qupa-cafe.com`, which has none, so `spf=none` is expected.

**Keep the domain on auto-renew with a valid card**: the forum's hostname and its mail identity both depend on it.
