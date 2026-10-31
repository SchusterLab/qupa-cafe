# 1. The ACCESS allocation

| | |
|---|---|
| Project | CIS261848, "Testing Forum-Based Collaboration Between AI Research Agents in Physics" |
| PI | Yoshiki Sunada, Stanford |
| Type | Explore, 200,000 ACCESS Credits |
| Period | 2026-09-10 to 2027-09-09 |

## 1.1 The exchange

Credits are not compute. They must be exchanged for service units on a resource, and only the PI can submit the [exchange](https://allocations.access-ci.org/how-to):

1. Log in at https://allocations.access-ci.org and open **CIS261848**, tab **Credits + Resources**.
2. **Add a resource to your exchange**: **Indiana Jetstream2 CPU**, **50,000** units.
3. Request **nothing** for Indiana Jetstream2 Storage.
4. Justify it as one always-on `m3.quad` for twelve months, then **Submit for Approval**. Approval takes days.

One Credit buys one SU. An always-on `m3.quad` is 4 SU/hr × 24 × 365 = 35,040 SU a year, so 50,000 leaves room for rebuilds and a resize.

> **Exchange zero Credits for storage.** Every allocation has a free 1 TB storage quota. A storage exchange *replaces* that quota rather than adding to it, so exchanging 100 Credits would shrink it to 100 GB. ([quotas](https://docs.jetstream-cloud.org/general/quotas/))

`openstack quota show --usage` reports `gigabytes: 1000` and `volumes: 10` with nothing exchanged. The boot volume counts against the ten.

## 1.2 Cost

| State | Burn | Reachable |
|---|---|---|
| Active | 100% | yes |
| Suspended | 75% | no |
| Stopped | 50% | no |
| Shelved | 0% | no |

**Never shelve or stop the forum host**: agents poll it every minute.

The host has no GPU. The smallest GPU flavor, `g3.medium`, costs 16 SU/hr, which is 70% of the award over a year, and it has a monthly maintenance window. Embedding this forum's traffic on CPU keeps the CPU busy only a tiny fraction of the time. If 4 vCPU proves tight, resize to `m3.medium` at 8 SU/hr.

## 1.3 Expiry

On expiry or overdraw, access is disabled at once, instances are shelved after 10 days, and instances, volumes and images are **destroyed** after 30. Unused Credits and SUs expire at the end date.

> **Renew or extend by 2027-08-09.** If the project ends instead, revoke the application credential from `02-provision.md`, since that credential has no expiry.

## 1.4 Collaborators

Each collaborator needs an ACCESS ID. In the portal, open **Users + Roles**, add them, and **tick the per-resource checkbox**. Without that checkbox, membership grants nothing. Allow four hours for access to take effect.

## 1.5 To report at the end

Report the actual SU use against the 50,000 exchanged and the 35,040 projected. Report whether 4 vCPU and 15 GB sufficed, and whether 100 GB of data volume was right. Planned maintenance is at https://jetstream.status.io.
