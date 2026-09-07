import enum


class UserRole(str, enum.Enum):
    """Who someone is, and therefore what they may do.

    ADMIN runs the business: the whole crew, every site, and all the money.
    SUPERVISOR runs one site for them - they mark attendance and receive
    material there and nowhere else, and they never see project finances.
    WORKER sees only themselves.

    CONTRACTOR IS AN ALIAS, NOT A FOURTH ROLE
    -----------------------------------------
    The role used to be `worker | contractor`. Phase 11 renamed contractor to
    admin, and `CONTRACTOR = "admin"` makes the old name a Python alias of the
    new member rather than a member of its own - `UserRole.CONTRACTOR is
    UserRole.ADMIN` is True. Every existing comparison keeps working while the
    stored value and the wire value are both "admin".

    This is deliberate and load-bearing: an enum rename that broke the
    fourteen places already comparing against CONTRACTOR would have been a
    rename plus fourteen chances to get an authorization check wrong.
    """

    WORKER = "worker"
    ADMIN = "admin"
    SUPERVISOR = "supervisor"

    # Alias of ADMIN. See above.
    CONTRACTOR = "admin"


class WorkerTrade(str, enum.Enum):
    """What a worker actually does on site.

    An enum rather than free text, for the same reason MaterialUnit is one:
    "electrician"/"Electrician"/"elec" would split one trade across three rows
    of any breakdown, and the app maps each value to a fixed icon. Adding a
    trade costs a migration - a deliberate trade for a list that stays
    countable.

    GENERAL_LABOUR is the catch-all, not the default: the column is nullable,
    so a worker whose trade was never recorded reads as unset rather than
    being silently filed as unskilled.
    """

    MASON = "mason"
    ELECTRICIAN = "electrician"
    PLUMBER = "plumber"
    CARPENTER = "carpenter"
    PAINTER = "painter"
    WELDER = "welder"
    BAR_BENDER = "bar_bender"  # steel fixer
    PLASTERER = "plasterer"
    TILER = "tiler"
    OPERATOR = "operator"  # mixer, JCB, hoist
    DRIVER = "driver"
    SUPERVISOR = "supervisor"
    GENERAL_LABOUR = "general_labour"


class PaymentMode(str, enum.Enum):
    """How money physically changed hands.

    Recorded so a disputed payment can be checked against something outside
    this app - a bank statement, a UPI history - which is exactly the argument
    a cash-only ledger cannot settle.
    """

    CASH = "cash"
    UPI = "upi"
    BANK_TRANSFER = "bank_transfer"


class AttendanceStatus(str, enum.Enum):
    """How one worker's day went.

    RAIN_OUT is not a flavour of ABSENT. The worker turned up and could not
    work, which is a different conversation about whether they get paid, and a
    different number when the contractor argues the schedule with the site
    owner. Folding it into "absent" would destroy that distinction the moment
    it is recorded.

    It carries no wage of its own: like every status here, pay follows from
    the `transactions` a contractor logs, not from attendance.
    """

    PRESENT = "present"
    HALF_DAY = "half_day"
    ABSENT = "absent"
    RAIN_OUT = "rain_out"


class WorkShift(str, enum.Enum):
    """Which shift a day was worked on.

    Orthogonal to AttendanceStatus, and deliberately not folded into it. A
    half day is a fraction of a day's PAY; a night shift is the same day's pay
    worked at a different time. Adding NIGHT to the status enum would make
    `days_worked` — which counts PRESENT as 1 and HALF_DAY as 0.5 — have to
    guess what a night is worth, and would break the moment someone works a
    half day at night.

    Both shifts are paid at the same standard daily rate. The column exists so
    the roster can be read back ("who was on nights last week"), not because
    it changes the arithmetic.

    NOT NULL and defaulted to DAY: every row that existed when this column
    arrived was worked in daylight, which is a fact rather than a guess.
    """

    DAY = "day"
    NIGHT = "night"


class AdvanceRequestStatus(str, enum.Enum):
    """Where a worker's request for an advance has got to.

    Terminal once decided: a rejected request is not re-opened, the worker
    raises a new one. That keeps the audit trail honest about what was asked
    and what was answered, instead of one row quietly changing its mind.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TransactionType(str, enum.Enum):
    WAGE = "wage"
    ADVANCE = "advance"
    PAYMENT = "payment"


class MaterialUnit(str, enum.Enum):
    """How a material is counted.

    An enum rather than free text so reports do not fragment across
    "bag"/"bags"/"Bags". Adding a unit means a migration — a deliberate
    trade for consistent totals.
    """

    BAG = "bag"
    TON = "ton"
    KG = "kg"
    LITRE = "litre"
    PIECE = "piece"
    CUBIC_FEET = "cubic_feet"
    CUBIC_METRE = "cubic_metre"
    BRASS = "brass"
    SQUARE_FEET = "square_feet"
    RUNNING_FEET = "running_feet"


class MaterialLogType(str, enum.Enum):
    """Direction of a material movement.

    `quantity` is always stored positive; this says which way it moved. Without
    it there is no way to compute stock on hand, which is the whole point of
    tracking inventory rather than just consumption.
    """

    CONSUMPTION = "consumption"  # taken out of site stock
    DELIVERY = "delivery"  # received into site stock


class CashDirection(str, enum.Enum):
    """Which way money moved through the contractor's own cash box.

    Worker wages already have their own direction encoded in TransactionType.
    This is for everything else: what the site owner pays in, and what the
    contractor spends on fuel, transport, materials and rentals.
    """

    IN = "in"  # received — site advance, client payment
    OUT = "out"  # spent — fuel, transport, materials, equipment rental


class WageType(str, enum.Enum):
    """How a worker's earnings are calculated.

    DAILY is the default and the overwhelming majority: turn up, get the day
    rate. PIECE_RATE is paid by output instead — 120 sq ft of tiles at ₹18 —
    which is how finishing trades are usually engaged, and the reason a
    P/A/H toggle is meaningless for a tiler.

    Not nullable and defaulted to DAILY: every existing worker was being paid
    by the day when this column was added, so that is a fact rather than a
    guess. Contractors have no wage type at all, and simply never read it.
    """

    DAILY = "daily"
    PIECE_RATE = "piece_rate"


class InvoiceStatus(str, enum.Enum):
    """Where a client bill has got to.

    DRAFT is a figure agreed on a phone call; SENT is paper that has left the
    building; PAID is money in. VOID rather than a delete, because an invoice
    that was sent and then cancelled is a thing the client remembers and will
    ask about.
    """

    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    VOID = "void"


class ToolAssignmentStatus(str, enum.Enum):
    """Where a tool that left the store has got to.

    OUT is the state the end-of-shift warning counts. LOST is terminal and
    kept apart from RETURNED because it is the one that costs money and the
    one a contractor needs to be able to list at the end of a job.
    """

    OUT = "out"
    RETURNED = "returned"
    LOST = "lost"
