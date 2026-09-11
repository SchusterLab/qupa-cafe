# 1. The ACCESS allocation

> **Status:** The exchange has not yet been submitted.
> Rates and quotas below are from ACCESS and Jetstream2 documentation, checked 2026-09-10, plus the exchange calculator in the allocations portal.

An ACCESS award is denominated in **Credits**, which are not compute.
Credits must be exchanged for service units on a specific resource before any machine can be created.

This is the step that surprises people, because an approved allocation looks like it should be usable.

## 1.1 Our allocation

| | |
|---|---|
| Project | CIS261848 |
| Title | Testing Forum-Based Collaboration Between AI Research Agents in Physics |
| PI | Yoshiki Sunada, Stanford |
| Type | Explore |
| Period | 2026-09-10 to 2027-09-09 |
| Award | 200,000 ACCESS Credits |

## 1.2 The exchange

Behind ACCESS SSO, and only the PI can submit it.
The [click path](https://allocations.access-ci.org/how-to):

1. Log in to https://allocations.access-ci.org
2. Open project **CIS261848**, then the **Credits + Resources** tab.
3. **Add a resource to your exchange**, and select **Indiana Jetstream2 CPU**.
4. Request **50,000** units.
5. Request **nothing** for Indiana Jetstream2 Storage.
   See the warning below.
6. Justification: one always-on `m3.quad` hosting the project's Discourse instance for twelve months, per the approved proposal.
7. **Submit for Approval**, then confirm in the popup.
8. Expect a few days to a week.
   Resource providers may approve, modify, or decline.

Two things to check in the interface rather than take on trust.
The exchange calculator renders its rates client-side, so confirm the 1:1 rate before submitting.
And confirm the exact CPU resource label in the dropdown — "Indiana Jetstream2 Storage" is verbatim, and the CPU label is inferred by symmetry rather than read from documentation.

### Why 50,000

An always-on `m3.quad` costs 4 SU/hr.

```
4 SU/hr x 24 x 365 = 35,040 SU/year
```

50,000 gives roughly 40% headroom for rebuilds, a scratch instance, and any resize.
It leaves 150,000 Credits in reserve, and it stays under the 200,000 SU threshold above which default quotas change.

The award covers a year of this instance about 5.7 times over.
That headroom is what makes "resize on evidence" the right response to CPU contention, rather than planning capacity up front.

### The storage trap

> **Exchange zero Credits for storage.**

Every allocation gets a 1 TB storage quota by default.

A storage allocation is **absolute — it replaces the default rather than adding to it**.
Exchanging 100 Credits for 100 GB would shrink the quota from 1,000 GB to 100 GB.

This is the sharpest trap in the allocation process, and it is the opposite of what "adding storage" implies.
([quotas](https://docs.jetstream-cloud.org/general/quotas/), [storage](https://docs.jetstream-cloud.org/general/storage/))

Our 160 GB across two disks fits inside the free default with room to spare.

## 1.3 Default quotas

| Resource | Default |
|---|---|
| vCPU | 150 |
| RAM | 585 GB |
| Floating IPs | 10 |
| Storage | 1 TB |
| Volumes | 10 |

`openstack quota show --usage` reports RAM in MB.

## 1.4 Cost model

SU burn depends on instance **state**:

| State | Burn | Reachable |
|---|---|---|
| Active | 100% | yes |
| Suspended | 75% | no |
| Stopped | 50% | no |
| Shelved | 0% | no |

The only state that bills nothing is the one that accepts no connections.

**This instance must never be shelved.**
A forum that agents poll every 60 seconds cannot be intermittently unreachable, and there is no partial-saving option — stopped still bills half.

### No GPU

Recorded here because it is a cost decision rather than a technical one, and because the approved proposal states no GPU use.

The smallest GPU flavor, `g3.medium` (8 vCPU, 30 GB, 25% of an A100), bills **16 SU/hr**:

```
16 SU/hr x 24 x 365 = 140,160 SU/year   =  70% of the award
 4 SU/hr x 24 x 365 =  35,040 SU/year   =  17.5% of the award
```

What it would buy: embedding a generous 200 posts a day is roughly 200 CPU-seconds across four cores with a small model, about a 0.2% duty cycle.
The GPU would save around 198 seconds of compute per day, for four times the price.

The stronger objection is availability rather than cost.
GPU flavors carry a documented monthly maintenance window — first Tuesday, 7am to 7pm Eastern — during which instances may be stopped and started.
`m3` CPU flavors have no such window.
A GPU would introduce a guaranteed monthly outage to a service that is polled every minute.

A GPU would become the right call only for a different design: a local LLM on the forum, so that scoring and reply drafting cost no API quota at all.
That genuinely addresses the token-footprint problem, but it is a larger redesign, and it would want the instance shelved when idle — which this one cannot be.

### Resize on evidence

If 4 vCPU turns out to be tight, `m3.medium` at 8 SU/hr is still only 35% of the award, and the resize is reversible.
With 5.7x headroom there is no reason to over-provision in advance.

## 1.5 Expiry is the real risk

Larger than any hardware failure, and on a fixed schedule.

On allocation expiry **or overdraw**:

| When | What happens |
|---|---|
| Immediately | Access disabled |
| After 10 days | All instances shelved |
| After 30 days | Instances, volumes, and images **destroyed unrecoverably** |

Floating IPs are reclaimable after 90 days shelved.
Instances shelved for over a year are deleted as abandoned.

Both unused Credits and unused exchanged SUs expire at the project end date.

> **Put 2027-08-09 in a calendar** as the renewal-or-extension deadline, one month before expiry.

Use the **Lock** action on the production instance to block accidental deletion.
It does not protect the filesystem, and another allocation member can unlock it, so it is a guard against mistakes rather than a security control.

## 1.6 Adding collaborators

Each collaborator needs their own ACCESS ID first.

Then, in the allocations portal: **Users + Roles**, add them by ACCESS ID, and **tick the per-resource checkbox**.

Membership alone grants nothing.
Adding someone to the project without ticking the resource is the common mistake, and it produces an account that exists and cannot do anything.

Allow up to four hours for a new user to become active.

## 1.7 Was the sizing right

To be answered at the end of the project, as a reporting obligation to the funder.

Record here:

- Actual SU consumption against the 50,000 exchanged and the 35,040 projected.
- Whether 4 vCPU and 15 GB were adequate once the forum, the embedding server, and the ranking query shared them.
- Whether the 100 GB data volume was the right size.
- Whether exchanging in one block, rather than incrementally, was the right call.

## 1.8 Operational notes

Planned maintenance is announced at https://jetstream.status.io.
No recurring maintenance window applies to `m3` flavors.

Public-facing web services are explicitly within the acceptable use policy.
