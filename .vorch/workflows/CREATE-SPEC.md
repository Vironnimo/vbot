# Spec File Format

A spec file documents everything an agent needs to know before working on a domain. It is not architecture documentation — it is working notes. Keep it short and factual.

Spec files live in `.vorch/specs/<domain>.md`. The Orchestrator creates and maintains them; all other agents read them.

**Remove every section that doesn't apply.** A spec for a small utility module might only have Overview and Interfaces. A spec for a payments module might have all sections. There is no required minimum.

## Template

```markdown
# <Domain Name>

<One sentence: what this module does and where it sits in the system.>

## Overview

[What this module is responsible for. What it owns. What it does NOT do (if non-obvious).
Keep it to 3–5 sentences. This is the "why should I care" before touching this code.]

## Data Model

[Only include if this domain owns entities or persists data.
Key entities, their shapes, important fields, relationships to other domains.
Pseudocode or TypeScript-style type annotations are fine — exact syntax doesn't matter.]

## Interfaces

[The contracts other parts of the system depend on: exported functions, classes, hooks,
event shapes, API endpoints, message formats. Include signatures and return shapes.
Focus on what callers need to know, not internal implementation.]

## Conventions

[Patterns specific to this domain that go beyond the global rules in AGENTS.md.
Examples: how errors are surfaced here, naming patterns for this module's files or exports,
how async is handled, specific patterns to follow when extending this module.]

## External Dependencies

[Only include if this domain integrates with external systems.
Third-party services, APIs, SDKs, databases, or infrastructure this domain owns or calls.
Note auth method, rate limits, or quirks relevant for development.]

## Constraints & Gotchas

[Things that have broken before. Non-obvious behavior. Known limitations.
Performance-sensitive areas. Things that look safe to change but aren't.
This section pays for itself the first time it prevents a bug.]
```

## Examples

### Minimal spec (technical utility module)

```markdown
# Hooks

Shared React hooks used across the application. Owns no state beyond the lifetime of the component that uses it.

## Interfaces

- `useDebounce<T>(value: T, delay: number): T` — returns a debounced copy of `value`
- `useLocalStorage<T>(key: string, initial: T): [T, (v: T) => void]` — persists value in localStorage; SSR-safe (falls back to `initial` on server)
- `useMediaQuery(query: string): boolean` — reactive media query match; cleans up listener on unmount

## Constraints & Gotchas

- `useLocalStorage` silently falls back to in-memory state if `localStorage` is unavailable (private browsing). Do not rely on it for anything critical.
- All hooks assume React 18+. Do not add hooks that depend on class component lifecycle.
```

### Full spec (business domain)

```markdown
# Payments

Handles all payment processing via Stripe. Owns the `payments` DB table and the `/api/payments` route group. Does NOT handle subscriptions — that lives in `billing`.

## Data Model

```ts
type Payment = {
  id: string           // UUID
  userId: string
  amount: number       // cents, never floats
  currency: string     // ISO 4217, e.g. "usd"
  status: "pending" | "succeeded" | "failed" | "refunded"
  stripePaymentIntentId: string
  createdAt: Date
  metadata: Record<string, string>  // arbitrary key-value, passed through to Stripe
}
```

## Interfaces

- `createPaymentIntent(userId, amount, currency, metadata?)` → `{ clientSecret, paymentIntentId }` — creates a Stripe PaymentIntent and a local `pending` record
- `confirmPayment(paymentIntentId)` → `Payment` — called by the Stripe webhook on `payment_intent.succeeded`; updates local status
- `refundPayment(paymentId, reason?)` → `Payment` — issues a full refund via Stripe and updates local status

## Conventions

- Amounts are always integers in the smallest currency unit (cents for USD). Never store or pass floats.
- All Stripe calls are wrapped in `withStripeRetry()` (defined in `src/payments/stripe-client.ts`) — do not call the Stripe SDK directly.
- Webhook handlers are idempotent — Stripe may deliver events more than once.

## External Dependencies

- **Stripe** — payment processing. Auth via `STRIPE_SECRET_KEY` env var. Webhook secret in `STRIPE_WEBHOOK_SECRET`.
  - Rate limit: 100 req/s in test mode, higher in production.
  - Webhook endpoint: `POST /api/webhooks/stripe`

## Constraints & Gotchas

- Never log `clientSecret` — it authorizes charges on behalf of the user.
- The `confirmPayment` function must be called from the webhook only, not from the client. Clients receive the `succeeded` status via polling or WebSocket, not by calling this function directly.
- Refunds can take 5–10 business days to appear. The local status updates immediately but Stripe's side is async.
```
