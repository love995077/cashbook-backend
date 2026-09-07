"""CSV exports of the ledger, for anyone who wants the numbers in a spreadsheet.

A PDF statement is for showing a worker what they are owed; a CSV is for the
contractor's accountant. Both come from the same rows.

WHY THE BOM
-----------
Excel on Windows reads a bare UTF-8 file as the local ANSI codepage, so the
rupee sign and any Devanagari name arrive as mojibake. A leading BOM is the one
thing that makes it detect UTF-8, and every other reader skips it. It is
written here rather than left to the caller so no export can forget it.

WHY \r\n
---------
RFC 4180 line endings. `csv.writer` emits them when told to, and Excel is
happier with them than with bare newlines.
"""

import csv
from datetime import date as date_type
from decimal import Decimal
from io import StringIO
from typing import Iterable, Optional, Sequence

from app.models.attendance import Attendance
from app.models.cash import CashEntry
from app.models.enums import AttendanceStatus, TransactionType, WorkShift
from app.models.petty_cash import PettyCash
from app.models.transaction import Transaction
from app.models.user import User

BOM = "\ufeff"

# Every member of AttendanceStatus must appear here. RAIN_OUT was added in
# Phase 9 and this dict was not updated, so `STATUS_LABELS[record.status]`
# raised KeyError - a 500 on the attendance export for any contractor who had
# ever used the rain-out button.
STATUS_LABELS = {
    AttendanceStatus.PRESENT: "Present",
    AttendanceStatus.HALF_DAY: "Half day",
    AttendanceStatus.ABSENT: "Absent",
    AttendanceStatus.RAIN_OUT: "Rained out",
}

TYPE_LABELS = {
    TransactionType.WAGE: "Wage earned",
    TransactionType.ADVANCE: "Advance given",
    TransactionType.PAYMENT: "Payment settled",
}

SHIFT_LABELS = {WorkShift.DAY: "Day", WorkShift.NIGHT: "Night"}

# How each type moves what the worker is owed, so a spreadsheet can be summed
# straight down the signed column.
TYPE_SIGN = {
    TransactionType.WAGE: 1,
    TransactionType.ADVANCE: -1,
    TransactionType.PAYMENT: -1,
}


def _render(header: Sequence[str], rows: Iterable[Sequence[object]]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return (BOM + buffer.getvalue()).encode("utf-8")


def _amount(value: Decimal) -> str:
    """Plain digits - no separators, no currency symbol, so it stays numeric."""
    return f"{value:.2f}"


def transactions_csv(
    transactions: Sequence[Transaction], workers: dict[int, User]
) -> bytes:
    """One row per cash transaction, oldest first.

    `signed_amount` is the column to sum: wages add to what is owed, advances
    and payments subtract from it.
    """
    rows = [
        [
            tx.date.isoformat(),
            workers[tx.worker_id].name if tx.worker_id in workers else "",
            workers[tx.worker_id].phone if tx.worker_id in workers else "",
            tx.type.value,
            TYPE_LABELS[tx.type],
            _amount(tx.amount),
            _amount(tx.amount * TYPE_SIGN[tx.type]),
            tx.note or "",
        ]
        for tx in transactions
    ]
    return _render(
        [
            "date",
            "worker",
            "phone",
            "type",
            "type_label",
            "amount",
            "signed_amount",
            "note",
        ],
        rows,
    )


def attendance_csv(records: Sequence[Attendance], workers: dict[int, User]) -> bytes:
    """One row per attendance mark, oldest first.

    `days_worked` is 1 / 0.5 / 0, which is what turns a column of marks into a
    payable day count without the reader having to know the rule.
    """
    # Must cover every AttendanceStatus. RAIN_OUT was missing alongside the
    # same gap in STATUS_LABELS - two KeyErrors in one row builder.
    #
    # Zero, and the reason matters: a rained-out day carries no wage of its
    # own (see AttendanceStatus). It is scored the same as an absence here
    # while staying a distinct status in the column beside it, so the day
    # count stays payable-accurate without losing why the day was lost.
    weight = {
        AttendanceStatus.PRESENT: "1",
        AttendanceStatus.HALF_DAY: "0.5",
        AttendanceStatus.ABSENT: "0",
        AttendanceStatus.RAIN_OUT: "0",
    }
    rows = [
        [
            record.date.isoformat(),
            workers[record.worker_id].name if record.worker_id in workers else "",
            workers[record.worker_id].phone if record.worker_id in workers else "",
            record.status.value,
            STATUS_LABELS[record.status],
            weight[record.status],
            SHIFT_LABELS[record.shift],
            record.note or "",
        ]
        for record in records
    ]
    return _render(
        [
            "date",
            "worker",
            "phone",
            "status",
            "status_label",
            "days_worked",
            "shift",
            "note",
        ],
        rows,
    )


def cash_entries_csv(entries: Sequence[CashEntry]) -> bytes:
    """One row per general expense or payment received, oldest first."""
    rows = [
        [
            entry.date.isoformat(),
            entry.direction.value,
            entry.category.name if entry.category else "Uncategorised",
            _amount(entry.amount),
            _amount(entry.amount if entry.direction.value == "in" else -entry.amount),
            entry.note or "",
        ]
        for entry in entries
    ]
    return _render(
        ["date", "direction", "category", "amount", "signed_amount", "note"], rows
    )


def filename(kind: str, owner: str, start: Optional[date_type], end: date_type) -> str:
    """e.g. 'Ramesh-Builders-transactions-2026-08-01-to-2026-09-03.csv'"""
    safe = "".join(char if char.isalnum() else "-" for char in owner)
    safe = "-".join(part for part in safe.split("-") if part) or "cashbook"
    span = f"{start.isoformat()}-to-{end.isoformat()}" if start else f"upto-{end.isoformat()}"
    return f"{safe}-{kind}-{span}.csv"



def monthly_csv(
    month: str,
    transactions: Sequence[Transaction],
    attendance: Sequence[Attendance],
    petty: Sequence[PettyCash],
    workers: dict[int, User],
) -> bytes:
    """One month of everything, as three labelled blocks in one sheet.

    Not three files in a zip: this gets sent to an accountant over WhatsApp,
    and a zip on a phone is a support call. One sheet opens on a tap.

    Each block repeats its own header row, so a reader scrolling past a blank
    line always knows what the columns mean.
    """
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def name_of(worker_id: int) -> str:
        worker = workers.get(worker_id)
        return worker.name if worker else f"Worker #{worker_id}"

    writer.writerow([f"Cashbook monthly report - {month}"])
    writer.writerow([])

    writer.writerow(["TRANSACTIONS"])
    writer.writerow(["Date", "Worker", "Type", "Amount", "Signed amount", "Paid by", "Note"])
    for row in transactions:
        writer.writerow([
            row.date.isoformat(),
            name_of(row.worker_id),
            TYPE_LABELS[row.type],
            _amount(row.amount),
            _amount(row.amount * TYPE_SIGN[row.type]),
            row.payment_mode.value if row.payment_mode else "",
            row.note or "",
        ])
    writer.writerow([])

    writer.writerow(["ATTENDANCE"])
    writer.writerow(["Date", "Worker", "Status", "Shift", "Disputed", "Note"])
    for record in attendance:
        writer.writerow([
            record.date.isoformat(),
            name_of(record.worker_id),
            STATUS_LABELS[record.status],
            SHIFT_LABELS[record.shift],
            "yes" if record.is_disputed else "",
            record.note or "",
        ])
    writer.writerow([])

    writer.writerow(["PETTY CASH"])
    writer.writerow(["Date", "Site", "Amount", "Description"])
    for entry in petty:
        writer.writerow([
            entry.date.isoformat(),
            entry.site.name if entry.site else f"Site #{entry.site_id}",
            _amount(entry.amount),
            entry.description,
        ])

    return (BOM + buffer.getvalue()).encode("utf-8")
