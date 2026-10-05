"""Every site's numbers at once, for the site-selection Home screen.

The Home screen is a list of the contractor's jobs, and each card answers the
three questions a contractor opens the app to ask about a site: who is on it
and have they been marked today, what is still owed to its crew, and what has
it cost. One request for all of them, built from one grouped query per figure
rather than one query per site - adding a tenth site must not add ten round
trips (see CLAUDE.md, "Aggregate in one grouped query, never per row").

WHERE EACH FIGURE COMES FROM
----------------------------
Money follows the site stamped on the row, never where somebody is posted
today: `transactions.site_id`, `cash_entries.site_id`, `petty_cash.site_id`.
A worker moved to another job, or deactivated, keeps everything they earned
here on this site's books - which is what "their historical financial data
must remain in site reports" means.

"Still owed" and the crew counts are the other way round: they are about the
people posted here NOW. A worker's balance is not split by site - what a
person is owed is one number - so a site's "still owed" is the positive
balances of its active crew, the same definition the business-wide figure
uses (`outstanding_payable`).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.cash import CashEntry
from app.models.enums import CashDirection, TransactionType, UserRole
from app.models.petty_cash import PettyCash
from app.models.site import Site
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.site import SiteOverview
from app.services.wages import weight

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


def _money(value: Optional[Decimal]) -> Decimal:
    return (Decimal(value) if value is not None else ZERO).quantize(CENTS)


def build_site_overviews(
    db: Session, sites: list[Site], today: date_type
) -> list[SiteOverview]:
    """One `SiteOverview` per site, in the order given."""
    if not sites:
        return []
    ids = [site.id for site in sites]
    month_start = today.replace(day=1)

    # --- the crew, active and not ------------------------------------------
    crew_active: dict[int, int] = defaultdict(int)
    crew_inactive: dict[int, int] = defaultdict(int)
    for site_id, active, count in db.execute(
        select(User.site_id, User.is_active, func.count())
        .where(User.site_id.in_(ids), User.role == UserRole.WORKER)
        .group_by(User.site_id, User.is_active)
    ).all():
        (crew_active if active else crew_inactive)[site_id] += int(count)

    # --- today's register ---------------------------------------------------
    marked: dict[int, int] = defaultdict(int)
    worked: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for site_id, status, count in db.execute(
        select(Attendance.site_id, Attendance.status, func.count())
        .where(Attendance.site_id.in_(ids), Attendance.date == today)
        .group_by(Attendance.site_id, Attendance.status)
    ).all():
        marked[site_id] += int(count)
        worked[site_id] += weight(status) * count

    # --- money attributed to the site ---------------------------------------
    def tx_totals(since: Optional[date_type]) -> dict[tuple[int, TransactionType], Decimal]:
        stmt = select(Transaction.site_id, Transaction.type, func.sum(Transaction.amount)).where(
            Transaction.site_id.in_(ids)
        )
        if since is not None:
            stmt = stmt.where(Transaction.date >= since, Transaction.date <= today)
        return {
            (site_id, tx_type): _money(total)
            for site_id, tx_type, total in db.execute(
                stmt.group_by(Transaction.site_id, Transaction.type)
            ).all()
        }

    def cash_totals(since: Optional[date_type]) -> dict[tuple[int, CashDirection], Decimal]:
        stmt = select(CashEntry.site_id, CashEntry.direction, func.sum(CashEntry.amount)).where(
            CashEntry.site_id.in_(ids)
        )
        if since is not None:
            stmt = stmt.where(CashEntry.date >= since, CashEntry.date <= today)
        return {
            (site_id, direction): _money(total)
            for site_id, direction, total in db.execute(
                stmt.group_by(CashEntry.site_id, CashEntry.direction)
            ).all()
        }

    def petty_totals(since: Optional[date_type]) -> dict[int, Decimal]:
        stmt = select(PettyCash.site_id, func.sum(PettyCash.amount)).where(
            PettyCash.site_id.in_(ids)
        )
        if since is not None:
            stmt = stmt.where(PettyCash.date >= since, PettyCash.date <= today)
        return {
            site_id: _money(total)
            for site_id, total in db.execute(stmt.group_by(PettyCash.site_id)).all()
        }

    tx_all, tx_month = tx_totals(None), tx_totals(month_start)
    cash_all, cash_month = cash_totals(None), cash_totals(month_start)
    petty_all, petty_month = petty_totals(None), petty_totals(month_start)

    # --- still owed to the crew posted here ---------------------------------
    posted = dict(
        db.execute(
            select(User.id, User.site_id).where(
                User.site_id.in_(ids),
                User.role == UserRole.WORKER,
                User.is_active.is_(True),
            )
        ).all()
    )
    balances: dict[int, Decimal] = defaultdict(lambda: ZERO)
    if posted:
        for worker_id, tx_type, total in db.execute(
            select(Transaction.worker_id, Transaction.type, func.sum(Transaction.amount))
            .where(Transaction.worker_id.in_(list(posted)))
            .group_by(Transaction.worker_id, Transaction.type)
        ).all():
            amount = _money(total)
            balances[worker_id] += amount if tx_type is TransactionType.WAGE else -amount
    owed: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for worker_id, balance in balances.items():
        if balance > 0:
            owed[posted[worker_id]] += balance

    def paid(totals: dict, site_id: int) -> Decimal:
        return totals.get((site_id, TransactionType.ADVANCE), ZERO) + totals.get(
            (site_id, TransactionType.PAYMENT), ZERO
        )

    overviews: list[SiteOverview] = []
    for site in sites:
        expenses = cash_all.get((site.id, CashDirection.OUT), ZERO)
        overviews.append(
            SiteOverview(
                site_id=site.id,
                crew_active=crew_active[site.id],
                crew_inactive=crew_inactive[site.id],
                marked_today=marked[site.id],
                worked_today=worked[site.id],
                wages_earned=tx_all.get((site.id, TransactionType.WAGE), ZERO),
                wages_paid=paid(tx_all, site.id),
                outstanding_payable=owed[site.id].quantize(CENTS),
                cash_in=cash_all.get((site.id, CashDirection.IN), ZERO),
                expenses_paid=expenses,
                petty_cash=petty_all.get(site.id, ZERO),
                month_spend=(
                    paid(tx_month, site.id)
                    + cash_month.get((site.id, CashDirection.OUT), ZERO)
                    + petty_month.get(site.id, ZERO)
                ).quantize(CENTS),
                today=today,
            )
        )
    return overviews
