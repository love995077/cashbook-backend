from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import PaymentMode
from app.schemas.transaction import TransactionRead


class PayrollWindow(BaseModel):
    """The period a settlement covers.

    `start_date` may be omitted, and usually is: the common request is "clear
    everything this worker is owed", which means the whole history rather than
    an arbitrary month. `end_date` defaults to today.
    """

    worker_id: int = Field(gt=0)
    start_date: Optional[date_type] = None
    end_date: Optional[date_type] = None

    @model_validator(mode="after")
    def _check_order(self) -> "PayrollWindow":
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class PayrollSummary(BaseModel):
    """What a worker is owed over one window, and the days behind it.

    Two figures, and the difference between them matters:

        window_balance = total_wages - total_advances - total_payments
                         over the requested dates alone

        net_payable    = min(window_balance, lifetime_balance)
                         what the contractor may actually hand over

    They are equal in the ordinary case. They diverge when an earlier
    settlement was dated outside the requested window - the wages show up
    inside it, the payment that already cleared them does not - and paying the
    window figure would pay the same money a second time. `net_payable` is the
    one to display and the one the settle endpoint writes.

    INSTALLMENT PLANS MOVE net_payable, NOT window_balance
    -----------------------------------------------------
    An advance being recovered through an `AutoDeduction` is still an ADVANCE
    row, so `total_advances` and `window_balance` include it in full - those
    two describe the ledger and must keep agreeing with the worker's own
    screen. `net_payable` is the figure that knows about the arrangement:

        net_payable = window_balance
                    + emi_advances          (put back - not due this period)
                    - installments_recovered (taken instead, day by day)

    capped, as ever, at the same adjustment applied to the lifetime balance.
    The two deliberately disagree while a plan is running; `emi_outstanding`
    is the gap, and is what the worker still owes.
    """

    worker_id: int
    worker_name: str
    start_date: Optional[date_type]
    end_date: date_type

    total_wages: Decimal
    total_advances: Decimal
    total_payments: Decimal
    # The window taken on its own, before the lifetime cap.
    window_balance: Decimal
    net_payable: Decimal

    # Attendance behind the wages, for the confirmation screen. These are
    # counts of marked days, not a second way to compute the money - wages
    # come from the ledger, which is the only place they are authoritative.
    days_present: int
    days_half: int
    days_absent: int
    # Days the crew turned up and the weather sent them home. Reported apart
    # from absences because it is a different argument about who pays.
    days_rain_out: int = 0
    # present + half/2, the number a contractor would say out loud.
    days_worked: Decimal

    # --- installment plans ---
    #
    # Advances inside the window that are being recovered day by day rather
    # than held against this period in full. Already counted in
    # total_advances above; net_payable adds them back.
    emi_advances: Decimal = Decimal("0.00")
    # Installments actually taken inside the window, which is what is held
    # against the period instead.
    installments_recovered: Decimal = Decimal("0.00")
    # Still to be recovered across every active plan, all time. Not part of
    # the window: it is the debt the worker carries into the next period.
    emi_outstanding: Decimal = Decimal("0.00")

    # The worker's position across their whole history, which is not the same
    # as the window unless the window covers everything.
    lifetime_balance: Decimal
    # False when there is nothing to pay, so the app can disable the button
    # instead of letting the request come back 409.
    can_settle: bool


class PayrollSettleRequest(PayrollWindow):
    payment_mode: PaymentMode = PaymentMode.CASH
    note: Optional[str] = Field(default=None, max_length=255)


class PayrollSettlement(BaseModel):
    """The result of clearing a worker's balance."""

    model_config = ConfigDict(from_attributes=True)

    summary: PayrollSummary
    # The clearing entry that was written.
    transaction: TransactionRead
    # The worker's lifetime balance after settling. Zero when the window
    # covered their whole history; otherwise whatever sits outside it.
    balance_after: Decimal
