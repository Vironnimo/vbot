# Access: Devices and pairing

The server-side lifecycle of Devices, pairing requests and login tickets: the device store, claims, rate limits, revocation, ticket browsers, names and expiry. What accessors see of them (endpoints, payloads, limits, error codes): `access/accessor-contracts.md`; rules and invariants: `access.md`.

## Device store

- Generation 1 document `access/devices.json` (kind `access_devices`, `json-documents.md`), entries `{id, name, kind, token_hash, created_at, last_used_at, last_client, approved_by, via?}` keyed by `id` (`dev_<16>`). `via` (`pairing` or `ticket`) is optional: records written before it existed lack it, and such a Device is never revoked as a ticket browser. Only the SHA-256 of a token's secret part is stored; the token is returned once.
- A removal (revoke, expiry) takes effect in memory before it is written; a failed write leaves the Device revoked for this process and answers `devices_unavailable`. Pending removals are retried by repeated revocation, any later successful Device write, and the shutdown flush, including when no last-use timestamp changed. A crash before storage recovers can still restore a removal that never reached disk.
- A Device unused for 90 days expires: at start, and on its next use. Activity of a ticket browser renews its existing issuer chain too; `last_used_at` and `last_client` include that delegated activity, so an active Desktop window keeps its Desktop signed in. Last use is kept in memory and written at most once an hour per Device, and at shutdown. A capability grant alone does not renew activity; ordinary credential authentication still does.
- A Device write that discovers an invalid or unreadable on-disk document makes the store unavailable and answers `devices_unavailable`, just as a failed load does; repair the file and restart. The damaged document is never overwritten.
- Device names are shown to approvers in the WebUI, the Desktop and terminals: a new name (`normalize_device_name`, pairing start, `devices.rename`, a ticket's `name`) refuses characters of the Unicode categories Cc, Cf, Cs, Zl and Zp (escape sequences, bidirectional overrides, zero-width marks, invalid surrogate code points); names stored earlier keep loading under the older rule (no ASCII controls).
- In `local`/`lan` a credential that does not verify is ignored (`open`), and `devices.list|rename|revoke` work for `open` principals, so the host can clean up Devices after leaving `authenticated` mode.

## Pairing claims

- A decided pairing request answers repeated polls with the same outcome for `CLAIM_GRACE_SECONDS` (60 s) once its claim has settled and can be returned (`_pairing.py`; the poll answers: `access/accessor-contracts.md` -> Endpoints). The approved answer, token included, stays in memory only that long: a timer sweeps the request once the grace has passed. Concurrent polls share one claim (`AccessService._claim`); a failed store write keeps the original unclaimed expiry, while an admitted claim is not swept during persistence. A retry after a lost or cancelled response receives the settled outcome, and a claimed approval is never withdrawn.
- A claim re-checks the approver (`DeviceStore.add` raises `DeviceNotFoundError` for an approver that is no longer paired), so no Device is created on behalf of a revoked one.

## Rate limits and memory bounds

- Rate limits count per effective client, an IPv6 client per /64; all direct-loopback addresses share one local client budget. The limits are in `access/accessor-contracts.md` -> Endpoints. Local pairing starts and polls stay outside the shared remote budget and have their own bounded table, so a flood from elsewhere cannot lock the host out of pairing.
- Memory stays bounded: a rate-limit window keeps at most its limit of events per key and 4096 keys (least recently used forgotten first), a refusal summary names at most 10 clients, and there are at most 256 remote pairing requests, 32 local pairing requests, and 256 login tickets. Both pairing tables make room by the same crowding-client rule.

## Revocation and ticket browsers

- Revoking or expiring a Device withdraws what it approved and nobody claimed yet: its approved pairing requests poll as `denied`, its login tickets are dropped (`access/accessor-contracts.md` -> RPCs). Devices it approved through pairing stay.
- Revoking or expiring a Device also revokes its ticket browsers, transitively (`_with_ticket_browsers`). Cascade expansion and removal run as one ordered Device write, after earlier admitted creations, so a ticket redeemed concurrently cannot escape the cascade. Expiry re-checks activity after earlier writes settle. Approval withdrawal and live connection closure settle even after write failure or caller cancellation; `revoke_others` spares the calling Device even when one of the others issued its ticket. Ticket browsers the host issued (local access token) belong to no Device and go only on their own.
- A login ticket redeemed by a browser that is already signed in keeps that Device and cookie. A ticket browser is rebound to the fresh ticket's issuer in the ordered Device write, repairing stale delegation after a Desktop pairs again; both Devices must still exist and rebinding cannot create a cycle. Self-issued tickets keep the current grant, as do independently paired browser Devices. Descendants follow the rebound browser, and later logout of its new issuer revokes them all.
