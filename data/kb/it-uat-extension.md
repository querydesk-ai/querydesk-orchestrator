---
title: UAT environment hours and extensions
category_id: it_uat_extension
source_url: https://kb.internal/it/uat-extension
---

# UAT environment hours and extensions

All UAT environments shut down automatically at 9:00 PM IST and start again at 7:00 AM IST the next
working day. The shutdown exists to control cloud spend, which is why extensions are approved
individually rather than granted standing.

## The environments

Six UAT environments are available: UAT-1 through UAT-4 for the policy administration system, UAT-5
for the CRM, and UAT-6 for the agent portal. UAT-3 is reserved for release-candidate testing during
a release week and cannot be booked for feature testing then.

## What the shutdown does

At 9:00 PM the application tiers stop and the compute instances are deallocated. Databases are left
running, so data in UAT is not lost at shutdown — a common misconception. Anything held only in
application memory, including in-flight test sessions and unsaved test-data builders, is lost.
Long-running batch jobs are terminated mid-run and must be restarted the next morning.

## Requesting an extension

An extension requires three details: which environment, how late it should stay up, and why. The
reason matters because it determines whether the request is approved, not just logged.

Extensions need infra lead approval and are time-bound. The latest an environment can be extended
to on a normal working day is 2:00 AM. Anything past 2:00 AM is treated as an overnight run and
needs the platform manager instead of the infra lead, because it overlaps the nightly backup
window.

Approval usually lands within 30 minutes during working hours. An extension requested after 8:30 PM
may not be approved before the automatic shutdown, so raise it earlier in the day when you can.

## Auto-revert

An approved extension reverts automatically when its window ends — the environment shuts down at
the approved time without anyone doing anything, and the request moves to a reverted state. You do
not need to raise a second request to put the schedule back, and you should not: the original
schedule returns by itself the following day.

If you need the extension again the next day, raise a new request. Extensions do not repeat.

## Release weeks

During a release week, UAT-3 is extended to 2:00 AM for all five nights by a standing arrangement,
so individual requests for UAT-3 are unnecessary in that window. The release calendar is published
at the start of each quarter.

## Common reasons a request is rejected

An extension is normally rejected when the reason given is just "testing" with no detail, when the
environment named is already extended that night, when the end time is past 2:00 AM without
platform manager involvement, or when the request arrives during a change freeze. Change freezes
cover the last two business days of each quarter.
