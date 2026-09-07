# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Shape of the project

Two halves in one tree, and most tasks touch both:

- `/` — FastAPI + SQLAlchemy 2.0 + MySQL backend. **Not a git repository.**
- `mobile/` — Expo (SDK 57) + Expo Router client. **This is the git repo** (`master`).

A feature is normally not done until the model, the schema, the router, `mobile/src/data/types.ts`, `mobile/src/api/endpoints.ts`, `mobile/src/data/store.tsx` and a screen all agree. Adding a screen without the route it calls, or a route without the mobile mapper, leaves a dead end.

`README.md` (backend) and `mobile/README.md` document the endpoint tables and the file layout in detail; `phase.md` holds the original phase-by-phase brief. Prefer reading those over re-deriving.

## Commands

Backend, from `/`:

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
python scripts/smoke_phase5.py          # end-to-end check, in-memory SQLite
alembic revision --autogenerate -m "..."
alembic upgrade head
```

`--host 0.0.0.0` matters: the default `127.0.0.1` bind is unreachable from a phone.

Mobile, from `mobile/`:

```bash
npx expo start          # then i / a, or scan the QR
npm run lint            # expo lint
npx tsc --noEmit        # there is no `typecheck` script; run it directly
```

**Both must pass before calling mobile work done.** `npm run lint` alone will not catch type errors.

## Verification

There is no pytest suite and no jest. The convention is a **standalone smoke script** driving `TestClient` against a throwaway SQLite database — see `scripts/smoke_phase5.py` for the pattern. New backend surface should get one, or extend an existing one.

Two traps when writing them:

- Build your **own** engine (`create_engine("sqlite://", ...)`) and override `get_db` via `app.dependency_overrides`. Do not import `app.database.engine`: `Settings.connect_args` injects PyMySQL's `ssl` dict whenever a `DATABASE_URL` is set, and `sqlite3.connect()` rejects it.
- `.env` points at a **live Aiven MySQL**. Never smoke-test against the configured database.

## Money, and how it crosses the wire

DECIMAL columns are serialised as **strings** (`"600.00"`) so nothing rides on a float. `parseAmount` in `mobile/src/lib/money.ts` is the single place that absorbs this, called from the wire mappers in `endpoints.ts`. Every new money field needs a `Wire*` type and a mapper entry — a raw `wire.amount` reaching a screen is a string pretending to be a number.

Money is `DECIMAL(10,2)` (invoices `12,2`); quantities are `DECIMAL(12,3)` — deliveries arrive as 2.5 tons and tiling is measured in 120.5 sq ft.

## Roles and the two-lock rule

Wire values are `worker | admin | supervisor`. `UserRole.CONTRACTOR` is a **Python alias of `ADMIN`**, not a fourth role — `UserRole.CONTRACTOR is UserRole.ADMIN` is True. The word "contractor" survives in route prefixes, dependency names and UI copy; the stored and transmitted value is `admin`.

Access is enforced **twice, and both are required**:

1. **API** — `app/dependencies.py`. `CurrentContractor` is admin-only and guards the money. `CurrentSiteStaff` admits admin or supervisor, then `require_site_access` / `require_site_worker` / `accessible_site_ids` narrow to the site a supervisor is actually assigned (`sites.supervisor_id`).
2. **App** — the screen is not drawn. `isAdmin` / `canRunSites` in `mobile/src/data/types.ts`; the tab set is narrowed in `app/(contractor)/dashboard/_layout.tsx` with `href: null`.

Hiding a screen means hiding the door to it too: the Workers tab is admin-only because it leads to `worker/[id]` → `settle/[id]`, which is payroll.

**Ownership misses answer 404, never 403** — a 403 would confirm that another contractor's site or worker exists.

The store's `load()` in `mobile/src/data/store.tsx` branches on role. A supervisor must not be sent down the admin path: the cash summary, categories and per-worker ledgers are all admin-only, so one 403 would take the whole dashboard down rather than hiding a card.

## Backend conventions

- **Routers return read models built by a `_read_*` helper**, which flattens joined names (`worker_name`, `site_name`, `supervisor_name`) so a list row renders without a lookup per row. Aggregate in one grouped query, never per row.
- **Uploads store a key, never a URL.** `app/services/storage.py` owns the allow-list and the key layout; `public_url()` derives the link at read time, so moving to a CDN changes one function. Write the file to storage **before** inserting the row, and delete it on the error path — an orphaned file is recoverable, a row pointing at a missing image is not.
- **Multipart routes validate through the Pydantic schema by hand.** FastAPI is not validating a `Form()` route, so construct the schema in a `try` and re-raise `ValidationError` as a 422 (see `app/routers/machinery.py`) — otherwise it surfaces as a 500.
- **Register new routers after `sites`** in `main.py`; they are parameterised children of `/contractor/sites` and the static path must match first. Declare static paths (`/preview`) before `/{id}` within a router for the same reason.
- Alembic reads its URL from `app.config.Settings`, so migrations cannot run against a different database than the app.

## Mobile conventions

- **`app/` is routing only.** Anything with real logic lives in `src/components/` and is rendered by the route file.
- **Per-screen data is fetched per screen**, not held in the store: petty cash, stock, machinery, invoices and photo feeds all fetch on mount with an `AbortController`. The store holds only what the dashboard reads on every open (workers, sites, cash box, outstanding tools).
- **Typed routes are generated.** After adding a file under `app/`, `.expo/types/router.d.ts` is stale until `npx expo start` regenerates it, and `tsc` will reject the new `router.push` path until then.
- **Report downloads go through `src/lib/reports.ts`** — fetch with the bearer header, write to cache, hand to the native share sheet. It validates the bytes (PDF magic number, UTF-8 BOM for CSV) so a JSON error body cannot masquerade as a report in the share sheet. Every CSV export therefore **must** keep its BOM.
- Dates for the API are built with `toISODate` / local calendar fields, never `toISOString().slice()` — that is UTC, and east of Greenwich it reports the wrong day for part of every day.

## Offline queue — read before changing flush rules

`mobile/src/offline/queue.ts` carries a long note that is load-bearing. In short: attendance replays are safe (unique `(worker_id, date)` → 409 → dropped), cash entries are safe (`client_ref` is a UNIQUE idempotency key and the server returns the existing row), **transactions are not** — there is no natural key, so a replay pays a worker twice. `TRANSACTION_RETRY_IS_SAFE` stays false until `transactions` gains a `client_ref` the way `cash_entries` has one. Payroll settlement is never queued for the same reason.

## Deliberate design decisions worth not "fixing"

- **`invoices.total_amount` is typed by the admin, not computed.** The preview adds up piece-rate work, site cash-out and machinery fuel; the create route takes the figure as given. Invoices carry markup, retention and discounts this schema does not model. The preview's three subtotals can legitimately overlap (diesel logged against a machine may also sit in expenses) and are shown apart rather than summed blindly.
- **`machinery_logs.fuel_cost` is stored, not derived** from litres × rate — the rate changes between deliveries and pumps; what the site holds is a receipt. Litres sit beside it because only they separate a thirsty machine from a rising market.
- **`outstanding_payable` is not subtracted from the cash balance.** It has not been paid, so subtracting it would misstate cash in hand.
- **A piece-rate log writes its WAGE `Transaction` in the same DB transaction**, so piece work lands in the same balance every other screen reads.
- **`rain_out` is not a flavour of `absent`**, and `WorkShift` is orthogonal to `AttendanceStatus`. Every `Record<AttendanceStatus, …>` must handle all four.
- **`site_materials` / `material_logs` are dead tables** superseded by `site_inventory` / `material_receipts`. Wiring endpoints to both would make stock recorded through one invisible to the other.

## Placeholders that raise on purpose

`app/services/speech_to_text.py`, `intent_parser.py` and `notifications.py` raise until a provider is implemented; the endpoints turn that into a 503 naming the file to edit. Everything around them — upload validation, crew scoping, result validation, the DB write — is real.
