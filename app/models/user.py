from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import UserRole, WageType

if TYPE_CHECKING:
    from app.models.advance_request import AdvanceRequest
    from app.models.attendance import Attendance
    from app.models.deduction import AutoDeduction
    from app.models.piece_rate import PieceRateLog
    from app.models.site import Site
    from app.models.transaction import Transaction


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Nullable: the only thing an admin must supply to put someone on the crew
    # is a name. A labourer taken on for a fortnight may not have a number worth
    # recording, and refusing to book them until one exists is how a crew list
    # stops matching the crew.
    #
    # Still UNIQUE, and both MySQL and PostgreSQL permit any number of NULLs in
    # a unique index, so the constraint keeps doing its real job — no two
    # accounts share a number — without forcing one to exist. A worker with no phone simply has no way to
    # sign in; they are a record their contractor keeps, not an account.
    phone: Mapped[Optional[str]] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )

    # bcrypt digest; never exposed through any schema. Nullable for the same
    # reason as `phone`: no PIN means no sign-in, not a rejected worker. The
    # login route refuses an empty digest outright rather than letting it reach
    # bcrypt, which would otherwise compare against nothing.
    password_hash: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, values_callable=lambda e: [m.value for m in e], name="user_role"),
        nullable=False,
        index=True,
    )

    # Daily wage rate for workers; contractors leave this empty.
    daily_wage: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)

    # How this worker's earnings are calculated. NOT NULL and defaulted to
    # DAILY, because every worker on the crew when this column arrived was
    # being paid by the day - that is a fact about them, not a guess.
    #
    # A PIECE_RATE worker still gets attendance marked (the site needs to know
    # who turned up) but their pay comes from `piece_rate_logs`, not from the
    # day rate above, so the attendance grid offers them quantity x rate
    # instead of a present/absent toggle.
    wage_type: Mapped[WageType] = mapped_column(
        SAEnum(WageType, values_callable=lambda e: [m.value for m in e], name="wage_type"),
        nullable=False,
        server_default=WageType.DAILY.value,
        default=WageType.DAILY,
        index=True,
    )

    # What the worker does on site — their post. Free text rather than an enum:
    # the thirteen values in `WorkerTrade` do not cover a real crew, which turns
    # up shuttering carpenters, chowkidars and bar-bending helpers, and a post
    # that needs a migration before it can be typed gets recorded as something
    # else or not at all.
    #
    # `WorkerTrade` survives as the catalogue of well-known posts: it still
    # drives the suggestion chips and the icons in the app. It is no longer a
    # constraint.
    #
    # Countability is kept by normalising on the way in — see `_normalise_trade`
    # in app/schemas/user.py. "Mason", "mason" and "MASON" all store as `mason`,
    # so a breakdown by post still groups, which was the original reason this
    # was an enum. Nullable: contractors do not have a post, and a crew that
    # predates this column reads as "not set" rather than being backfilled.
    trade: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)

    # Workers are linked to the contractor they work for.
    contractor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Which site this worker is currently assigned to. Optional: a worker can
    # be on the crew before a site exists, and contractors running a single job
    # never need sites at all.
    #
    # `use_alter`: users and sites reference each other, so neither CREATE
    # TABLE can carry this constraint - it is added by ALTER once both exist,
    # exactly as the initial migration does, and under the same name.
    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey(
            "sites.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_users_site_id_sites",
        ),
        nullable=True,
        index=True,
    )

    # Deactivation is the "remove from crew" of this app: the row and its
    # ledger survive, the worker drops out of the crew list and the
    # attendance grid. See `remove_worker`.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # The first day the worker no longer works here, set when they are
    # deactivated and cleared when they come back.
    #
    # A flag alone cannot answer "may this day be marked?". A worker who left
    # on the 20th worked the 18th, and the 18th may still need marking - back-
    # dated from a paper register, or replayed from a phone that was offline.
    # Refusing every mark for an inactive worker would lose those days; allowing
    # every mark would let a deactivated worker keep earning. So days BEFORE
    # this date may be marked and days on or after it are refused. NULL with
    # is_active false is a worker deactivated before this column existed, and
    # is treated as "no new days at all".
    deactivated_on: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contractor: Mapped[Optional["User"]] = relationship(
        remote_side=[id], back_populates="workers"
    )
    workers: Mapped[list["User"]] = relationship(back_populates="contractor")

    # Sites this user owns (contractors) and the site they work at (workers).
    sites: Mapped[list["Site"]] = relationship(
        back_populates="contractor",
        cascade="all, delete-orphan",
        foreign_keys="Site.contractor_id",
    )
    site: Mapped[Optional["Site"]] = relationship(
        back_populates="workers", foreign_keys=[site_id]
    )

    attendances: Mapped[list["Attendance"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )
    advance_requests: Mapped[list["AdvanceRequest"]] = relationship(
        back_populates="worker",
        cascade="all, delete-orphan",
        foreign_keys="AdvanceRequest.worker_id",
    )
    piece_rate_logs: Mapped[list["PieceRateLog"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )
    auto_deductions: Mapped[list["AutoDeduction"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} name={self.name!r} role={self.role}>"
