"""Progress photos and material receipts, filed against a site and a date.

One row per uploaded image. The image itself lives in storage (see
app/services/storage.py); this table holds the storage key, not the bytes and
not a URL — the reasoning is in that module's docstring.
"""

from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.user import User


class SitePhoto(Base):
    __tablename__ = "site_photos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Storage-relative key, e.g. "photos/7/2026/09/1f3c….jpg".
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)

    # The day the photo is ABOUT, which is not always the day it was uploaded:
    # a contractor with no signal at the site uploads yesterday's progress the
    # next morning, and the feed has to group it under the day it shows.
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # SET NULL, not CASCADE: removing a user must not delete the site's record
    # of what the job looked like.
    uploaded_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Kept so the app can render a gallery without fetching every image first,
    # and so an orphan sweep can report how much disk it is about to reclaim.
    content_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["Site"] = relationship(back_populates="photos")
    uploader: Mapped[Optional["User"]] = relationship(foreign_keys=[uploaded_by])

    def __repr__(self) -> str:
        return f"<SitePhoto id={self.id} site_id={self.site_id} date={self.date}>"
