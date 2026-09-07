# Cashbook API

Attendance and cash ledger for contractors and workers.
**Phase 9:** JWT auth, contractor and worker routes, balance calculation,
voice-driven entry (AI scaffolded), PDF statements, SMS/WhatsApp notifications
(provider scaffolded), and a containerised production setup.

**Phase 5 (cash box):** categorised expenses and site advances, a dashboard
summary, day/week/month analytics, and PDF + CSV exports.

**Phase 10 (site logistics):** site petty cash, piece-rate payroll, advances
recovered in daily installments, tool checkout, and material receipts that move
live site stock.

## Stack
FastAPI · SQLAlchemy 2.0 · MySQL (PyMySQL) · Pydantic v2 · PyJWT · bcrypt · ReportLab

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your MySQL credentials and a JWT secret:

```
DB_USER=root
DB_PASSWORD=yourpassword
DB_HOST=localhost
DB_PORT=3306
DB_NAME=cash_book

JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
JWT_EXPIRE_MINUTES=10080
CORS_ORIGINS=*
```

## Run (development)

```bash
uvicorn app.main:app --reload
```

On startup the app creates the `cash_book` schema if it is missing, then creates
all tables. Visit http://127.0.0.1:8000/docs for the interactive API docs and
http://127.0.0.1:8000/health for a liveness check.

## API

All routes except `/health`, `/auth/register` and `/auth/login` need an
`Authorization: Bearer <token>` header. Tokens carry the user id and role;
contractor routes reject workers and vice versa with a 403.

### Auth

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/auth/register` | `name, phone, password, role`, plus `daily_wage`/`contractor_id` for workers | 201 + token and user |
| POST | `/auth/login` | `phone, password` | token and user |
| GET | `/auth/me` | — | the signed-in user |

### Contractor

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/contractor/workers` | `name, phone, password, daily_wage?` | 201 + the new worker |
| GET | `/contractor/workers` | — | this contractor's crew |
| POST | `/contractor/attendance` | `worker_id, date, status, note?` | 201 + the attendance row |
| POST | `/contractor/transactions` | `worker_id, type, amount, date, note?` | 201 + the transaction |
| GET | `/contractor/workers/{id}/ledger` | `?start_date&end_date` | that worker's ledger |

`status` is `present`, `half_day` or `absent`; `type` is `wage`, `advance` or
`payment`. A worker who belongs to a different contractor reads as a 404, and a
second attendance row for the same worker and date is a 409.

### Cash box (Phase 5)

Everything the contractor handles that is **not** tied to a worker: the advance
the site owner pays in, and what gets spent on materials, fuel, transport and
rentals.

| Method | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| GET | `/contractor/categories` | `?direction&include_inactive` | this contractor's categories |
| POST | `/contractor/categories` | `name, direction, icon?, color?` | 201 + the category |
| PATCH | `/contractor/categories/{id}` | `name?, icon?, color?, is_active?` | the category |
| DELETE | `/contractor/categories/{id}` | — | 204 |
| GET | `/contractor/cash-entries` | `?start_date&end_date&direction&limit` | entries, newest first |
| POST | `/contractor/cash-entries` | `amount, date, category_id?, direction?, note?, client_ref?` | 201 + the entry |
| PATCH | `/contractor/cash-entries/{id}` | `amount?, date?, category_id?, note?` | the entry |
| DELETE | `/contractor/cash-entries/{id}` | — | 204 |
| GET | `/contractor/summary` | `?start_date&end_date` | cash in, cash out, balance |
| GET | `/contractor/analytics` | `?period=day\|week\|month`, `start_date?`, `end_date?` | chart buckets and totals |

`direction` is `in` (received) or `out` (spent), and is stored on the entry as
well as the category so renaming or deleting a category later cannot flip a
year of history from spend to income.

The first read of `/contractor/categories` seeds a starter set (Site Advance,
Client Payment, Materials, Fuel, Transport, Equipment Rental, Food and Tea,
Other), once per contractor. A contractor who deletes them does not get them
back. Deleting a category that already has entries against it **retires** it
instead: the rows keep their label so old reports still read correctly.

`POST /contractor/cash-entries` is **idempotent on `client_ref`**. The mobile
offline queue sends its local key, and a replay returns the stored row rather
than charging the contractor twice.

**What counts as spent.** A worker's `wage` transaction is money *earned*, not
money handed over, so `wages_paid` in the summary and the analytics buckets is
advances given + payments settled. `wages_earned` is reported separately, and
`outstanding_payable` (what is still owed to the crew, all time) is deliberately
**not** subtracted from the balance - it has not been paid yet, so subtracting
it would misstate the cash actually in hand.

### Site logistics, piece rate and tools (Phase 10)

Five things a contractor does on site between marking the crew and paying them.

| Method | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| GET | `/contractor/sites/{id}/petty-cash` | `?start_date&end_date&limit` | small cash logged at this site |
| POST | `/contractor/sites/{id}/petty-cash` | `amount, description, date?` | 201 + the entry |
| GET | `/contractor/sites/{id}/petty-cash/totals` | `?start_date&end_date` | all-time and windowed totals |
| DELETE | `/contractor/sites/{id}/petty-cash/{entry_id}` | — | 204 |
| GET | `/contractor/piece-rate-logs` | `?worker_id&site_id&start_date&end_date` | measured work |
| POST | `/contractor/piece-rate-logs` | `worker_id, work_description, quantity, rate, site_id?, date?` | 201 + the log |
| POST | `/contractor/attendance/bulk` | `marks[], date?, site_id?, overwrite?` | 201 + what changed |
| GET | `/contractor/deductions` | `?worker_id&active_only` | installment plans |
| POST | `/contractor/deductions` | `worker_id, principal_amount, daily_deduction` | 201 + the plan |
| PATCH | `/contractor/deductions/{id}` | `daily_deduction?, is_active?` | the plan |
| GET | `/contractor/deductions/{id}/installments` | — | every deduction taken, oldest first |
| GET | `/contractor/sites/{id}/tools` | `?include_inactive` | the store, with who holds what |
| POST | `/contractor/sites/{id}/tools` | `name, inventory_count?` | 201 + the tool |
| POST | `/contractor/sites/{id}/tools/{tool_id}/assign` | `worker_id, note?` | 201 + the checkout |
| PATCH | `/contractor/tool-assignments/{id}` | `status: returned\|lost` | the closed assignment |
| GET | `/contractor/tools/outstanding` | `?limit` | what is still out, across every site |
| GET | `/contractor/sites/{id}/inventory` | — | live stock |
| GET | `/contractor/sites/{id}/material-receipts` | `?start_date&end_date&limit` | deliveries |
| POST | `/contractor/sites/{id}/material-receipts` | multipart: `item_name, quantity_received, unit, challan?` | 201 + the GRN |

**Piece rate.** `users.wage_type` is `daily` or `piece_rate`. A piece-rate log
writes the matching WAGE `Transaction` in the same database transaction and
keeps its id — piece work is not a second definition of what a worker is owed,
so it lands in the same balance every other screen reads. `total_amount` is
computed server-side; a client-supplied total is a number nobody can check
against the two meant to produce it.

**Installment plans.** Sending `daily_deduction` on an advance opens an
`AutoDeduction` against it. The advance is still an ADVANCE row and still fully
subtracted from the ledger balance — that is what was handed over, and the
worker's own screen must agree. What changes is what may be PAID:

```
net_payable = window_balance + emi_advances - installments_recovered
```

The advance is put back because it is not due in full this period; the
installments actually recovered come off instead. Over the life of a plan the
two sides meet, so nothing is recovered twice or forgiven by accident.
`emi_outstanding` is the gap, and is the debt carried into the next period.

Installments are taken by `POST /contractor/attendance/bulk` on days marked
present (a half day recovers half; an absence recovers nothing — there were no
wages to take it from). Each one is a dated row in `deduction_installments`,
unique on `(deduction_id, date)`, so resubmitting a day takes nothing more.

**Bulk attendance** is replay-safe throughout: an already-marked worker is left
alone unless `overwrite` is set, a second piece-rate row for the same worker and
date is skipped, and installments are guarded by that unique constraint.
Resubmitting a grid whose response was lost therefore changes nothing rather
than paying the crew twice.

**Material GRN.** A delivery files the receipt and moves `site_inventory` in one
transaction, so the two cannot disagree about what arrived. The unit is settled
the first time an item is received at a site; a later delivery in a different
unit is refused rather than converted, because nobody here knows how many kg are
in a brass of sand. The challan photo is stored as a key, not a URL — the public
link is derived at read time and surfaces as `challan_image_url`.

### Worker

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/worker/ledger` | own transactions, attendance and balance |

### Voice entry

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/api/voice-entry` | multipart: `audio` file, optional `worker_id`, `language` (default `hi`) | what was recorded |

Contractor-only. The response `status` is one of:

- `recorded` — entries were written; `created` holds them
- `needs_confirmation` — understood but ambiguous; `pending` and `problems`
  explain what to check. Nothing was written
- `not_understood` — no usable transaction in the transcript

**The two AI steps are placeholders.** `app/services/speech_to_text.py` and
`app/services/intent_parser.py` raise until a provider is implemented, and the
endpoint turns that into a 503 naming the file to edit. Everything around them
— upload validation, crew scoping, result validation, and the database write —
is implemented and tested.

### Statements

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| GET | `/api/reports/worker/{worker_id}/pdf` | `start_date?`, `end_date?` | the statement as `application/pdf` |

Readable by the worker themselves **and** by the contractor whose crew they are
on; anyone else gets a 404. The PDF carries the worker and contractor details,
an opening balance, the period's wages/advances/payments, a closing balance,
then the cash entries and attendance with a days-worked total. Tables repeat
their header across pages.

Built with **ReportLab**, not WeasyPrint or pdfkit: WeasyPrint needs a
system-wide GTK/Pango runtime on Windows (it installs, then fails to import),
and pdfkit shells out to the unmaintained wkhtmltopdf binary. ReportLab is pure
Python. Note it renders Latin text only — see the font note at the top of
`app/services/pdf_report.py` for enabling Devanagari.

Design notes worth knowing:

- **One utterance can carry several transactions.** "Aaj 600 rupaye wage aur 200
  rupaye advance diya" is a wage *and* an advance, and both get written.
- **The write is all-or-nothing.** If any entry in an utterance is unresolved,
  none are saved — a contractor who said two things and saw one recorded has no
  way to tell which.
- **The model's output is untrusted.** A worker id it returns is re-checked
  against the caller's crew, and negative amounts and future dates are rejected
  before anything reaches the ledger.
- **`worker_id` fills the gap** when the speaker names nobody. Without it, an
  unnamed worker becomes `needs_confirmation` rather than a guess.

Ledger responses look like:

```json
{
  "worker": { "id": 2, "name": "Suresh", "...": "..." },
  "balance": {
    "total_wages": "900.00",
    "total_advances": "200.00",
    "total_payments": "150.00",
    "balance": "550.00"
  },
  "transactions": [ ... ],
  "attendance": [ ... ]
}
```

`balance = total_wages - total_advances - total_payments` — a positive balance is
what the contractor still owes. `start_date` / `end_date` narrow the listed rows
only; the balance always covers the worker's full history.

### Exports (Phase 5)

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| GET | `/api/reports/business/pdf` | `start_date?`, `end_date?` | the whole job as `application/pdf` |
| GET | `/api/reports/transactions/csv` | `worker_id?`, `start_date?`, `end_date?` | `text/csv` |
| GET | `/api/reports/attendance/csv` | `worker_id?`, `start_date?`, `end_date?` | `text/csv` |
| GET | `/api/reports/cash-entries/csv` | `start_date?`, `end_date?` | `text/csv` |

The business report is contractor-only and covers cash in, cash out, a
per-category breakdown, the crew with days worked and balances, and every
expense in the period. The cash-entries export is contractor-only for the same
reason — a worker has no visibility of the site cash box.

The transaction and attendance exports are scoped once, in `_export_scope`: a
contractor gets the whole crew (or one worker via `worker_id`), a worker gets
exactly themselves whatever `worker_id` they pass.

Every CSV starts with a **UTF-8 BOM**. Without it Excel on Windows reads the
file as the local ANSI codepage and a rupee sign or a Devanagari name arrives as
mojibake; every other reader skips it. Each export also carries a signed column
(`signed_amount`, `days_worked`) so a spreadsheet can sum straight down without
the reader knowing which types subtract.

## Layout

```
app/
  config.py          Environment-backed settings (DB, JWT, CORS)
  database.py        Engine, SessionLocal, Base, get_db, init_db
  dependencies.py    DbSession, CurrentUser, CurrentContractor, CurrentWorker
  main.py            FastAPI app; CORS, routers, table creation on startup
  core/
    security.py      bcrypt hashing, JWT encode/decode
  models/
    enums.py         UserRole, AttendanceStatus, TransactionType, CashDirection, Material*
    user.py          User (worker / contractor)
    attendance.py    Attendance (one row per worker per day)
    transaction.py   Transaction (wage / advance / payment)
    cash.py          CashCategory, CashEntry (the contractor's cash box)
    site.py          Site (a place work happens)
    material.py      SiteMaterial, MaterialLog (quantities on site)
  routers/
    auth.py          /auth/register, /auth/login, /auth/me
    contractor.py    /contractor/* including categories, cash entries, analytics
    worker.py        /worker/ledger
    voice.py         /api/voice-entry
    reports.py       /api/reports/* (worker PDF, business PDF, CSV exports)
  schemas/
    auth.py          RegisterRequest, LoginRequest, TokenResponse
    voice.py         ParsedEntry, ParsedIntent, VoiceEntryResponse
    ledger.py        BalanceSummary, WorkerLedger
    user.py          UserCreate, UserUpdate, UserRead, WorkerCreate
    attendance.py    AttendanceCreate, AttendanceUpdate, AttendanceRead
    transaction.py   TransactionCreate, TransactionUpdate, TransactionRead
    cash.py          Cash categories and entries, CashSummary, AnalyticsSeries
  services/
    ledger.py            calculate_balance, build_worker_ledger
    cash.py              Category seeding, build_summary, build_series
    speech_to_text.py    PLACEHOLDER — audio to text
    intent_parser.py     PLACEHOLDER — transcript to transactions
    pdf_report.py        Statement and business report rendering (ReportLab)
    csv_report.py        CSV exports (transactions, attendance, cash entries)
    notifications.py     PLACEHOLDER — SMS / WhatsApp sending
```

## Tables

**users** — `id, name, phone (unique), password_hash, role, daily_wage, contractor_id, is_active, created_at, updated_at`
Self-referential: a worker's `contractor_id` points at their contractor.

**attendance** — `id, worker_id, date, status, note, created_at, updated_at`
Unique on `(worker_id, date)`.

**transactions** — `id, worker_id, type, amount, date, note, created_at, updated_at`

**cash_categories** — `id, contractor_id, name, direction, icon, color, is_default, is_active, created_at, updated_at`
Unique on `(contractor_id, name)`.

**cash_entries** — `id, contractor_id, category_id, site_id, direction, amount, date, note, client_ref, created_at, updated_at`
`client_ref` is UNIQUE and nullable: it is the mobile offline queue's key, and
the unique index is what makes replaying a queued expense a no-op. `category_id`
is `ON DELETE SET NULL` — deleting a category must not delete the money spent
under it.

**petty_cash** — `id, site_id, amount, description, date, recorded_by, created_at, updated_at`
`description` is NOT NULL, unlike most notes here: an unlabelled amount is the
one entry nobody can account for a month later.

**piece_rate_logs** — `id, worker_id, site_id, work_description, quantity, rate, total_amount, date, transaction_id, created_at, updated_at`
`transaction_id` is the WAGE this log wrote, `ON DELETE SET NULL`.

**auto_deductions** — `id, worker_id, principal_amount, daily_deduction, balance_remaining, is_active, transaction_id, note, created_at, updated_at`
`balance_remaining` is a rollup of the installments below and is always
re-derivable from them; `app/services/deductions.py` is the only thing that
moves it.

**deduction_installments** — `id, deduction_id, worker_id, amount, date, created_at`
Unique on `(deduction_id, date)` — the replay guard that stops a day being
deducted twice.

**tools** — `id, site_id, name, inventory_count, notes, is_active, created_at, updated_at`
Unique on `(site_id, name)`. `inventory_count` is what the site holds in total;
what is on the shelf is that minus the open assignments.

**tool_assignments** — `id, tool_id, worker_id, checkout_time, return_time, status, note, created_at, updated_at`

**site_inventory** — `id, site_id, item_name, quantity_in_stock, unit, created_at, updated_at`
Unique on `(site_id, item_name)` — the upsert key a delivery writes through.

**material_receipts** — `id, site_id, item_name, quantity_received, unit, inventory_id, challan_image_path, date, supplier, note, received_by, created_at, updated_at`

`site_materials` and `material_logs` predate `site_inventory` /
`material_receipts` and cover the same ground from the other direction — a
catalogue plus a signed movement log, from which stock is derived. They have
never been wired to an endpoint. Running both would be a genuine hazard: stock
recorded through one is invisible to the other. If `material_logs` is ever given
endpoints, one of the two has to become the source of truth and the other a view
of it.

Money is `DECIMAL(10,2)`, mapped to Python `Decimal`. Material and piece-work
quantities are `DECIMAL(12,3)` — deliveries arrive as 2.5 tons, and tiling is
measured in 120.5 sq ft. Enums are stored as lowercase string values (`worker`,
`half_day`, `advance`, `piece_rate`, ...).

`password_hash` is new in Phase 2. `init_db` only creates missing tables, so an
existing `users` table needs the column added by hand:

```sql
ALTER TABLE users ADD COLUMN password_hash VARCHAR(60) NOT NULL AFTER phone;
```

## Notifications

`app/services/notifications.py` messages a worker when money moves in a way
worth interrupting them for:

- **any payment settled** — cash actually changed hands
- **an advance at or above `LARGE_ADVANCE_THRESHOLD`** (default Rs. 3,000)

Wages are deliberately excluded: they are logged daily, and a message per wage
entry trains everyone to ignore the messages.

Hooked into `POST /contractor/transactions` and `POST /api/voice-entry`. Both
dispatch through `BackgroundTasks` **after** the response, and
`notify_transaction` swallows its own errors — a messaging outage must never
turn a committed ledger write into a failed request.

`send_message` is a **placeholder**. It raises until a provider is implemented;
the module documents both Twilio and the Meta WhatsApp Cloud API, plus the
traps: WhatsApp's 24-hour window (these notifications are contractor-triggered,
so they will nearly always need pre-approved templates), and E.164 formatting
of the bare 10-digit numbers the database stores. Disabled by default via
`NOTIFICATIONS_ENABLED`.

## Deploy

```bash
cp .env.production.example .env.production   # fill in the CHANGE_ME values
docker compose --env-file .env.production up -d --build
```

MySQL plus the API behind gunicorn. Table creation runs as a one-off `migrate`
service rather than in the API workers — `AUTO_CREATE_TABLES` is false in
production, because otherwise every gunicorn worker races to run
`CREATE DATABASE` / `create_all` on boot.

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for updates, backups, migrations, the
TLS/CORS checklist, and the EAS build commands.

## Mobile app

`mobile/` — Expo + Expo Router client. See `mobile/README.md`.

## Next
Implement the two AI providers, then worker summaries and editing or voiding
past entries. `transactions` should also gain a nullable UNIQUE `client_ref`,
the way `cash_entries` already has, so the mobile offline queue can replay a
worker advance safely instead of surfacing an ambiguous failure.
