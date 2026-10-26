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
| Cost for the demo | Free: a test number that chats with up to 5 numbers you add | A paid account; the free trial answers with its own auto-reply and never calls our webhook |
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
   | `META_ACCESS_TOKEN` | API Setup. The temporary token lasts 24 hours; for a longer demo, create a System User token in Business Settings. |
   | `META_PHONE_NUMBER_ID` | API Setup. The ID, not the phone number. |
   | `META_APP_SECRET` | App settings → Basic. |
   | `META_VERIFY_TOKEN` | Any random string. You enter it again in step 6. |
   | `WHATSAPP_ID_SECRET` | A random value. |

5. **Start the stack and the tunnel:** `docker compose --profile stand-in --profile tunnel up -d`.
   It needs `NGROK_AUTHTOKEN` and `NGROK_DOMAIN`. The URL stays the same across runs.
6. **Set the webhook:** in **WhatsApp → Configuration → Webhook**, set the Callback URL to
   `https://<NGROK_DOMAIN>/webhooks/whatsapp` and the Verify token to `META_VERIFY_TOKEN`. Then
   **Verify and save**: Meta calls the api once with a GET, so the stack must be running. Under
   **Webhook fields**, subscribe to **messages**.
7. **From your phone, message the test number:**
   - `does a rolling restart cause downtime?` (a cited answer);
   - `restart the claims app in UAT, it's hung` (an SR number);
   - move the SR along in the stand-in's Swagger (`http://localhost:8081/docs`) and get the update
     on WhatsApp;
   - `status of my requests` (the SR is listed).
8. **Check:** Neon and the logs show `wa-…` ids, never phone numbers.
9. **Stop the tunnel when you're done:** `docker compose stop tunnel`. While it runs, anyone with the
   address can reach the api; WhatsApp messages without a valid signature are still rejected.

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
- **The Twilio Sandbox** only reaches numbers that sent its join code, and drops them after 72
  hours of silence.
