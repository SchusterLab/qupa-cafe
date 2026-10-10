# Providers

How this deployment meets the requirements in `docs/01-requirements.md` for services other than the host. `platforms/` is the sibling for hosts.

| Directory | What it is | What it satisfies |
|---|---|---|
| [`brevo/`](brevo/) | The transactional mail relay | The SMTP relay |
| [`porkbun/`](porkbun/) | The registrar and DNS host | The domain and DNS |
| [`slack/`](slack/) | A bridge to a chat workspace | Nothing required; optional |

The relay and the registrar are coupled: the relay authenticates the sending subdomain through records in the registrar's zone, and that subdomain is the forum's hostname.

Any relay will do that authenticates a sending subdomain and offers SMTP on port 587. Check its real quota, and whether its free tier holds sending pending review.
