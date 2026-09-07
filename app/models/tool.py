"""Who has the drill.

A grinder walks off a site because nobody could say who took it out. These
two tables are the answer to that: a store of what the site owns, and a row
per checkout that stays open until the tool comes back.

`inventory_count` is how many the site holds in total, not how many are on
the shelf. Available = count - assignments still OUT, which is a grouped
query rather than a second column that drifts out of step with the
assignments the moment one is written by hand.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import ToolAssignmentStatus

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.user import User


class Tool(Base):
    """A tool the site keeps — "Bosch drill", "9in grinder"."""

    __tablename__ = "tools"
    __table_args__ = (
        # Two "Drill" rows at one site split the count and make "2 available"
        # unanswerable.
        UniqueConstraint("site_id", "name", name="uq_tools_site_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # How many the site holds. Three drills is one row with a count of 3
    # rather than three rows, because nobody labels them and the contractor
    # only ever asks "is one free".
    inventory_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="tools")
    assignments: Mapped[list["ToolAssignment"]] = relationship(
        back_populates="tool", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tool id={self.id} name={self.name!r} count={self.inventory_count}>"


class ToolAssignment(Base):
    """One tool, out with one worker, until it is back."""

    __tablename__ = "tool_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_id: Mapped[int] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Timestamps, not dates: a tool taken out at 9am and back at 4pm is the
    # whole point, and a date would flatten both ends of that to "today".
    checkout_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    return_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    status: Mapped[ToolAssignmentStatus] = mapped_column(
        SAEnum(
            ToolAssignmentStatus,
            values_callable=lambda e: [m.value for m in e],
            name="tool_assignment_status",
        ),
        nullable=False,
        default=ToolAssignmentStatus.OUT,
        index=True,
    )
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tool: Mapped["Tool"] = relationship(back_populates="assignments")
    worker: Mapped["User"] = relationship(foreign_keys=[worker_id])

    def __repr__(self) -> str:
        return (
            f"<ToolAssignment id={self.id} tool_id={self.tool_id} "
            f"worker_id={self.worker_id} status={self.status}>"
        )
