# Brevo

The transactional email relay for `forum.qupa-cafe.com`.

> **Status:** DNS done and verified by resolution on 2026-09-11.
> The account exists, the domain-authentication records are published at Porkbun, and every one resolves — including the DKIM chains, which resolve to real RSA keys.
> Still outstanding: the SMTP key, and a delivered message with readable headers.
> Nothing below is marked `[unverified]` any more, and the record shapes are now what the panel asked for rather than what its documentation implied.

## Why a relay at all

`docs/01-requirements.md` has the reasoning.
The short version: Discourse cannot complete a password reset without SMTP, and a freshly allocated cloud address has no matching reverse DNS and poor sender reputation, so direct delivery lands in spam.

## Why this one

| | |
|---|---|
| Free tier | **300 emails per month** |
| Endpoint | `smtp-relay.brevo.com:587`, STARTTLS |
| Domain authentication | Yes, per subdomain |

> **Discourse's own `INSTALL-email.md` says this tier is 300 per _day_. It is 300 per _month_.**
> That document disclaims its own pricing accuracy.
> Take quota figures from the provider.

300 a month is about ten a day, which is ample for password resets and would not survive notification mail being left on.
That is why `docs/05-forum-config.md` turns everything else off, and why the suppression settings there are load-bearing rather than tidy.

Rejected alternatives, recorded so they are not revisited without cause:

**Stanford's `smtp.stanford.edu`** authenticates with a SUNet ID and password, which would put personal Stanford credentials on an internet-facing VM — a credential far more valuable than the forum.
Stanford's bypass policy also requires servers to sit on a University-IT-provisioned campus network, which a cloud VM does not.

**SendGrid** withdrew its permanent free plan in May 2025.

## Do this in Phase 1, not at install time

None of it needs the forum, the host, or an IP address.
Doing it early means SPF and DKIM have propagated long before the install needs them, and any friction surfaces while there is time to deal with it.

That reasoning held up.
The records were published before the host existed, so propagation is not on the critical path for the install, and the one surprise — that the panel asks for a different set of records than the written guidance describes — surfaced with nothing else half-built.

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

5. Brevo shows three records to add.
   All three are on the **subdomain**, and two of them are CNAMEs rather than TXT:

| Kind | Name | Type | Value |
|---|---|---|---|
| Ownership | `forum.qupa-cafe.com` | TXT | `brevo-code:<32 hex characters>` |
| DKIM 1 | `brevo1._domainkey.forum.qupa-cafe.com` | CNAME | `b1.forum-qupa-cafe-com.dkim.brevo.com` |
| DKIM 2 | `brevo2._domainkey.forum.qupa-cafe.com` | CNAME | `b2.forum-qupa-cafe-com.dkim.brevo.com` |

Three things in that table are not what the older written guidance leads you to expect.

**DKIM is delegated, not published.**
The records are CNAMEs pointing into `dkim.brevo.com`, so the key material lives with Brevo and rotates without anyone touching our zone.
Instructions that tell you to paste a `v=DKIM1; k=rsa; p=...` blob into a TXT record at `mail._domainkey` describe an older flow.

**The DKIM target embeds the domain with hyphens**, not dots: `forum-qupa-cafe-com`, once per selector.
Copy it from the panel.
A hand-typed `forum.qupa-cafe.com` in that position resolves to nothing and the failure is a verification that never completes, with no indication why.

**The ownership TXT sits at the subdomain itself**, sharing that name with anything else published there.
There is no `brevo-code.` label in front of it.

6. Add them in the Porkbun DNS panel for `qupa-cafe.com`.

> **Porkbun's host field is relative to the zone**, so the DKIM host is `brevo1._domainkey.forum`, not the full name from Brevo's panel.
> Pasting the absolute name produces `brevo1._domainkey.forum.qupa-cafe.com.qupa-cafe.com`, which resolves for nobody.
> The resolution check below is what tells you which of the two you did.

7. Brevo does not ask for an SPF record, and none is needed for DMARC to pass — see the SPF section below.
8. Add a DMARC record, which Brevo also does not ask for:

```
_dmarc.forum.qupa-cafe.com   TXT   "v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com"
```

`p=none` to start.
Observe before enforcing; a `p=reject` published before SPF and DKIM are confirmed aligned will silently destroy your own mail.

`rua@dmarc.brevo.com` sends the aggregate reports to Brevo, who surface them in their panel, rather than to a mailbox.
That is a real choice and not a default worth accepting blindly, because it means nobody reads the reports unless somebody opens that panel.
Any address you actually read is a defensible substitute.

> **A `rua` at another organisation's domain needs that organisation's permission, published in DNS.**
> RFC 7489 §7.1 requires a reporter to check for an authorisation record before sending reports across an organisational boundary, and to decline if it is absent.
> Brevo publishes ours, which is checked below.
> This is the failure mode where DMARC looks configured and no report is ever generated.

9. Wait for propagation, then verify in Brevo's panel.
10. Create an **SMTP key**, under **SMTP & API** -> **SMTP**.
    It gives a login and a key, which are the `DISCOURSE_SMTP_USER_NAME` and `DISCOURSE_SMTP_PASSWORD` in `.env`.

> **If the key contains a `#`, quote it in `app.yml`.**
> An unquoted `#` begins a YAML comment and the rest of the password is silently discarded.
> `config/app.yml.example` quotes it already; this matters if you ever hand-edit.

## SPF, and why there is not one

Brevo's domain authentication asks for ownership and DKIM only.
Older instructions add `v=spf1 include:spf.brevo.com ~all` on the sending domain, and it is worth being clear about why skipping it is not a gap.

SPF authenticates the **envelope** sender, which is the relay's own bounce domain, not the `From:` header.
An SPF record on `forum.qupa-cafe.com` would therefore pass for a domain that is not the one in `From:`, which is unaligned and contributes nothing to DMARC.
DMARC passes on aligned SPF **or** aligned DKIM, and here DKIM is the aligned one.

Publishing the include anyway is harmless and gives a receiver something to see for the From domain rather than `spf=none`.
We have not done it, so this page does not tell you it works.

> **`_spf.brevo.com` is not the include.**
> That name exists and publishes `v=spf1 -all`, which authorises nothing.
> The include is `spf.brevo.com`, without the underscore, and it publishes ten `ip4` ranges.
> The two differ by one character and fail in opposite directions.

## Verify from the command line

Run these before touching the panel's verify button.
`dig` on a Linux host:

```bash
dig +short TXT forum.qupa-cafe.com
dig +short CNAME brevo1._domainkey.forum.qupa-cafe.com
dig +short CNAME brevo2._domainkey.forum.qupa-cafe.com
dig +short TXT _dmarc.forum.qupa-cafe.com
dig +short TXT forum.qupa-cafe.com._report._dmarc.dmarc.brevo.com
```

Or `nslookup`, which is what is present on Windows:

```
nslookup -type=TXT forum.qupa-cafe.com 1.1.1.1
nslookup -type=CNAME brevo1._domainkey.forum.qupa-cafe.com 1.1.1.1
```

Naming a resolver explicitly matters more than it looks.
A stale negative answer in the local resolver's cache is indistinguishable from a record you forgot to add.

What resolved on 2026-09-11:

| Query | Answer |
|---|---|
| `TXT forum.qupa-cafe.com` | `brevo-code:<32 hex>` |
| `CNAME brevo1._domainkey.forum` | `b1.forum-qupa-cafe-com.dkim.brevo.com` |
| `CNAME brevo2._domainkey.forum` | `b2.forum-qupa-cafe-com.dkim.brevo.com` |
| `TXT _dmarc.forum.qupa-cafe.com` | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |
| `TXT forum.qupa-cafe.com._report._dmarc.dmarc.brevo.com` | `v=DMARC1`, so reports are authorised |

**Follow the DKIM CNAMEs to the end.**
Each is itself a CNAME into a numbered selector that holds the key, and only the last hop proves there is a key rather than a dangling delegation:

```bash
dig +short TXT b1.forum-qupa-cafe-com.dkim.brevo.com
```

Ours ends at `brevo7.dkim.brevo.com` and `brevo8.dkim.brevo.com`, each publishing a `k=rsa; p=<2048-bit key>`.
A CNAME that resolves while its target has no TXT is a DKIM setup that verifies in the panel and signs nothing.

## Then the check that actually matters

Once the forum is up, send a test message from Discourse's admin panel to an address at a major mailbox provider, open the received headers, and confirm `dkim=pass` with `d=forum.qupa-cafe.com` — the same domain as `From:`.

**Read the alignment, not the pass.**
`spf=pass` for the relay's bounce domain is a pass for a domain nobody sees, and it appears next to a `dmarc=fail` that is easy to overlook.
With no SPF record of our own, expect `spf=none` or an unaligned pass, and expect DKIM to be what carries DMARC.

`docs/03-install.md` §3.6 treats this as a gate on declaring the install finished.

## After it works

Watch the usage counter.

On a 300-per-month quota, anything climbing means something is sending that should not be, and the consequence is not cosmetic: the quota drains and password resets stop working silently.
`docs/08-runbook.md` says to check this counter **first** when a reset email does not arrive.
