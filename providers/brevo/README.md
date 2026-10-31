# Brevo

The transactional mail relay for `forum.qupa-cafe.com`.

> **Status:** Outstanding: a password reset that actually arrives.

| | |
|---|---|
| Free tier | 300 messages per day |
| Endpoint | `smtp-relay.brevo.com:587`, STARTTLS |
| Domain authentication | per subdomain |

300 a day covers resets and onboarding, but not a job that mails on a timer. That is why `40-configure-forum.py` turns notification mail off (`docs/05-forum-config.md`). Take quota figures from the provider.

## Steps

Do this early. None of it needs the host, and starting early takes DNS propagation off the critical path.

1. Create an account and send a test message from the panel, to find out whether sending is held for review.
2. **Senders, Domains & Dedicated IPs → Domains → Add a domain**: `forum.qupa-cafe.com`, the subdomain you send from, not the apex.
3. Skip the branded subdomain. Choose **Individual DNS records**, then **Manual**.
4. Add the four records at Porkbun, with hosts relative to the zone:

| Kind | Porkbun host | Type | Value |
|---|---|---|---|
| Ownership | `forum` | TXT | `brevo-code:<32 hex>` |
| DKIM 1 | `brevo1._domainkey.forum` | CNAME | `b1.forum-qupa-cafe-com.dkim.brevo.com` |
| DKIM 2 | `brevo2._domainkey.forum` | CNAME | `b2.forum-qupa-cafe-com.dkim.brevo.com` |
| DMARC | `_dmarc.forum` | TXT | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |

Copy the DKIM values from the panel: they spell the domain with hyphens. Leave DMARC at `p=none` until delivery is confirmed.

5. Check the records, below, then press verify.
6. **SMTP & API → SMTP**: create an SMTP key. Put the **SMTP login** (ending `@smtp-brevo.com`) and the key, shown once, into `.env` as `DISCOURSE_SMTP_USER_NAME` and `DISCOURSE_SMTP_PASSWORD`, unquoted.

## Check the records

```bash
dig +short @1.1.1.1 TXT forum.qupa-cafe.com
dig +short @1.1.1.1 CNAME brevo1._domainkey.forum.qupa-cafe.com
dig +short @1.1.1.1 CNAME brevo2._domainkey.forum.qupa-cafe.com
dig +short @1.1.1.1 TXT _dmarc.forum.qupa-cafe.com
dig +short @1.1.1.1 TXT b1.forum-qupa-cafe-com.dkim.brevo.com    # k=rsa;p=<2048-bit key>
```

Name the resolver, since a stale local negative cache looks like a missing record. The last query follows DKIM through to the key, and it is the check that proves signing will work.

## Then

Send a test from Discourse's admin Email page, and confirm `dkim=pass` with `d=forum.qupa-cafe.com`. `spf=none` is expected. After that, watch the usage counter. If it climbs while nobody joins, something is mailing on a timer, and resets will stop when the day's allowance runs out.
