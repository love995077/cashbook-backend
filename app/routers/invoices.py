"""Client invoices: money asked for, which is not the same as money received.

Everything else in this schema records what already happened. `cash_entries`
are payments the site owner has ALREADY made; an invoice is a bill that has
gone out and not been answered. Keeping them apart is what makes
"outstanding" a question with an answer.

ADMIN ONLY, ALL OF IT
---------------------
`CurrentContractor` resolves to the ADMIN role. Billing the client is the
clearest case of project finance there is, so a supervisor never reaches this
router — not the preview, not the list, not the PDF.

WHY THE TOTAL IS TYPED RATHER THAN COMPUTED
-------------------------------------------
`GET /preview` adds up what a site cost. `POST /` takes the number the client
is actually being asked for, and does not check one against the other. An
invoice carries markup, retention, a rounding, a discount agreed on the
phone, and none of those live in this database. See the note in
`app/schemas/invoice.py`.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.clock import business_today
from app.dependencies import CurrentContractor, DbSession, require_own_site
from app.models.cash import CashCategory, CashEntry
from app.models.enums import CashDirection, InvoiceStatus
from app.models.inventory import MaterialReceipt
from app.models.invoice import Invoice
from app.models.machinery import MachineryLog
from app.models.piece_rate import PieceRateLog
from app.models.site import Site
from app.models.user import User
from app.schemas.invoice import (
    InvoiceCreate,
    InvoiceLine,
    InvoicePreview,
    InvoiceRead,
    InvoiceUpdate,
)
from app.services.ledger import CENTS
from app.services.pdf_report import build_invoice
from app.services.storage import (
    StorageError,
    delete_stored,
    public_url,
    store_upload,
)

router = APIRouter(prefix="/contractor/invoices", tags=["invoices"])

ZERO = Decimal("0")


def _read(invoice: Invoice) -> InvoiceRead:
    read = InvoiceRead.model_validate(invoice)
    read.site_name = invoice.site.name if invoice.site else None
    read.pdf_url = public_url(invoice.pdf_path)
    return read


def _get_own_invoice(db: Session, contractor: User, invoice_id: int) -> Invoice:
    """Someone else's invoice is a 404, proved through the site it bills for."""
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found"
        )
    require_own_site(db, contractor, invoice.site_id)
    return invoice


def _build_preview(
    db: Session,
    site: Site,
    start_date: Optional[date_type],
    end_date: date_type,
) -> InvoicePreview:
    """Add up what one site cost over a window, as billable lines.

    Three sources, each grouped in the database rather than in Python so the
    figure does not depend on how many rows the screen happened to draw:

      * piece-rate work, by what was done — the labour that was billed by
        output, which is the part of a bill a client can actually check
      * cash paid out at this site, by category — materials, transport,
        rentals, everything the cash box spent on the job
      * machinery fuel, by machine

    Daily wages are deliberately absent. A day rate is what the contractor
    pays their crew, not what the client is charged for it, and putting it on
    a bill would publish the contractor's margin to the person paying it.
    """
    lines: list[InvoiceLine] = []

    def window(column):
        clauses = [column <= end_date]
        if start_date is not None:
            clauses.append(column >= start_date)
        return clauses

    # --- piece-rate work ---------------------------------------------------
    piece_rows = db.execute(
        select(
            PieceRateLog.work_description,
            func.sum(PieceRateLog.quantity),
            func.sum(PieceRateLog.total_amount),
        )
        .where(PieceRateLog.site_id == site.id, *window(PieceRateLog.date))
        .group_by(PieceRateLog.work_description)
        .order_by(func.sum(PieceRateLog.total_amount).desc())
    ).all()

    piece_rate_total = ZERO
    for description, quantity, amount in piece_rows:
        amount = amount or ZERO
        piece_rate_total += amount
        quantity = quantity or ZERO
        lines.append(
            InvoiceLine(
                kind="piece_rate",
                label=description,
                quantity=quantity,
                # The effective rate across the period, which is not
                # necessarily any single log's rate — a description billed at
                # two rates averages here, and the quantity beside it says so.
                # Quantised because an unrounded division would put fifteen
                # decimal places on a printed invoice line.
                rate=(amount / quantity).quantize(CENTS) if quantity else None,
                amount=amount,
            )
        )

    # --- what the cash box spent here --------------------------------------
    cash_rows = db.execute(
        select(CashCategory.name, func.sum(CashEntry.amount))
        .join(CashCategory, CashCategory.id == CashEntry.category_id, isouter=True)
        .where(
            CashEntry.site_id == site.id,
            CashEntry.direction == CashDirection.OUT,
            *window(CashEntry.date),
        )
        .group_by(CashCategory.name)
        .order_by(func.sum(CashEntry.amount).desc())
    ).all()

    materials_total = ZERO
    for name, amount in cash_rows:
        amount = amount or ZERO
        materials_total += amount
        lines.append(
            InvoiceLine(
                kind="materials",
                label=name or "Uncategorised",
                amount=amount,
            )
        )

    # --- plant and diesel --------------------------------------------------
    machine_rows = db.execute(
        select(
            MachineryLog.machine_name,
            func.sum(MachineryLog.diesel_litres),
            func.sum(MachineryLog.fuel_cost),
        )
        .where(MachineryLog.site_id == site.id, *window(MachineryLog.date))
        .group_by(MachineryLog.machine_name)
        .order_by(func.sum(MachineryLog.fuel_cost).desc())
    ).all()

    machinery_total = ZERO
    for name, litres, cost in machine_rows:
        cost = cost or ZERO
        machinery_total += cost
        lines.append(
            InvoiceLine(
                kind="machinery",
                label=f"{name} - fuel",
                quantity=litres or ZERO,
                unit="L",
                amount=cost,
            )
        )

    receipts = db.scalar(
        select(func.count())
        .select_from(MaterialReceipt)
        .where(MaterialReceipt.site_id == site.id, *window(MaterialReceipt.date))
    )

    return InvoicePreview(
        site_id=site.id,
        site_name=site.name,
        start_date=start_date,
        end_date=end_date,
        piece_rate_total=piece_rate_total,
        materials_total=materials_total,
        machinery_total=machinery_total,
        suggested_total=piece_rate_total + materials_total + machinery_total,
        lines=lines,
        material_receipt_count=receipts or 0,
    )


# --- the calculation --------------------------------------------------------
#
# Declared before /{invoice_id} so "preview" is matched as a path rather than
# parsed as an id and answered with a 422.


@router.get("/preview", response_model=InvoicePreview)
def preview_invoice(
    contractor: CurrentContractor,
    db: DbSession,
    site_id: int = Query(..., gt=0),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
) -> InvoicePreview:
    """What this site cost over a window, ready to be billed.

    Reads only — nothing is written, so the admin can move the dates around
    and watch the figure change before committing to one.
    """
    site = require_own_site(db, contractor, site_id)
    until = end_date or business_today()
    if start_date is not None and start_date > until:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The start of the period cannot fall after its end",
        )
    return _build_preview(db, site, start_date, until)


# --- the invoices themselves ------------------------------------------------


@router.get("", response_model=list[InvoiceRead])
def list_invoices(
    contractor: CurrentContractor,
    db: DbSession,
    site_id: Optional[int] = Query(default=None, gt=0),
    invoice_status: Optional[InvoiceStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[InvoiceRead]:
    """This contractor's invoices, newest first."""
    if site_id is not None:
        require_own_site(db, contractor, site_id)
        site_ids = [site_id]
    else:
        site_ids = list(
            db.scalars(select(Site.id).where(Site.contractor_id == contractor.id)).all()
        )
    if not site_ids:
        return []

    stmt = (
        select(Invoice)
        .options(joinedload(Invoice.site))
        .where(Invoice.site_id.in_(site_ids))
    )
    if invoice_status is not None:
        stmt = stmt.where(Invoice.status == invoice_status)

    invoices = db.scalars(stmt.order_by(Invoice.id.desc()).limit(limit)).all()
    return [_read(invoice) for invoice in invoices]


@router.post("", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
def raise_invoice(
    payload: InvoiceCreate, contractor: CurrentContractor, db: DbSession
) -> InvoiceRead:
    """Raise a bill against a site.

    `total_amount` is taken as given rather than recomputed from the preview.
    See the module docstring for why.
    """
    require_own_site(db, contractor, payload.site_id)

    invoice = Invoice(
        site_id=payload.site_id,
        client_name=payload.client_name,
        total_amount=payload.total_amount,
        status=payload.status,
        note=payload.note,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return _read(invoice)


@router.patch("/{invoice_id}", response_model=InvoiceRead)
def edit_invoice(
    invoice_id: int,
    payload: InvoiceUpdate,
    contractor: CurrentContractor,
    db: DbSession,
) -> InvoiceRead:
    """Move an invoice along, or correct one that has not gone out yet.

    The amount is only editable while the invoice is a DRAFT. Once it has been
    SENT the client is holding a piece of paper with a number on it, and
    silently restating that number here would leave the two disagreeing with
    no record that they ever did. Cancel with VOID and raise a new one.
    """
    invoice = _get_own_invoice(db, contractor, invoice_id)
    fields = payload.model_dump(exclude_unset=True)

    restating = {"total_amount", "client_name"} & fields.keys()
    if restating and invoice.status is not InvoiceStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This invoice has already left the building. Void it and raise a "
                "new one rather than changing what the client was billed."
            ),
        )

    for field, value in fields.items():
        setattr(invoice, field, value)

    db.commit()
    db.refresh(invoice)
    return _read(invoice)


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_invoice(
    invoice_id: int, contractor: CurrentContractor, db: DbSession
) -> Response:
    """Delete a draft. Anything that has been sent is voided, never deleted.

    An invoice the client has seen is a thing they remember and will ask
    about, so it stays on the books with a VOID against it.
    """
    invoice = _get_own_invoice(db, contractor, invoice_id)

    if invoice.status is not InvoiceStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This invoice is marked {invoice.status.value} and cannot be "
                "deleted. Set its status to void instead."
            ),
        )

    path = invoice.pdf_path
    db.delete(invoice)
    db.commit()
    delete_stored(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the paper ---------------------------------------------------------------


@router.get(
    "/{invoice_id}/pdf",
    response_class=Response,
    summary="Render the invoice as a PDF and keep a copy",
)
def invoice_pdf(
    invoice_id: int,
    contractor: CurrentContractor,
    db: DbSession,
    start_date: Optional[date_type] = Query(
        default=None, description="Start of the basis breakdown"
    ),
    end_date: Optional[date_type] = Query(default=None),
) -> Response:
    """Build the bill, store it against the invoice, and return the bytes.

    Rendered on demand rather than at creation: a draft is a figure agreed on
    a phone call, and most of them are edited before any paper is wanted.

    The stored copy is replaced on every call — `pdf_path` is a convenience
    for re-sharing the last one built, not an archive of every version. A
    storage failure is swallowed on purpose: the admin asked for a PDF, and
    handing them one that could not be filed beats handing them an error.
    """
    invoice = _get_own_invoice(db, contractor, invoice_id)
    site = db.get(Site, invoice.site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Site not found"
        )

    until = end_date or business_today()
    preview = _build_preview(db, site, start_date, until)

    today = business_today()
    pdf = build_invoice(
        invoice=invoice,
        site=site,
        contractor=contractor,
        preview=preview,
        generated_on=today,
    )

    previous = invoice.pdf_path
    try:
        stored = store_upload(
            pdf,
            kind="documents",
            owner_id=contractor.id,
            content_type="application/pdf",
            label=f"invoice {invoice.id} {invoice.client_name}",
        )
    except StorageError:
        stored = None
    else:
        invoice.pdf_path = stored.path
        db.commit()
        # Only after the row points at the new file: deleting first and then
        # failing to commit would leave the invoice pointing at nothing.
        if previous and previous != stored.path:
            delete_stored(previous)

    name = f"invoice-{invoice.id}-{invoice.client_name}.pdf"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "Content-Length": str(len(pdf)),
            "Cache-Control": "no-store",
        },
    )
