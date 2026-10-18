# Access: Devices and pairing

The server-side lifecycle of Devices, pairing requests and login tickets: the device store, claims, rate limits, revocation, ticket browsers, names and expiry. What accessors see of them (endpoints, payloads, limits, error codes): `access/accessor-contracts.md`; rules and invariants: `access.md`.

## Device store

- Generation 1 document `access/devices.json` (kind `access_devices`, `json-documents.md`), entries `{id, name, kind, token_hash, created_at, last_used_at, last_client, approved_by, via?}` keyed by `id` (`dev_<16>`). `via` (`pairing` or `ticket`) is optional: records written before it existed lack it, and such a Device is never revoked as a ticket browser. Only the SHA-256 of a token's secret part is stored; the token is returned once.
- A removal (revoke, expiry) takes effect in memory before it is written; a failed write leaves the Device revoked for this process and answers `devices_unavailable`.
- A Device unused for 90 days expires: at start, and on its next use. Last use is kept in memory and written at most once an hour per Device, and at shutdown.
- Device names are shown to approvers in the WebUI, the Desktop and terminals: a new name (`normalize_device_name`, pairing start, `devices.rename`, a ticket's `name`) refuses characters of the Unicode categories Cc, Cf, Zl and Zp (escape sequences, bidirectional overrides, zero-width marks); names stored earlier keep loading under the older rule (no ASCII controls).
- In `local`/`lan` a credential that does not verify is ignored (`open`), and `devices.list|rename|revoke` work for `open` principals, so the host can clean up Devices after leaving `authenticated` mode.

## Pairing claims

- A decided pairing request answers repeated polls with the same outcome for `CLAIM_GRACE_SECONDS` (60 s) after the first poll that learned it (`_pairing.py`; the poll answers: `access/accessor-contracts.md` -> Endpoints). The approved answer, token included, stays in memory only that long: a timer sweeps the request once the grace has passed. Concurrent polls share one claim (`AccessService._claim`); a claim that failed on the store is tried again by the next poll, and a claimed approval is never withdrawn.
- A claim re-checks the approver (`DeviceStore.add` raises `DeviceNotFoundError` for an approver that is no longer paired), so no Device is created on behalf of a revoked one.

## Rate limits and memory bounds

- Rate limits count per effective client, an IPv6 client per /64; the limits and the pending cap are in `access/accessor-contracts.md` -> Endpoints. A direct-loopback client's pairing starts and polls count only against its own per-client limit and stay outside the pending cap, so a flood from elsewhere cannot lock the host out of pairing.
- Memory stays bounded: a rate-limit window keeps at most its limit of events per key and 4096 keys (least recently used forgotten first), a refusal summary names at most 10 clients, and there are at most 256 pending pairing requests and 256 login tickets.

## Revocation and ticket browsers

- Revoking or expiring a Device withdraws what it approved and nobody claimed yet: its approved pairing requests poll as `denied`, its login tickets are dropped (`access/accessor-contracts.md` -> RPCs). Devices it approved through pairing stay.
- Revoking a Device (`devices.revoke`, `revoke_others`, `logout`) also revokes its ticket browsers, transitively (`_with_ticket_browsers`); `revoke_others` spares the calling Device even when one of the others issued its ticket. Expiry revokes only the expired Device. Ticket browsers the host issued (local access token) belong to no Device and go only on their own.
- A login ticket redeemed by a browser that is already signed in as a browser Device keeps that Device and creates none, so repeated sign-ins of one browser (a Desktop window, the tray's Open in browser) do not pile up Devices.
