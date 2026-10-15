---
title: CRM lead access and territory mapping
category_id: ops_crm_lead_access
source_url: https://kb.internal/ops/crm-lead-access
---

# CRM lead access and territory mapping

Leads appear in an agent's CRM queue based on territory mapping. Almost every "I cannot see my
leads" report traces back to the mapping rather than to permissions.

## How leads are assigned

Inbound leads carry a pin code. A nightly job maps each pin code to a territory, each territory to a
branch, and each branch to its mapped agents. An agent sees a lead if their territory matches and
they are below their open-lead cap.

The job runs at 1:00 AM. A lead created during the day is visible the next morning, not immediately.
A newly mapped agent therefore has an empty queue on their first day even when everything is
configured correctly, which is expected rather than a fault.

## The open-lead cap

Each agent can hold 40 open leads. At the cap, no new leads are assigned until some are worked —
moved to contacted, qualified, converted or closed. This is the second most common cause of an
apparently empty or stale queue: the queue is not empty, it is full and static.

The cap is 60 for senior agents and 25 for agents in their first 30 days.

## Lead ageing

An untouched lead returns to the pool after 72 hours and is reassigned to another agent in the same
territory. Leads touched at least once stay with the agent for 30 days. An agent who reports leads
disappearing is usually seeing the 72-hour rule work as designed.

## What to check before raising a request

Confirm the agent code is active and the licence is not expired — an expired licence withdraws lead
access entirely. Check the agent's open-lead count against their cap. Check the territory recorded
against the agent matches where they actually operate. Confirm at least one business day has passed
since onboarding so the nightly job has run.

## Required details

A request needs the agent code and a description of the issue. Be specific about which of these it
is: no leads at all, leads for the wrong territory, leads visible to the wrong agent, or a specific
named lead that cannot be opened.

## When it is a mapping problem

If the territory recorded against the agent is wrong, operations corrects the mapping and the change
takes effect at the next nightly run. This does not need a request to be raised as a service
request — it is a data correction and is handled directly.

If the territory mapping is already correct and leads still are not arriving, that is a genuine
system issue and does need a request, because it means the assignment job is failing for that
agent. Supply the agent code and the date from which leads stopped.

## Error codes

CRM-5501 means the agent has no territory mapped. CRM-5503 means the agent is at their open-lead
cap. CRM-5507 means the lead is locked by another agent's session and will release within 15
minutes. CRM-5512 means the agent code is suspended, which points at the licence rather than
the CRM.
