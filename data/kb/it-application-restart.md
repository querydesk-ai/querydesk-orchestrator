---
title: Application restart policy
category_id: it_application_restart
source_url: https://kb.internal/it/application-restart
---

# Application restart policy

Restarts are performed by the infra team on request. Application teams have restart rights in UAT
for their own services only, and no restart rights in production.

## What a restart does

A rolling restart takes nodes out of the pool one at a time, stops the service, starts it, waits
for the health check to pass, and returns the node to the pool before moving to the next. For a
clustered service this means no downtime, and a four-node cluster takes roughly eight minutes.

A full restart stops every node at once. It is faster — under two minutes — but the service is
unavailable throughout. Full restarts are only used when a rolling restart cannot work, such as
when a shared cache schema has changed and mixed versions would corrupt it.

Rolling is the default. You do not need to ask for it by name.

## When a restart is the right fix

Restarts genuinely resolve a hung thread pool, a memory leak that has not yet caused an out-of-memory
condition, and a stale configuration cache after a config change that is not hot-reloaded. They
clear the symptom, not the cause, so a service needing repeated restarts should have a problem
ticket raised alongside.

Restarts do not fix a failing downstream dependency, a database deadlock, bad data, or a code
defect. In those cases a restart buys a few minutes and the problem returns, which wastes the
restart window.

## Required details

Name the application and the environment. Without both the request cannot be actioned, because
service names are reused across environments.

## Approval

Restarts in UAT and pre-production need no approval and are usually done within 30 minutes.

Production restarts always need approval from the service owner. During business hours, 9 AM to
7 PM, this is because a restart during peak policy issuance can drop in-flight quotes. Outside
business hours the approval is faster but still required.

A production restart during an active Sev-1 incident does not need approval — the incident commander
can authorise it directly, and it is logged afterward.

## Memory settings

Java services run with -Xmx512m in UAT and -Xmx2g in production. A service restarting repeatedly in
UAT with an out-of-memory error is very often hitting the lower UAT heap rather than carrying a real
leak, and raising the UAT heap is a config change, not a restart.

## Known restart-sensitive services

The document rendering service holds a render queue in memory. Restarting it discards queued renders
and the submitting user has to resubmit; queue depth should be checked first. The commission
calculation batch must not be restarted while a payout run is in progress, because a partially
completed run has to be reversed manually before it can be re-run.
