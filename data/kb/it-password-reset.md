---
title: Password reset and account unlock
category_id: it_password_reset
source_url: https://kb.internal/it/password-reset
---

# Password reset and account unlock

Most password problems are resolved in under two minutes through the self-service portal at
https://passwordreset.internal. IT only needs to get involved when the portal itself cannot
complete the reset.

## Self-service reset

Any employee or registered agent can reset their own password at the portal, provided they have
previously enrolled a recovery method. You will need your employee code or agent code and access to
one of your enrolled factors: the authenticator app, your registered mobile number, or your
alternate email address.

The portal walks through four steps: identify yourself, verify with a factor, choose a new password,
confirm. The new password takes effect immediately for most systems. The CRM and the policy
administration system both cache credentials for up to 15 minutes, so a sign-in attempt there may
keep failing briefly after a successful reset. Wait 15 minutes before reporting a problem.

## Password rules

Passwords must be at least 12 characters and include an uppercase letter, a lowercase letter, and a
digit. The last five passwords cannot be reused. Passwords expire every 90 days; you receive a
notice 14 days before expiry and daily for the final three days. An expired password still lets you
sign in to the portal itself so you can change it — you are not locked out by expiry alone.

## Account lockout

Five consecutive failed sign-in attempts lock the account. Lockouts clear automatically after 30
minutes, so in most cases waiting is faster than raising a request. The lockout counter resets on
any successful sign-in.

An account locked three or more times in a single day is flagged for review and will not clear
automatically. This is a security control, not a fault, and it requires IT to release the account.

## Common portal errors

| Code | Meaning | What to do |
| --- | --- | --- |
| PWD-1401 | No recovery method enrolled | IT must reset manually; the portal cannot verify you |
| PWD-1402 | Recovery mobile number no longer in service | Update your number through HR first, then retry |
| PWD-1403 | Account flagged after repeated lockouts | IT must release the account |
| PWD-1407 | New password matched one of your last five | Choose a different password |
| PWD-1410 | Portal cannot reach the directory service | A platform incident; check the status page |

## When IT has to do it

Raise a password reset request only when the self-service portal cannot complete the reset — in
practice that means errors PWD-1401, PWD-1402 or PWD-1403, or when the portal is itself down.
A manual reset is handled within one business hour during 9 AM to 7 PM, and the temporary password
is sent to your manager rather than to you, because your own channels are what could not be
verified.

Contractors whose accounts have passed their contract end date cannot be reset at all. That needs a
contract extension from the hiring manager first; a reset on an expired contract will be rejected.
