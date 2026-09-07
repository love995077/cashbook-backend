from datetime import date as date_type
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AttendanceStatus, WorkShift


class AttendanceBase(BaseModel):
    worker_id: int = Field(gt=0)
    date: date_type
    status: AttendanceStatus
    # Day or night. Defaulted rather than required so every existing caller
    # keeps working and simply records a day shift.
    shift: WorkShift = WorkShift.DAY
    note: Optional[str] = Field(default=None, max_length=255)


class AttendanceCreate(AttendanceBase):
    pass


class AttendanceScan(BaseModel):
    """One QR scan at the gate.

    `worker_id` is whatever the camera decoded. `date` defaults to today and
    `status` to present, so the scanner screen can post the id alone.
    """

    worker_id: int = Field(gt=0)
    date: Optional[date_type] = None
    status: AttendanceStatus = AttendanceStatus.PRESENT
    shift: WorkShift = WorkShift.DAY


class DisputeRequest(BaseModel):
    """A worker flagging one attendance row, optionally with a voice note.

    A body rather than a bare query flag, because the recording is the point:
    a worker on a noisy site is far more likely to say what went wrong than to
    type it.
    """

    disputed: bool = True
    audio_note_path: Optional[str] = Field(default=None, max_length=255)


class AttendanceUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""

    date: Optional[date_type] = None
    status: Optional[AttendanceStatus] = None
    shift: Optional[WorkShift] = None
    note: Optional[str] = Field(default=None, max_length=255)


class AttendanceRead(AttendanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: Optional[int] = None
    audio_note_url: Optional[str] = None
    # Drives the yellow row in the contractor's list and the flag state in the
    # worker's history, so both screens read it off the same field.
    is_disputed: bool = False
    created_at: datetime
    updated_at: datetime
