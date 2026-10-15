---
title: Agent onboarding and licensing
category_id: ops_agent_onboarding_licensing
source_url: https://kb.internal/ops/agent-onboarding
---

# Agent onboarding and licensing

A new agent cannot sell until they hold a valid licence and have been issued an agent code. The two
are separate steps and the licence must come first.

## Licensing

Agents are licensed by the regulator, not by us. The licence requires 15 hours of training from an
accredited institute followed by the agent examination. We sponsor the training for agents
onboarding under a branch, and the agent pays if onboarding independently.

The licence number is in the form IRDAI-AG-XXXXXXX and is valid for three years from the date of
issue. Composite agents, who sell both life and general products, hold two licence numbers and both
must be recorded.

## Onboarding steps

Onboarding needs the agent's full legal name, the licence number, and the region they will operate
in. The region determines the branch mapping, the reporting manager, and which lead pool they draw
from in the CRM, so it cannot be left blank or corrected informally later.

Alongside those, operations collects PAN, Aadhaar, bank account details for commission credit, a
cancelled cheque, the training completion certificate, and the examination pass certificate. The
bank account must be in the agent's own name — commission cannot be credited to a third party,
including a spouse or a firm.

Onboarding needs approval from the regional head because it creates a commission-bearing
relationship.

## Timelines

Once documents are complete, an agent code is issued within three business days. Portal access
follows within one further business day, and CRM lead access within two, because the territory
mapping runs on a nightly job.

An agent who has a code but no CRM access on day three after code issue has usually had the nightly
mapping job skip them for a missing region, which is a separate fix.

## Renewals

A licence must be renewed before expiry with 25 hours of refresher training. We notify the agent 90,
60 and 30 days before expiry. An expired licence suspends the agent code immediately: in-force
policies continue and renewal commission continues to be paid, but no new business can be written
and CRM lead access is withdrawn until the renewal is recorded.

Reinstating a suspended code after renewal takes one business day and does not need a fresh
onboarding.

## Common rejections

Onboarding is rejected when the licence number does not validate against the regulator's register,
when the name on the licence does not match the name on PAN, when the bank account is not in the
agent's name, or when the agent already holds an active code with another insurer in a category that
does not permit multiple appointments. A name mismatch is the most common and is usually a
middle-name or initial difference; it needs a correction at source rather than an override here.
