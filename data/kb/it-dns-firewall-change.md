---
title: DNS and firewall change requests
category_id: it_dns_firewall_change
source_url: https://kb.internal/it/dns-firewall-change
---

# DNS and firewall change requests

Network changes are executed by the network team and always require approval. They are the most
common cause of self-inflicted outages, which is why the process is deliberately strict.

## What counts as a network change

DNS record changes — adding, updating or removing an A, CNAME, MX or TXT record. Firewall rule
changes — opening or closing a port, whitelisting a source IP or CIDR range, or changing an existing
rule's scope. Load balancer routing changes are handled by the infra team instead and are a
different request type.

## Required details

Every request needs the change type, the target, the environment, and the reason. The target is
whichever of these applies: the DNS record name, the port number, or the IP address or CIDR range.

Vague targets are the main reason requests bounce. "Open the firewall for the vendor" cannot be
actioned; "allow TCP 8443 inbound from 203.0.113.0/24 to pas-web in UAT" can.

## Approval and timelines

All network changes need approval from the network lead. Production changes additionally need a
security review, which adds one business day.

| Change | Environment | Typical turnaround |
| --- | --- | --- |
| DNS record add or update | UAT | Same business day |
| DNS record add or update | Production | 1-2 business days |
| Firewall port open | UAT | 1 business day |
| Firewall port open | Production | 2-3 business days, after security review |
| IP whitelist for a known vendor | Any | 1 business day |

## DNS propagation

Internal DNS records have a TTL of 300 seconds, so a change is visible within five minutes.
External records use a TTL of 3600 seconds and can take up to an hour. A change that appears not to
have worked has usually just not propagated — check again after the TTL before raising an incident.

The most frequent misdiagnosis here is a client that caches DNS for its own lifetime. JVM services
cache DNS indefinitely by default, so a pointed-elsewhere hostname needs the service restarted
before it follows the new record.

## What is never approved

Opening a port to 0.0.0.0/0 in production is refused without exception. Inbound SSH from outside
the corporate network is refused; use the bastion host. Whitelisting a residential or dynamic IP
address is refused because it does not stay stable — use the VPN instead.

Disabling a firewall rule temporarily "to test" is also refused. Testing is done by adding a scoped
rule, not by removing protection.

## Error codes you may see

FW-2201 means the rule conflicts with an existing deny rule that takes precedence. FW-2204 means
the CIDR range overlaps a reserved internal range. DNS-3310 means the record already exists with a
different value and needs an explicit update rather than an add.
