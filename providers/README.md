# Providers

`docs/` states the requirement.
This directory records how we actually met it, for the services that are not the host.

`platforms/` is the sibling directory for hosts.
The split is the same in both cases: the guide is written against requirements, and the vendor specifics live somewhere a reader can ignore.

## Why this directory exists

`docs/01-requirements.md` says the deployment needs an SMTP relay, a domain you control, and authentication records published for the sending subdomain.
It deliberately does not say whose relay or whose DNS.

But the records a particular relay wants, and the exact panel to click in a particular registrar, are things we had to work out, and a reproducer benefits from seeing a worked example.
Putting them here keeps them out of the guide without throwing them away.

## What is here

| Directory | What it is | What it satisfies |
|---|---|---|
| [`brevo/`](brevo/) | The transactional email relay we used | The SMTP relay requirement in `docs/01-requirements.md` |

DNS for `qupa-cafe.com` is at Porkbun, on their own nameservers rather than behind a proxying CDN.
That matters for one reason and it is recorded in `docs/01-requirements.md` as a requirement: a proxied record intercepts the HTTP-01 challenge, and then the container cannot obtain its own certificate.

Their default zone also carries a wildcard `*.qupa-cafe.com` pointing at a parking host, and it is worth knowing how that interacts with the subdomain we send from.
A wildcard answers only for names that exist nowhere else in the zone, so publishing the relay's records at `forum.qupa-cafe.com` took that name out of its scope.
Verified on 2026-09-11: `forum.qupa-cafe.com` returns no address at all, while an unused name beside it still resolves to the parking host.
The consequence is the convenient one.
The forum's address record can be added at provisioning time with no parking answer cached ahead of it, which is one fewer reason for the certificate challenge to arrive somewhere unexpected.

## If you are using something else

Read `brevo/README.md` as an example of what to look for, not as instructions.

Any transactional relay will do, provided it lets you authenticate a sending **subdomain** and gives you an SMTP username and password for port 587.
The two things worth checking before you commit to one are in `docs/01-requirements.md`: what the quota actually is, and whether the free tier requires account review before it will send at all.
