# Providers

`docs/` states the requirement.
This directory records how we actually met it, for the services that are not the host.

`platforms/` is the sibling directory for hosts.
The split is the same in both cases: the guide is written against requirements, and the vendor specifics live somewhere a reader can ignore.

## Why this directory exists

`docs/01-requirements.md` says the deployment needs an SMTP relay, a domain you control, and SPF, DKIM, and DMARC published for the sending subdomain.
It deliberately does not say whose relay or whose DNS.

But the record names a particular relay wants, and the exact panel to click in a particular registrar, are things we had to work out, and a reproducer benefits from seeing a worked example.
Putting them here keeps them out of the guide without throwing them away.

## What is here

| Directory | What it is | What it satisfies |
|---|---|---|
| [`brevo/`](brevo/) | The transactional email relay we used | The SMTP relay requirement in `docs/01-requirements.md` |

DNS for `qupa-cafe.com` is at Porkbun, on their own nameservers rather than behind a proxying CDN.
That matters for one reason and it is recorded in `docs/01-requirements.md` as a requirement: a proxied record intercepts the HTTP-01 challenge, and then the container cannot obtain its own certificate.

## If you are using something else

Read `brevo/README.md` as an example of what to look for, not as instructions.

Any transactional relay will do, provided it lets you authenticate a sending **subdomain** and gives you an SMTP username and password for port 587.
The two things worth checking before you commit to one are in `docs/01-requirements.md`: what the quota actually is, and whether the free tier requires account review before it will send at all.
