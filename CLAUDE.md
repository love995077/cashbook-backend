# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Shape of the project

Two halves in one tree, and most tasks touch both:

- `/` — FastAPI + SQLAlchemy 2.0 + **PostgreSQL** backend. Git repo `love995077/cashbook-backend`, branch `main`.
- `mobile/` — Expo (SDK 57) + Expo Router client. Its **own** git repo `love995077/cashbook-mobile`, branch `master` (ignored by the backend repo).

Commits carry no `Co-authored-by` trailer - attribute to the local git user only.

A feature is normally not done until the model, the schema, the router, `mobile/src/data/types.ts`, `mobile/src/api/endpoints.ts`, `mobile/src/data/store.tsx` and a screen all agree. Adding a screen without the route it calls, or a route without the mobile mapper, leaves a dead end.

`README.md` (backend) and `mobile/README.md` document the endpoint tables and the file layout in detail; `phase.md` holds the original phase-by-phase brief; `DEPLOYMENT.md` is how production runs. Prefer reading those over re-deriving.

## Commands

Backend, from `/`:

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
.venv/Scripts/python.exe -m pytest                       # SQLite, ~4 min with the property test
TEST_DATABASE_URL=postgresql+psycopg://... pytest        # the real engine - do this before shipping
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

**pytest** (`tests/`, deps in `requirements-dev.txt`). `tests/conftest.py` overrides `DATABASE_URL` before importing the app and builds its own engine, so the configured database is never touched. SQLite by default; set `TEST_DATABASE_URL` to run against PostgreSQL (schema built by the real Alembic chain) or MySQL. Run against PostgreSQL before shipping anything that touches money - SQLite stores DECIMAL through a float and takes no row locks.

- Write through the HTTP API (`tests/helpers.py`), assert by reading the DB. `api.fresh()` ends the assertion session's transaction - required on MySQL (REPEATABLE READ), harmless elsewhere.
- Compute expected money **independently** (`expected_day_wage` in helpers), never with the code under test.
- `api.assert_ledger_matches_register(...)` checks the wage invariant two ways; call it after anything that changes a day.
- `tests/test_property_ledger.py` (Hypothesis) runs random edit sequences against a model. When you change wage/attendance rules, update the model, and mutation-check it: reintroduce a bug and confirm it fails.
- `tests/test_migrations.py` asserts `alembic check` is clean and the chain round-trips on PostgreSQL and MySQL.

The older `scripts/smoke_*.py` still run and still pass. `scripts/smoke_deployed.py <url>` checks a live deployment over HTTP (it writes a throwaway account).

## Wages are derived from attendance - the invariant

Every attendance row's linked WAGE (`attendance.transaction_id`) is exactly `round_half_up(wage_rate × weight(status))` on the row's date and site; a day that earns nothing has none. `weight`: present 1, half_day 0.5, absent/rain_out 0 (`STATUS_WEIGHT` in `app/services/wages.py` - one table, shared by wages, installments, reports).

- **The rate is frozen on the day** (`attendance.wage_rate`): NULL = no rate known yet (priced when one is set, `price_unpriced_days`), 0 = piece-rate day (never priced), >0 = the rate. Syncing reads the row, never `users.daily_wage` - so a raise never reprices history. Only `reprice_existing` (PUT worker) moves old days deliberately.
- **Every write to a day goes through `app/services/attendance.py`** (`record_day`, `correct_day`, `remove_day`), which moves the row, its wage and its installment together. Do not write `Attendance` rows anywhere else.
- `app/services/integrity.py` audits the invariant (`GET /contractor/integrity/wages`) and reconciles it (dry-run by default; adopts a hand-typed wage for the same day+amount instead of doubling it).
- "Today" is `business_today()` (`app/core/clock.py`, `BUSINESS_TIMEZONE`, default Asia/Kolkata). Never `date.today()` - containers run UTC.

## Site-first

Every money row says which site it belongs to: `transactions.site_id` (a day's wage = its attendance site, piece work = the log's site, anything else = the site named or the worker's posting at the time), `cash_entries.site_id`, `petty_cash.site_id`. Site figures and reports filter by **the site on the row**, never by `users.site_id` - so a moved or deactivated worker's history stays on the site where it happened. "Still owed" and crew counts are the opposite: who is posted there now. Deactivation keeps `users.site_id` and records `deactivated_on` (first day not worked); days before it can still be marked, days from it cannot (422, not 409 - the offline queue drops a 409 as "already recorded").

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
- Alembic reads its URL from `app.config.Settings`, so migrations cannot run against a different database than the app (override only via `ALEMBIC_DATABASE_URL`).
- **Migrations must build PostgreSQL and MySQL.** Enum types: `ensure_enum` + `enum_type` from `app/core/migration_support.py` (an `op.add_column` never creates a PostgreSQL type); boolean defaults `sa.false()`, not `'0'`. Downgrades drop tables without dropping their indexes first (MySQL refuses FK-backed index drops). `alembic check` must stay clean on both.
- Name lookups are case-insensitive where MySQL's collation made them so (stock items); PostgreSQL enforces it with `lower(name)` unique indexes owned by migration `3b7d9e2a4c61` and excluded from autogenerate (`_ci` suffix).

## Mobile conventions

- **Site-first layout.** An admin's Home is the site list (`SiteCard`, fed by `GET /contractor/sites/overview` held in the store). A site (`site/[id]`) holds that site's Labour, Expenses and Reports tabs; the attendance grid takes `?siteId=`. The business-wide cash box lives on Analytics. The Workers tab is the crew directory (site filter, deactivated workers).
- **`app/` is routing only.** Anything with real logic lives in `src/components/` and is rendered by the route file.
- **Per-screen data is fetched per screen**, not held in the store: petty cash, stock, machinery, invoices and photo feeds all fetch on mount with an `AbortController`. The store holds only what the dashboard reads on every open (workers, sites, cash box, outstanding tools).
- **Typed routes are generated.** After adding a file under `app/`, `.expo/types/router.d.ts` is stale until `npx expo start` regenerates it, and `tsc` will reject the new `router.push` path until then.
- **Report downloads go through `src/lib/reports.ts`** — fetch with the bearer header, write to cache, hand to the native share sheet. It validates the bytes (PDF magic number, UTF-8 BOM for CSV) so a JSON error body cannot masquerade as a report in the share sheet. Every CSV export therefore **must** keep its BOM.
- Dates for the API are built with `toISODate` / local calendar fields, never `toISOString().slice()` — that is UTC, and east of Greenwich it reports the wrong day for part of every day.

## Offline queue — read before changing flush rules

`mobile/src/offline/queue.ts` carries a long note that is load-bearing. In short: attendance replays are safe (unique `(worker_id, date)` → 409 → dropped); cash entries and transactions are safe because both carry a UNIQUE `client_ref` and the server returns the existing row on a repeat. The store mints the `clientRef` **before the first online attempt** and reuses it if the write is queued - a fresh key per attempt would defeat the dedupe. Payroll settlement is never queued (it is computed from the balance at send time); it takes an optional `client_ref` for double-taps.

## Deliberate design decisions worth not "fixing"

- **`invoices.total_amount` is typed by the admin, not computed.** The preview adds up piece-rate work, site cash-out and machinery fuel; the create route takes the figure as given. Invoices carry markup, retention and discounts this schema does not model. The preview's three subtotals can legitimately overlap (diesel logged against a machine may also sit in expenses) and are shown apart rather than summed blindly.
- **`machinery_logs.fuel_cost` is stored, not derived** from litres × rate — the rate changes between deliveries and pumps; what the site holds is a receipt. Litres sit beside it because only they separate a thirsty machine from a rising market.
- **`outstanding_payable` is not subtracted from the cash balance.** It has not been paid, so subtracting it would misstate cash in hand.
- **`crew_ids` takes `active_only` per call site rather than filtering by default.** Removing a worker deactivates the row, and the two kinds of figure want opposite answers: `outstanding_payable` and `worker_count` are the *active* crew (nobody is waiting to be paid), while `wages_paid`, `wages_earned`, `balance` and the analytics buckets are *everyone* (the cash left the tin). Filtering in one place would silently break whichever half was not being thought about.
- **A piece-rate log writes its WAGE `Transaction` in the same DB transaction**, so piece work lands in the same balance every other screen reads.
- **`rain_out` is not a flavour of `absent`**, and `WorkShift` is orthogonal to `AttendanceStatus`. Every `Record<AttendanceStatus, …>` must handle all four.
- **`site_materials` / `material_logs` are dead tables** superseded by `site_inventory` / `material_receipts`. Wiring endpoints to both would make stock recorded through one invisible to the other.

## Placeholders that raise on purpose

`app/services/speech_to_text.py`, `intent_parser.py` and `notifications.py` raise until a provider is implemented; the endpoints turn that into a 503 naming the file to edit. Everything around them — upload validation, crew scoping, result validation, the DB write — is real.
