# WhatsApp chat

Each user gets a private one-to-one chat with the QueryDesk bot on WhatsApp, the way they would
message a colleague. It is not a broadcast or a group: every user sees only their own thread, their
own answers and their own SR updates.

## What the user sees

- **A question** gets a grounded answer, with the cited articles listed under it.
- **A request** gets an SR number, or a question for the missing details. They answer in the next
  message, and the conversation carries on as it does on the chat page.
- **Status questions** ("status of my requests") list their open SRs.
- **SR updates** ("Your request SR 1042 (UAT extension) was approved…") arrive in the same chat.
- **Images and voice notes** get "I can only read text messages".

## Providers

`WHATSAPP_PROVIDER` picks one of two providers. Both are official WhatsApp APIs.

| | **Meta Cloud API** (`meta`, the default) | **Twilio** (`twilio`) |
| --- | --- | --- |
| Cost for the demo | A free test number that chats with up to 5 numbers you add. Since 1 Oct 2026 the WhatsApp account still needs a **card on file** before replies are delivered (see [Meta account checklist](#meta-account-checklist)) | A paid account; the free trial answers with its own auto-reply and never calls our webhook |
| Webhook signature | `X-Hub-Signature-256`, HMAC-SHA256 of the body under the app secret | `X-Twilio-Signature`, HMAC-SHA1 under the auth token of the URL + form fields |
| Code | `services/messaging/meta.py` | `services/messaging/twilio.py` |

Everything else is shared: `services/messaging/whatsapp.py` (identity, dispatch), `text.py` (reply
formatting) and `inbound.py` (the pipeline call).

## How it works

```
phone ──WhatsApp──> Meta / Twilio ──POST /webhooks/whatsapp──> api ──Celery──> worker ──> pipeline
                          ^                                      │ 200 at once             │
                          └──────── provider API <── whatsapp_send <──── reply text ───────┘
```

1. **The api verifies the provider's signature.** A bad signature gets 403.
2. **It drops a redelivered message** (`wa:msg:{id}` in Redis for a day) and queues it.
3. **It answers the provider at once.** The pipeline takes 5–8 s, longer than a webhook should be
   held open.
4. **The worker runs the same pipeline as `POST /chat`** (`services/messaging/inbound.py`). It
   then queues the reply on `querydesk.whatsapp_send`, which retries a provider failure with
   backoff. The message task itself never retries, because a second run could advance a
   conversation twice. A failure sends "Something went wrong on our side" instead.
5. **Status updates** go through the one notification write path (`notify.handle_status_change`).
   A new notification for a `wa-` user is also queued to their chat, so the `(sr_id, status)`
   dedupe stops double messages too.

**Privacy:** the pipeline knows a user as `wa-` plus 16 hex characters of
HMAC-SHA256(`WHATSAPP_ID_SECRET`, number). The phone number never reaches the ticketing
service, the logs or `decision_logs`. It is kept only in `whatsapp_users` (migration 004), so
updates can find the chat.

## Meta setup (free test number)

1. **Create the app:** at https://developers.facebook.com/apps, go to **Create App** and choose the
   WhatsApp use case ("Connect with customers through WhatsApp").
2. **Note the Phone number ID:** **WhatsApp → API Setup** shows the free test number and its
   **Phone number ID**.
3. **Add your phone:** under **To → Manage phone number list**, add each phone that should chat
   with the bot (up to 5), and confirm the code WhatsApp sends.
4. **Fill in `.env`:**

   | Variable | Where |
   | --- | --- |
   | `META_ACCESS_TOKEN` | A **System User token that never expires** (below). API Setup's temporary token is fine for a first try, but it can expire within a few hours, and then every reply fails with 401 code 190. |
   | `META_PHONE_NUMBER_ID` | API Setup. The ID, not the phone number. |
   | `META_APP_SECRET` | App settings → Basic. |
   | `META_VERIFY_TOKEN` | Any random string. You enter it again in step 6. |
   | `WHATSAPP_ID_SECRET` | A random value. |

5. **Start the stack and the tunnel:** `docker compose --profile stand-in --profile tunnel up -d`.
   It needs `NGROK_AUTHTOKEN` and `NGROK_DOMAIN`. The URL stays the same across runs.
6. **Set the webhook:** in **WhatsApp → Configuration → Webhook**, set the Callback URL to
   `https://<NGROK_DOMAIN>/webhooks/whatsapp` and the Verify token to `META_VERIFY_TOKEN`. Then
   **Verify and save**: Meta calls the api once with a GET, so the stack must be running. Under
   **Webhook fields**, subscribe to **messages**, then click **Test** on that row: a sample message
   should reach the worker (it answers as a `wa-` user; the reply goes to Meta's dummy number).
   **If the Test arrives but messages from your phone don't**, the app isn't subscribed to the
   WhatsApp account yet. Subscribe it once:
   `curl -X POST -H "Authorization: Bearer $META_ACCESS_TOKEN" https://graph.facebook.com/v23.0/<WABA_ID>/subscribed_apps`
   (the WABA id is on API Setup). `GET` on the same URL lists the subscribed apps; yours must be
   there.
7. **From your phone, message the test number:**
   - `does a rolling restart cause downtime?` (a cited answer);
   - `restart the claims app in UAT, it's hung` (an SR number);
   - move the SR along in the stand-in's Swagger (`http://localhost:8081/docs`) and get the update
     on WhatsApp;
   - `status of my requests` (the SR is listed).
8. **Check:** Neon and the logs show `wa-…` ids, never phone numbers.
9. **Stop the tunnel when you're done:** `docker compose stop tunnel`. While it runs, anyone with the
   address can reach the api; WhatsApp messages without a valid signature are still rejected.

### A token that never expires

In **Business Settings → Users → System users**:
1. Add a system user (role Admin).
2. **Assign assets**: the app and the WhatsApp account, both with full control.
3. **Generate token**: pick the app, expiry **Never**, and the permissions
   `whatsapp_business_messaging` and `whatsapp_business_management`.

`GET https://graph.facebook.com/v23.0/debug_token?input_token=<token>&access_token=<token>` should
show `"type": "SYSTEM_USER"` and `"expires_at": 0`.

### Meta account checklist

The app's **Permissions** page can stay as it is: *Ready for testing* is enough for your own
WhatsApp account, and App Review is only for apps that serve other businesses. A message can still
be accepted by the API (200) and then fail at delivery. Meta reports that on the webhook, and the
api logs it as `WhatsApp delivery … failed: <code>`. Before a phone test, check:

1. **Business profile complete**: legal name, country and website, in Business Settings → Business
   info. Missing fields block sending (error 131000 in the health check).
2. **A card on the WhatsApp account.** Since 1 October 2026 Meta charges for replies inside the 24-hour
   window beyond the free allowance, and an account with no payment method can't send (141006).
   - Add it in **Payment settings → Payment methods → Add payment method → Card**, using a credit
     card with international transactions enabled.
   - **Add funds** is prepaid *ad* credit, which WhatsApp can't use.
   - Indian banks often send no OTP when international use is off.
3. **Your phone on the test number's list**, verified with the code (otherwise 131030).

The health check shows what blocks sending, per phone number, WhatsApp account, business and app:

```bash
curl -H "Authorization: Bearer $META_ACCESS_TOKEN" \
  "https://graph.facebook.com/v23.0/<PHONE_NUMBER_ID>?fields=status,account_mode,health_status"
```

### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| No request reaches the tunnel when you message the number | The webhook isn't saved, `messages` isn't subscribed, or the app isn't subscribed to the WhatsApp account | Setup step 6 |
| Worker: `Meta rejected the message (401)`, code 190 | The access token expired | A System User token (above) |
| Worker: `(#131030) Recipient phone number not in allowed list` | Your number isn't on the test number's list, or isn't verified | Setup step 3 |
| api: `delivery … failed: 131031 Business Account locked` | The account is restricted: no payment method, an incomplete profile, or a pending review | The checklist above, then business.facebook.com/accountquality → *Request review*; Meta may ask for business verification |
| A 502 on one delivery right after a restart | Meta delivered while the api was starting | None: Meta retries and the retry gets 200 |

The live test on 2026-10-27–30 hit every row of this table. With all the health checks
AVAILABLE, replies still failed with 131031. That's where it stands: Meta has to lift the lock
(review or business verification).

## Twilio setup (paid account)

Set `WHATSAPP_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_WHATSAPP_FROM` (the Sandbox number, e.g. `whatsapp:+14155238886`) and
`WHATSAPP_PUBLIC_URL`. `WHATSAPP_PUBLIC_URL` must be the webhook URL **exactly** as entered in the
Twilio console, because Twilio's signature covers it. Set the Sandbox's "When a message comes in"
to that URL (POST), then join the Sandbox from your phone with its `join <code>` message.

## On EC2 (Day 10)

The ticketing team's Nginx proxies `/webhooks/whatsapp` (GET and POST) to the `api` service. Point
the provider's webhook at the EC2 address. For Twilio, also set `WHATSAPP_PUBLIC_URL` to it.

## Limits

- **The 24-hour window:** both providers deliver a free-form message only within 24 hours of the
  user's last message. An SR update after a quiet day is still in `notifications`, but doesn't
  reach WhatsApp: Meta reports error 131047 on the webhook (logged as a warning), and Twilio
  reports 63016. Production needs approved message templates for those updates.
- **Meta's test number** only reaches the numbers on its list, up to 5.
- **Pricing (Meta, from 1 October 2026):** replies inside the 24-hour window are no longer free
  beyond a monthly allowance per number, so production needs a payment method from day one.
- **Two log leaks to fix before production:**
  - The delivery-failure warning logs Meta's message id (`wamid…`), which is base64 and contains the
    recipient's number.
  - uvicorn's access log prints `META_VERIFY_TOKEN` in the GET query string.

  Both break the rule that phone numbers and secrets stay out of logs ([task.md](task.md)).
- **The Twilio Sandbox** only reaches numbers that sent its join code, and drops them after 72
  hours of silence.
