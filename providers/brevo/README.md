# Brevo

The transactional email relay for `forum.qupa-cafe.com`.

> **Status:** Done as far as it can be without the forum, on 2026-09-11.
> The account exists, the four records are published at Porkbun and all four resolve, and the SMTP key is generated.
> Outstanding: a password reset that actually arrives, which needs the forum.

## Why a relay at all

Discourse cannot complete a password reset without SMTP, and mail sent straight from a freshly allocated cloud address lands in spam.
`docs/01-requirements.md` has the rest.

## Why this one

| | |
|---|---|
| Free tier | **300 emails per month** |
| Endpoint | `smtp-relay.brevo.com:587`, STARTTLS |
| Domain authentication | Yes, per subdomain |

> **Discourse's own `INSTALL-email.md` says this tier is 300 per _day_. It is 300 per _month_.**
> Take quota figures from the provider.

300 a month is about ten a day.
Ample for password resets, and it would not survive notification mail being left on, which is why `docs/05-forum-config.md` turns everything else off.

Rejected alternatives, recorded so they are not revisited without cause:

**Stanford's `smtp.stanford.edu`** authenticates with a SUNet ID and password, which would put personal Stanford credentials on an internet-facing VM — a credential far more valuable than the forum.
Stanford's bypass policy also requires servers to sit on a University-IT-provisioned campus network, which a cloud VM does not.

**SendGrid** withdrew its permanent free plan in May 2025.

## Do this in Phase 1, not at install time

None of it needs the forum, the host, or an IP address, and doing it early keeps DNS propagation off the critical path.

> **Free tiers sometimes hold sending pending account review.**
> Create the account and send a throwaway test message.
> Discovering a hold while the forum is half-built is the avoidable version of this problem.

## Steps

1. Create a Brevo account.
2. Send a test message from their panel, to confirm sending is actually enabled and not pending review.
3. Go to **Senders, Domains & Dedicated IPs** -> **Domains** -> **Add a domain**.
4. Enter **`forum.qupa-cafe.com`**.

> **Authenticate the subdomain you send from, not the apex.**
> Discourse's own documentation is explicit that verifying `qupa-cafe.com` while sending as `forum.qupa-cafe.com` leaves mail misconfigured.

5. Brevo shows four records on one page, all of them on the subdomain.
   Add them in the Porkbun DNS panel for `qupa-cafe.com`.

| Kind | Name | Type | Value |
|---|---|---|---|
| Ownership | `forum.qupa-cafe.com` | TXT | `brevo-code:<32 hex characters>` |
| DKIM 1 | `brevo1._domainkey.forum.qupa-cafe.com` | CNAME | `b1.forum-qupa-cafe-com.dkim.brevo.com` |
| DKIM 2 | `brevo2._domainkey.forum.qupa-cafe.com` | CNAME | `b2.forum-qupa-cafe-com.dkim.brevo.com` |
| DMARC | `_dmarc.forum.qupa-cafe.com` | TXT | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |

> **Copy the DKIM values from the panel rather than typing them.**
> They embed the domain with hyphens — `forum-qupa-cafe-com` — and a hand-typed `forum.qupa-cafe.com` in that position resolves to nothing.
> The only symptom is a verification that never completes.

> **Porkbun's host field is relative to the zone.**
> The DKIM host is `brevo1._domainkey.forum`, not the full name Brevo displays.
> The ownership TXT goes on `forum` itself, with no `brevo-code.` label in front of it.

> **Leave DMARC at the `p=none` Brevo offers.**
> Tightening it to `p=reject` before delivery is confirmed silently destroys your own mail, password resets included.

6. Check the records resolve, below, then press verify in Brevo's panel.
7. Create an **SMTP key** under **SMTP & API** -> **SMTP**.
   Its login and key are `DISCOURSE_SMTP_USER_NAME` and `DISCOURSE_SMTP_PASSWORD` in `.env`.

> **If the key contains a `#`, quote it in `app.yml`.**
> An unquoted `#` begins a YAML comment and the rest of the password is silently discarded.
> `config/app.yml.example` quotes it already; this matters if you ever hand-edit.

## Check the records resolve

`dig` on a Linux host, `nslookup` on Windows:

```bash
dig +short @1.1.1.1 TXT forum.qupa-cafe.com
dig +short @1.1.1.1 CNAME brevo1._domainkey.forum.qupa-cafe.com
dig +short @1.1.1.1 CNAME brevo2._domainkey.forum.qupa-cafe.com
dig +short @1.1.1.1 TXT _dmarc.forum.qupa-cafe.com
dig +short @1.1.1.1 TXT b1.forum-qupa-cafe-com.dkim.brevo.com
```

Name the resolver explicitly, as above, or `nslookup -type=TXT forum.qupa-cafe.com 1.1.1.1`.
A stale negative answer in a local cache looks exactly like a record you forgot to add.

The last query is the one to remember.
It follows the DKIM record through to the key itself, and a DKIM record that resolves while its target holds no key verifies in the panel and signs nothing.

What ours returned on 2026-09-11:

| Query | Answer |
|---|---|
| `TXT forum.qupa-cafe.com` | `brevo-code:<32 hex>` |
| `CNAME brevo1._domainkey.forum` | `b1.forum-qupa-cafe-com.dkim.brevo.com` |
| `CNAME brevo2._domainkey.forum` | `b2.forum-qupa-cafe-com.dkim.brevo.com` |
| `TXT _dmarc.forum.qupa-cafe.com` | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |
| `TXT b1.forum-qupa-cafe-com.dkim.brevo.com` | `k=rsa; p=<2048-bit key>` |

## Then send a real message

Once the forum is up: Discourse admin -> Email -> send a test to an address at a major mailbox provider.
Open the received headers and confirm `dkim=pass` with `d=forum.qupa-cafe.com`, the same domain as the one in `From:`.

`spf=none` is expected here and is not a fault.

`docs/03-install.md` §3.6 treats this as a gate on declaring the install finished.

## After it works

Watch the usage counter.

On a 300-per-month quota, anything climbing means something is sending that should not be, and the consequence is not cosmetic: the quota drains and password resets stop working silently.
`docs/08-runbook.md` says to check this counter **first** when a reset email does not arrive.
