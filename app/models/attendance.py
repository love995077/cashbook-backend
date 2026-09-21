from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import AttendanceStatus, WorkShift

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.transaction import Transaction
    from app.models.user import User


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        # One attendance record per worker per day.
        UniqueConstraint("worker_id", "date", name="uq_attendance_worker_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    # Where the day was worked. Recorded on the row rather than read from
    # the worker, because a worker can be moved between sites and past
    # attendance must keep saying where it actually happened.
    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[AttendanceStatus] = mapped_column(
        SAEnum(
            AttendanceStatus,
            values_callable=lambda e: [m.value for m in e],
            name="attendance_status",
        ),
        nullable=False,
    )
    # Day or night. Its own column rather than a status, because a night is
    # the same day's pay worked at a different time while a half day is a
    # different amount of pay — see WorkShift for why merging them breaks
    # `days_worked`. Indexed because the roster view filters on it.
    shift: Mapped[WorkShift] = mapped_column(
        SAEnum(WorkShift, values_callable=lambda e: [m.value for m in e], name="work_shift"),
        nullable=False,
        server_default=WorkShift.DAY.value,
        default=WorkShift.DAY,
        index=True,
    )

    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The WAGE `Transaction` this day earned, for a worker on a day rate.
    #
    # WHY THE LINK IS STORED RATHER THAN RECOMPUTED
    # ---------------------------------------------
    # Marking a day present is what earns a daily worker their wage, the same
    # way a piece-rate log earns one (`piece_rate_logs.transaction_id`). The
    # row has to be reversible: correcting a day to absent takes the wage
    # back, and unmarking it removes the wage entirely.
    #
    # Finding that wage by (worker_id, date) instead would be indistinguishable
    # from one the contractor typed by hand on the same day, so an edit could
    # silently delete money somebody entered deliberately. The explicit link is
    # the only thing that says "this row created that row".
    #
    # NULL is ordinary, not a gap: absent and rain-out earn nothing, a
    # piece-rate worker is paid by their log instead, and a worker with no
    # `daily_wage` set has no figure to earn.
    #
    # SET NULL rather than CASCADE - deleting the wage must not delete the
    # record that the worker turned up. `remove_transaction` refuses to delete
    # an attendance-backed wage anyway; correct the day instead.
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )

    # Raised by the worker when the row does not match the day they worked.
    # It does not change the status or the balance - it is a flag that says
    # "come and look at this", cleared by the contractor once settled in
    # person. Indexed because the contractor's view filters on it.
    is_disputed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    # The worker's spoken explanation of a dispute. Storage key, not a URL.
    audio_note_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker: Mapped["User"] = relationship(back_populates="attendances")
    transaction: Mapped[Optional["Transaction"]] = relationship()
    site: Mapped[Optional["Site"]] = relationship(back_populates="attendances")

    def __repr__(self) -> str:
        return (
            f"<Attendance id={self.id} worker_id={self.worker_id} "
            f"date={self.date} status={self.status}>"
        )
