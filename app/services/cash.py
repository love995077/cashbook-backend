"""The contractor's cash box: category seeding, summaries and chart buckets.

WHAT COUNTS AS SPENT
--------------------
A worker's `wage` transaction is money *earned*, not money *handed over* - it
is an accrual, and the ledger nets it against advances to work out what is
still owed. The cash box only cares about money that actually moved, so
"wages paid" here is advances given + payments settled. `wages_earned` is
reported alongside it so the Home screen can still show what the crew has run
up. Getting this backwards would double-count every advance.

BUCKETING IN PYTHON
-------------------
Rows are grouped into day/week/month buckets in Python rather than with SQL
date functions. MySQL's WEEK() and SQLite's strftime disagree on where a week
starts, and the app runs against both. One contractor's rows for a reporting
window is a small result set, so the portability is worth more than pushing
the GROUP BY down.
"""

from collections import defaultdict
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Iterable, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.models.cash import CashCategory, CashEntry
from app.models.enums import CashDirection, TransactionType, UserRole
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.cash import AnalyticsBucket, AnalyticsSeries, CashSummary, CategoryTotal

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")

Period = Literal["day", "week", "month"]

# Cash that has actually left the contractor's hands, as opposed to wages
# accrued but not yet paid.
PAID_OUT_TYPES = (TransactionType.ADVANCE, TransactionType.PAYMENT)

# The starter set every contractor gets on first use. Editable afterwards -
# renaming "Food" to "Chai and food" is the point of customisable categories.
DEFAULT_CATEGORIES: tuple[tuple[str, CashDirection, str, str], ...] = (
    ("Site Advance", CashDirection.IN, "business-outline", "#1B873F"),
    ("Client Payment", CashDirection.IN, "cash-outline", "#0A7EA4"),
    ("Materials", CashDirection.OUT, "cube-outline", "#B25E09"),
    ("Fuel", CashDirection.OUT, "car-outline", "#D93036"),
    ("Transport", CashDirection.OUT, "bus-outline", "#7C5CFF"),
    ("Equipment Rental", CashDirection.OUT, "construct-outline", "#0A7EA4"),
    ("Food and Tea", CashDirection.OUT, "cafe-outline", "#C2410C"),
    ("Other", CashDirection.OUT, "ellipsis-horizontal-outline", "#60646C"),
)


def _money(value: Optional[Decimal]) -> Decimal:
    return (Decimal(value) if value is not None else ZERO).quantize(CENTS)


def ensure_default_categories(db: Session, contractor_id: int) -> None:
    """Give a contractor the starter categories, once.

    Called before any read of the category list. The check is "has this
    contractor ever had a category", not "is the list empty" - a contractor who
    deliberately deleted the defaults should not have them grow back.
    """
    exists = db.scalar(
        select(CashCategory.id).where(CashCategory.contractor_id == contractor_id).limit(1)
    )
    if exists is not None:
        return

    db.add_all(
        [
            CashCategory(
                contractor_id=contractor_id,
                name=name,
                direction=direction,
                icon=icon,
                color=color,
                is_default=True,
            )
            for name, direction, icon, color in DEFAULT_CATEGORIES
        ]
    )
    db.commit()


# --- period helpers --------------------------------------------------------


def default_window(period: Period, today: Optional[date_type] = None) -> tuple[date_type, date_type]:
    """A sensible span to chart when the caller gives no dates.

    Roughly a dozen bars in each mode: two weeks of days, twelve weeks, or a
    year of months.
    """
    today = today or business_today()
    if period == "day":
        return today - timedelta(days=13), today
    if period == "week":
        return _week_start(today - timedelta(weeks=11)), today
    return _month_start(today.replace(year=today.year - 1)), today


def _week_start(day: date_type) -> date_type:
    """Monday of that week."""
    return day - timedelta(days=day.weekday())


def _month_start(day: date_type) -> date_type:
    return day.replace(day=1)


def _next_month(day: date_type) -> date_type:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _bucket_start(day: date_type, period: Period) -> date_type:
    if period == "day":
        return day
    if period == "week":
        return _week_start(day)
    return _month_start(day)


def _bucket_end(start: date_type, period: Period) -> date_type:
    if period == "day":
        return start
    if period == "week":
        return start + timedelta(days=6)
    return _next_month(start) - timedelta(days=1)


def _label(start: date_type, period: Period) -> str:
    """Short enough to sit under a bar on a phone."""
    if period == "month":
        return start.strftime("%b %y")
    return start.strftime("%d %b")


def _buckets_between(start: date_type, end: date_type, period: Period) -> list[date_type]:
    cursor = _bucket_start(start, period)
    out: list[date_type] = []
    while cursor <= end:
        out.append(cursor)
        cursor = _bucket_end(cursor, period) + timedelta(days=1)
    return out


# --- queries ---------------------------------------------------------------


def crew_ids(
    db: Session,
    contractor_id: int,
    active_only: bool = False,
    site_id: Optional[int] = None,
) -> list[int]:
    """This contractor's workers.

    WHY `active_only` IS A CHOICE AND NOT A DEFAULT
    ----------------------------------------------
    Taking a worker off the crew deactivates the row rather than deleting it
    (see `remove_worker`), and the two kinds of figure on the Home screen want
    opposite answers about them:

      * Cash that has already moved — `wages_paid`, and therefore the balance —
        must keep counting a removed worker. The advance was handed over; the
        note left the tin. Dropping it would make the balance claim money the
        contractor does not have.

      * What is still OWED, and how many people are on the books, must not.
        Nobody is waiting to be paid, so `outstanding_payable` and
        `worker_count` are scoped to the active crew.

    Passing the flag at each call site rather than filtering here is what keeps
    those two apart.

    `site_id` narrows to the workers posted there now - the crew a site's
    "still owed" and head count are about.
    """
    stmt = select(User.id).where(
        User.contractor_id == contractor_id, User.role == UserRole.WORKER
    )
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    if site_id is not None:
        stmt = stmt.where(User.site_id == site_id)
    return list(db.scalars(stmt).all())


def _worker_rows(
    db: Session,
    crew: Iterable[int],
    start: Optional[date_type],
    end: Optional[date_type],
    site_id: Optional[int] = None,
) -> list[tuple[date_type, TransactionType, Decimal]]:
    crew = list(crew)
    if not crew:
        return []
    stmt = select(Transaction.date, Transaction.type, Transaction.amount).where(
        Transaction.worker_id.in_(crew)
    )
    if site_id is not None:
        # The site the money is attributed to, stamped on the row - not where
        # the worker happens to be posted today. A mason moved to another job
        # keeps every rupee earned here on this site's books.
        stmt = stmt.where(Transaction.site_id == site_id)
    if start is not None:
        stmt = stmt.where(Transaction.date >= start)
    if end is not None:
        stmt = stmt.where(Transaction.date <= end)
    return [(row[0], row[1], _money(row[2])) for row in db.execute(stmt).all()]


def _cash_rows(
    db: Session,
    contractor_id: int,
    start: Optional[date_type],
    end: Optional[date_type],
    site_id: Optional[int] = None,
) -> list[tuple[date_type, CashDirection, Decimal, Optional[int]]]:
    stmt = select(
        CashEntry.date, CashEntry.direction, CashEntry.amount, CashEntry.category_id
    ).where(CashEntry.contractor_id == contractor_id)
    if site_id is not None:
        stmt = stmt.where(CashEntry.site_id == site_id)
    if start is not None:
        stmt = stmt.where(CashEntry.date >= start)
    if end is not None:
        stmt = stmt.where(CashEntry.date <= end)
    return [(row[0], row[1], _money(row[2]), row[3]) for row in db.execute(stmt).all()]


def outstanding_payable(
    db: Session, contractor_id: int, site_id: Optional[int] = None
) -> Decimal:
    """What the contractor still owes, summed over workers who are owed.

    Workers in credit (paid ahead) are excluded rather than netted off - one
    worker's overpayment does not reduce what another is owed.

    Active crew only. A worker taken off the crew is not standing there waiting
    for their money, and leaving them in made "still owed" grow every time
    somebody was removed mid-balance — the figure a contractor uses to decide
    what to pay out on Saturday, quietly counting people who left in March.
    """
    crew = crew_ids(db, contractor_id, active_only=True, site_id=site_id)
    if not crew:
        return ZERO

    rows = db.execute(
        select(Transaction.worker_id, Transaction.type, func.sum(Transaction.amount))
        .where(Transaction.worker_id.in_(crew))
        .group_by(Transaction.worker_id, Transaction.type)
    ).all()

    per_worker: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for worker_id, tx_type, total in rows:
        amount = _money(total)
        if tx_type is TransactionType.WAGE:
            per_worker[worker_id] += amount
        else:
            per_worker[worker_id] -= amount

    owed = sum((value for value in per_worker.values() if value > 0), ZERO)
    return owed.quantize(CENTS)


def build_summary(
    db: Session,
    contractor_id: int,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    site_id: Optional[int] = None,
) -> CashSummary:
    """The Home screen numbers for one period (all time when unbounded).

    With `site_id`, the same figures for one site: money by the site stamped
    on each row, "still owed" and the head count by who is posted there now.
    """
    # The whole crew, removed workers included: see `crew_ids`. Cash that
    # reached somebody who has since left the job still left the cash box.
    crew = crew_ids(db, contractor_id)

    wages_earned = ZERO
    wages_paid = ZERO
    for _day, tx_type, amount in _worker_rows(db, crew, start, end, site_id):
        if tx_type is TransactionType.WAGE:
            wages_earned += amount
        else:
            wages_paid += amount

    cash_in = ZERO
    expenses_paid = ZERO
    for _day, direction, amount, _category_id in _cash_rows(
        db, contractor_id, start, end, site_id
    ):
        if direction is CashDirection.IN:
            cash_in += amount
        else:
            expenses_paid += amount

    total_out = wages_paid + expenses_paid
    return CashSummary(
        cash_in=cash_in.quantize(CENTS),
        wages_paid=wages_paid.quantize(CENTS),
        expenses_paid=expenses_paid.quantize(CENTS),
        total_out=total_out.quantize(CENTS),
        balance=(cash_in - total_out).quantize(CENTS),
        wages_earned=wages_earned.quantize(CENTS),
        # Always lifetime: what is owed is not a property of a date range.
        outstanding_payable=outstanding_payable(db, contractor_id, site_id),
        # The crew as it stands today, which is what "across N workers" beside
        # the owed figure is counting — not everyone who ever worked here.
        worker_count=len(crew_ids(db, contractor_id, active_only=True, site_id=site_id)),
        start_date=start,
        end_date=end,
        site_id=site_id,
    )


def build_series(
    db: Session,
    contractor_id: int,
    period: Period,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    site_id: Optional[int] = None,
) -> AnalyticsSeries:
    """Bars for the analytics chart, plus totals and a category breakdown.

    Empty buckets are included. A week with no spend is information - dropping
    it would slide the other bars together and make a gap look like activity.
    """
    window_start, window_end = default_window(period)
    start = start or window_start
    end = end or window_end
    if start > end:
        start, end = end, start

    crew = crew_ids(db, contractor_id)
    wage_rows = _worker_rows(db, crew, start, end, site_id)
    cash_rows = _cash_rows(db, contractor_id, start, end, site_id)

    starts = _buckets_between(start, end, period)
    wages: dict[date_type, Decimal] = {key: ZERO for key in starts}
    spend: dict[date_type, Decimal] = {key: ZERO for key in starts}
    income: dict[date_type, Decimal] = {key: ZERO for key in starts}

    for day, tx_type, amount in wage_rows:
        if tx_type in PAID_OUT_TYPES:
            wages[_bucket_start(day, period)] += amount

    per_category: dict[Optional[int], Decimal] = defaultdict(lambda: ZERO)
    for day, direction, amount, category_id in cash_rows:
        key = _bucket_start(day, period)
        if direction is CashDirection.IN:
            income[key] += amount
        else:
            spend[key] += amount
        per_category[category_id] += amount

    buckets = [
        AnalyticsBucket(
            start_date=key,
            end_date=min(_bucket_end(key, period), end),
            label=_label(key, period),
            wages_paid=wages[key].quantize(CENTS),
            expenses_paid=spend[key].quantize(CENTS),
            total_out=(wages[key] + spend[key]).quantize(CENTS),
            cash_in=income[key].quantize(CENTS),
        )
        for key in starts
    ]

    return AnalyticsSeries(
        period=period,
        start_date=start,
        end_date=end,
        buckets=buckets,
        totals=build_summary(db, contractor_id, start, end, site_id),
        by_category=_category_totals(db, contractor_id, per_category),
    )


def _category_totals(
    db: Session, contractor_id: int, per_category: dict[Optional[int], Decimal]
) -> list[CategoryTotal]:
    """Label the per-category sums, biggest first."""
    if not per_category:
        return []

    ids = [key for key in per_category if key is not None]
    known = {}
    if ids:
        known = {
            category.id: category
            for category in db.scalars(
                select(CashCategory).where(
                    CashCategory.contractor_id == contractor_id, CashCategory.id.in_(ids)
                )
            ).all()
        }

    totals: list[CategoryTotal] = []
    for category_id, amount in per_category.items():
        category = known.get(category_id) if category_id is not None else None
        totals.append(
            CategoryTotal(
                category_id=category_id,
                name=category.name if category else "Uncategorised",
                direction=category.direction if category else CashDirection.OUT,
                color=category.color if category else None,
                icon=category.icon if category else None,
                total=amount.quantize(CENTS),
            )
        )

    totals.sort(key=lambda item: item.total, reverse=True)
    return totals
