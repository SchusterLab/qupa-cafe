# Providers

`docs/` states the requirement. This directory records how one deployment met it, for the services that are not the host.

`platforms/` is the sibling directory for hosts. The split is the same in both cases: the guide is written against requirements, and the vendor specifics live somewhere a reader can ignore.

## Why this directory exists

`docs/01-requirements.md` says the deployment needs an SMTP relay, a domain you control, and authentication records published for the sending subdomain. It deliberately does not say whose relay or whose DNS.

But the records a particular relay wants, and the exact panel to click in a particular registrar, take working out, and a reproducer benefits from seeing a worked example. Putting them here keeps them out of the guide without throwing them away.

## What is here

| Directory | What it is | What it satisfies |
|---|---|---|
| [`brevo/`](brevo/) | The transactional email relay | The SMTP relay requirement in `docs/01-requirements.md` |
| [`porkbun/`](porkbun/) | The registrar and DNS host | The domain and DNS requirements in `docs/01-requirements.md` |

The two are coupled more tightly than the split suggests, which is why both are needed to satisfy either. The relay authenticates a sending subdomain by asking for records in the registrar's zone, and the forum's hostname has to be that same subdomain, so neither can be chosen independently of the other. `docs/01-requirements.md` states that as one requirement rather than two.

## If you are using something else

Read `brevo/README.md` as an example of what to look for, not as instructions.

Any transactional relay will do, provided it lets you authenticate a sending **subdomain** and gives you an SMTP username and password for port 587. The two things worth checking before you commit to one are in `docs/01-requirements.md`: what the quota actually is, and whether the free tier requires account review before it will send at all.
