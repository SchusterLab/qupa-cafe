# Brevo

The transactional email relay for `forum.qupa-cafe.com`.

> **Status:** Not yet done.
> Every record name below is marked, because they must be taken from Brevo's own panel rather than from here.
> This page is a checklist and a set of traps, not a source of truth for record values.

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

> **Free tiers sometimes hold sending pending account review.**
> Create the account and send a throwaway test message now.
> Discovering a hold while the forum is half-built is the avoidable version of this problem.

## Steps

1. Create a Brevo account.
2. Send a test message from their panel, to confirm sending is actually enabled and not pending review.
3. Go to **Senders, Domains & Dedicated IPs** -> **Domains** -> **Add a domain**.
4. Enter **`forum.qupa-cafe.com`**.

> **Authenticate the subdomain you send from, not the apex.**
> Discourse's own documentation is explicit that verifying `qupa-cafe.com` while sending as `forum.qupa-cafe.com` leaves mail misconfigured.

5. Brevo shows the records to add, of three kinds:

| Kind | Expected shape |
|---|---|
| Ownership | a TXT record, `brevo-code` **[unverified]** |
| DKIM | a TXT record under `mail._domainkey` **[unverified]** |
| SPF | an include for `spf.brevo.com` **[unverified]** |

> **Take these from the panel, not from this table.**
> The names above follow Brevo's documented pattern but were not confirmed against a live account, which is what the markers mean.

6. Add them in the Porkbun DNS panel for `qupa-cafe.com`.
7. Add a DMARC record as well, which Brevo does not ask for:

```
_dmarc.forum.qupa-cafe.com   TXT   "v=DMARC1; p=none; rua=mailto:<an address you read>"
```

`p=none` to start.
Observe before enforcing; a `p=reject` published before SPF and DKIM are confirmed aligned will silently destroy your own mail.

8. Wait for propagation, then verify in Brevo's panel.
9. Create an **SMTP key**, under **SMTP & API** -> **SMTP**.
   It gives a login and a key, which are the `DISCOURSE_SMTP_USER_NAME` and `DISCOURSE_SMTP_PASSWORD` in `.env`.

> **If the key contains a `#`, quote it in `app.yml`.**
> An unquoted `#` begins a YAML comment and the rest of the password is silently discarded.
> `config/app.yml.example` quotes it already; this matters if you ever hand-edit.

## Verify from the command line

```bash
dig +short TXT forum.qupa-cafe.com
dig +short TXT mail._domainkey.forum.qupa-cafe.com
dig +short TXT _dmarc.forum.qupa-cafe.com
```

Then, once the forum is up, the check that actually matters.
Send a test message from Discourse's admin panel to an address at a major mailbox provider, open the received headers, and confirm `spf=pass` and `dkim=pass` with the From domain **aligned**.

Passing SPF for a different domain than the one in `From:` looks like a pass at a glance and is not one.

## After it works

Watch the usage counter.

On a 300-per-month quota, anything climbing means something is sending that should not be, and the consequence is not cosmetic: the quota drains and password resets stop working silently.
`docs/08-runbook.md` says to check this counter **first** when a reset email does not arrive.
