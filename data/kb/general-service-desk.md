---
title: Service desk hours, SLAs and escalation
category_id: general
source_url: https://kb.internal/general/service-desk
---

# Service desk hours, SLAs and escalation

This article covers how requests are handled once raised: when the desk is staffed, what response
times to expect, and how to escalate. It applies to every request category.

## Hours

The service desk is staffed 9:00 AM to 7:00 PM IST, Monday to Saturday. Sunday and public holidays
are covered by an on-call rota for Sev-1 incidents only.

Requests can be raised at any time. One raised outside hours is queued and picked up at the start of
the next working period; its SLA clock starts then, not at submission.

## Severity and response times

| Severity | Definition | First response | Target resolution |
| --- | --- | --- | --- |
| Sev-1 | Production down or a regulatory deadline at risk | 15 minutes | 4 hours |
| Sev-2 | Major function degraded, a workaround exists | 1 hour | 1 business day |
| Sev-3 | Single user blocked, or a non-urgent change | 4 hours | 3 business days |
| Sev-4 | Question, or a cosmetic issue | 1 business day | 5 business days |

Severity is assigned by the desk, not by the requester. A request marked urgent without a
corresponding business impact is reassessed, because inflated severity across many requests makes
the queue useless for the genuinely urgent ones.

## Approvals

A request needing approval does not begin its resolution clock until approved. Approvers are
notified immediately and reminded after four working hours. An approval outstanding for two
business days is escalated to the approver's manager automatically.

This is worth knowing because a request can look stalled when it is simply waiting on an approver,
and chasing the service desk will not move it. The requester can see who the approval is sitting
with.

## Status values

Requests move through: pending, approved or rejected, in progress, resolved, and in some cases
reverted. Reverted applies where a change was time-bound and its window has ended, such as a UAT
extension.

You are notified at every status change — there is no need to poll. A rejection always carries a
reason.

## Escalation

Escalate when a response SLA has been missed, when a Sev-1 has not had an update in 30 minutes, or
when a resolution is wrong and reopening has not worked.

Escalation goes to the service desk lead first, then the IT operations manager for IT categories or
the operations head for insurance categories. Escalating straight past the desk lead slows things
down in practice, because the lead holds the context.

## Reopening

A resolved request can be reopened within five business days if the problem recurs, and it keeps its
original number and severity. After five days it closes permanently and a recurrence needs a new
request, which is deliberate: a long-closed request is no longer a reliable record of current state.

## What the desk will not do

The desk does not act on requests on behalf of someone else without their manager's confirmation,
does not accept credentials or payment details in a request description, and does not bypass
approval even for a Sev-1 outside the incident process. Credentials sent in a request body cause the
request to be closed and the credential rotated.
