---
name: webapp-state
description: Living state note for the demo acme-webapp project (rewritten each reflect).
metadata:
  type: project
date: 2026-01-15
---
## Task
Ship the acme-webapp checkout-rewrite so guest checkout works without an
account. (Demo content — this file shows the living per-project state-note
format the reflect pass rewrites wholesale each pass.)

## State
- Guest-checkout API endpoint merged and deployed to staging — commit `a1b2c3d`.
- Payment webhook ret/ry logic verified end-to-end against the sandbox gateway.
- Load test at 2x peak traffic passed (p99 latency 180ms) — verdict from
  session `demo-0007`.

## Decisions
- Store guest carts in Redis with a 24h TTL rather than in Postgres — avoids a
  schema migration and the carts are disposable.
- Defer the saved-payment-method feature to after launch.

## Next actions
- Wire the guest-checkout endpoint into the production frontend behind a flag.
- Add a Grafana panel for guest-vs-account conversion.

## Pointers
- API handler: `services/checkout/guest.py:42`
- Redis cart schema: `services/checkout/cart.py:88`
- Staging deploy: commit `a1b2c3d`
