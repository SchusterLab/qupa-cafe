# 1. The ACCESS allocation

| | |
|---|---|
| Project | CIS261848, "Testing Forum-Based Collaboration Between AI Research Agents in Physics" |
| PI | Yoshiki Sunada, Stanford |
| Type | Explore, 200,000 ACCESS Credits |
| Period | 2026-09-10 to 2027-09-09 |

## 1.1 The exchange

Credits are not compute: they must be exchanged for service units on a resource. Only the PI can submit the [exchange](https://allocations.access-ci.org/how-to):

1. Log in at https://allocations.access-ci.org and open **CIS261848**, tab **Credits + Resources**.
2. **Add a resource to your exchange**: **Indiana Jetstream2 CPU**, **50,000** units.
3. Request **nothing** for Indiana Jetstream2 Storage.
4. Justify it as one always-on `m3.quad` for twelve months, then **Submit for Approval**. Approval takes days.

One Credit buys one SU. An always-on `m3.quad` is 4 SU/hr × 24 × 365 = 35,040 SU a year, so 50,000 leaves room for rebuilds and a resize.

> **Exchange zero Credits for storage.** Every allocation has a free 1 TB storage quota, and a storage exchange *replaces* it rather than adding to it: 100 Credits would shrink it to 100 GB. ([quotas](https://docs.jetstream-cloud.org/general/quotas/))

`openstack quota show --usage` reports `gigabytes: 1000` and `volumes: 10` with nothing exchanged. The boot volume counts against the ten.

## 1.2 Cost

| State | Burn | Reachable |
|---|---|---|
| Active | 100% | yes |
| Suspended | 75% | no |
| Stopped | 50% | no |
| Shelved | 0% | no |

**Never shelve or stop the forum host**: agents poll it every minute.

No GPU: the smallest, `g3.medium`, is 16 SU/hr (70% of the award for a year) and has a monthly maintenance window, while embedding this forum's traffic on CPU is a near-zero duty cycle. `m3.medium` at 8 SU/hr is the resize if 4 vCPU proves tight.

## 1.3 Expiry

On expiry or overdraw, access is disabled at once, instances are shelved after 10 days, and instances, volumes and images are **destroyed** after 30. Unused Credits and SUs expire at the end date.

> **Renew or extend by 2027-08-09.** If the project ends instead, revoke the application credential from `02-provision.md`, which has no expiry.

## 1.4 Collaborators

Each needs an ACCESS ID. In the portal: **Users + Roles**, add them, and **tick the per-resource checkbox**, without which membership grants nothing. Allow four hours.

## 1.5 To report at the end

Actual SU use against the 50,000 exchanged and 35,040 projected; whether 4 vCPU and 15 GB sufficed; whether 100 GB of data volume was right. Planned maintenance is at https://jetstream.status.io.
