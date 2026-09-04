# Stripe to WordPress Membership Sync

A webhook service that syncs Stripe subscription events to membership levels on a WordPress site running Paid Memberships Pro (PMPro).

Payments happen in Stripe. Access is gated by PMPro. This keeps the two in sync: a new subscription grants a level, a cancellation removes it, a plan change swaps it.

Built with FastAPI, the Stripe SDK, and httpx.

## How it works

1. Stripe sends a signed webhook to `POST /webhooks/stripe`.
2. The service verifies the signature. Unsigned or tampered requests get a 400.
3. The event ID is checked against a ledger table. Already-processed events return the previous result. Stripe delivers at least once.
4. The event is normalized into a customer email, a price ID, and an action.
5. The price ID is mapped to a PMPro level via `config.yaml`.
6. The service finds or creates the WordPress user and sets their level.
7. The outcome is written to the ledger, including any error.

WordPress calls retry with exponential backoff and jitter.

## Requirements

- Python 3.11 or newer
- A Stripe account and the signing secret for your webhook endpoint
- A WordPress site with [Paid Memberships Pro](https://www.paidmembershipspro.com/) and the [PMPro REST API add-on](https://www.paidmembershipspro.com/add-ons/pmpro-rest-api/)
- A WordPress Application Password, from Users, then your profile

## Setup

```bash
git clone https://github.com/yourusername/stripe-membership-sync.git
cd stripe-membership-sync
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows: `venv\Scripts\activate`.

Fill in `.env`:

```env
STRIPE_WEBHOOK_SECRET=whsec_your_signing_secret
WORDPRESS_URL=https://yoursite.com
WORDPRESS_USERNAME=your_wp_admin_user
WORDPRESS_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
PMPRO_DEFAULT_LEVEL_ID=1
API_KEY=
```

`API_KEY` guards the admin endpoints. It has no default and must be at least 16 characters:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Mapping prices to levels

```yaml
membership_map:
  - stripe_price_id: "price_monthly_10"
    pmpro_level_id: 1
    level_name: "Basic"
    duration: "month"

  - stripe_price_id: "price_annual_100"
    pmpro_level_id: 2
    level_name: "Pro"
    duration: "year"

default_level_id: 1
```

Unmapped price IDs fall through to `default_level_id`. Check the ledger after adding a Stripe product to confirm it mapped correctly.

## Running it

```bash
uvicorn app.main:app --reload --port 8000
```

Forward real Stripe events with the CLI in a second terminal:

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

## Tests

```bash
pytest
```

23 tests covering event parsing, the sync engine, and the webhook endpoint. The WordPress client is mocked.

## Events handled

| Stripe event | Action |
|---|---|
| `checkout.session.completed` | Find or create the user, assign the mapped level |
| `customer.subscription.updated` | Reassign the level. `cancel_at_period_end` is treated as a cancellation |
| `customer.subscription.deleted` | Set level to 0 |
| `invoice.payment_failed` | Record only. Stripe retries and sends a deletion event if it gives up |
| `customer.updated` | Record only. No price on these events, so no level change |

Unhandled event types return 200 and are skipped.

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/webhooks/stripe` | Stripe signature | Webhook receiver |
| `GET` | `/health` | none | Status, last event time, processed count |
| `GET` | `/events` | `X-API-Key` | Recent events, newest first |
| `GET` | `/events/{event_id}` | `X-API-Key` | One event by ID |
| `POST` | `/sync/manual` | `X-API-Key` | Grant a level to an email by hand |

`/sync/manual` covers missed webhooks and members added outside Stripe.

## Deployment

```bash
docker build -t stripe-membership-sync .
docker run -p 8000:8000 --env-file .env stripe-membership-sync
```

Or `docker-compose up -d`.

Before live traffic:

- Set `ENVIRONMENT=production` and a real `API_KEY`
- Switch `DATABASE_URL` to PostgreSQL. SQLite writes to a local file that does not survive a container restart unless mounted.
- Terminate HTTPS in front of the service. Stripe will not deliver to plain HTTP.
- Register the endpoint in the Stripe Dashboard and subscribe to the five events above
- Replay events with the Stripe CLI against staging
- Monitor `/health`

## Security notes

- Signatures are verified with `stripe.Webhook.construct_event`, which enforces a timestamp tolerance. Captured payloads will not replay.
- The admin API key is compared with `secrets.compare_digest`, not `==`.
- Emails are masked in logs as `s***@example.com`. Full addresses live in the ledger.
- WordPress error bodies stay on the exception and out of the logs. `/sync/manual` returns a generic failure message.
- The ledger holds subscriber emails and Stripe customer IDs. `.gitignore` excludes `*.db` and `data/`.

## Known limitations

- The retry wrapper retries on any exception, including 4xx responses that will never succeed. Narrow `retryable_exceptions` to timeouts and 5xx.
- Failed events get a `retry_count` but nothing re-drives them. Use `/sync/manual` or resend from the Stripe Dashboard.
- `checkout.session.completed` carries line items only when the session was created with them expanded. Otherwise the price falls back to `default_level_id`.
- No dedicated test file for the WordPress client. It is mocked in the sync engine tests.

## Layout

```
app/
  main.py                     FastAPI app, routes, dependencies
  config.py                   Env settings and the YAML level mapping
  models/
    events.py                 Event ledger table and DB setup
    schemas.py                Request and response models
  services/
    stripe_handler.py         Signature check and event normalization
    wordpress_client.py       WordPress and PMPro REST calls
    sync_engine.py            Orchestration, idempotency, error recording
  utils/
    logging.py                Structured logging, email masking
    retry.py                  Backoff decorator
tests/
  conftest.py                 Fixtures, mocked WordPress client
  test_stripe_handler.py
  test_sync_engine.py
  test_webhook.py
config.yaml                   Price to level mapping
.env.example
Dockerfile
docker-compose.yml
pytest.ini
requirements.txt
```

## License

MIT
