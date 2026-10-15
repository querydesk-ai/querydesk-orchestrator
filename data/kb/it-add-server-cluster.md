---
title: Adding a server to an application cluster
category_id: it_add_server_cluster
source_url: https://kb.internal/it/add-server-cluster
---

# Adding a server to an application cluster

Capacity is added to a cluster by provisioning a node and joining it to the load-balanced pool.
This is always an infra-executed change: application teams cannot add nodes themselves in any
environment, including UAT.

## What you need to supply

Three details are mandatory: the target cluster, the environment, and the reason. The reason should
state what capacity problem is being solved — sustained CPU above 70%, a planned campaign, a known
traffic event — because the infra team sizes the node from it. "Need more capacity" without a
driver gets queried rather than actioned.

## Clusters and their limits

| Cluster | Purpose | Max nodes |
| --- | --- | --- |
| pas-web | Policy administration web tier | 12 |
| pas-batch | Policy administration batch workers | 8 |
| crm-web | CRM web tier | 6 |
| agent-portal | Agent portal | 8 |
| doc-render | Document rendering and PDF generation | 4 |

A cluster already at its maximum needs an architecture review before it can grow further, which is
a separate process and typically takes two weeks. The doc-render cluster reaches its limit most
often, usually during the March renewal peak.

## Timelines

In UAT, a node is provisioned and joined within one business day. In production, the change needs a
change-advisory-board slot: the board meets Tuesday and Thursday, and a request must be submitted
by 2:00 PM the previous day to make the agenda. A production node added outside a CAB slot requires
an emergency change, which needs the platform manager and a post-incident review.

Approval is always required, regardless of environment.

## Warm-up

A new node joins the pool with a 10-minute warm-up during which it receives reduced traffic. JVM
services need this to reach steady-state performance, and routing full traffic at a cold node is
the most common cause of latency spikes right after a scale-out. Do not judge the effect of a
scale-out until 15 minutes after the node joins.

## What this request is not for

Replacing a failed node is handled by the infra team automatically and does not need a request —
failed nodes are detected by health checks and rebuilt. Resizing an existing node, as opposed to
adding one, is a different request type and needs a maintenance window because the node restarts.
Adding capacity to the database tier is also out of scope here; databases are scaled by the data
platform team.
