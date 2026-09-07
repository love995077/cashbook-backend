"""Voice-driven cashbook entry.

The pipeline, end to end:

    phone records .m4a
        -> POST /api/voice-entry (multipart)
        -> transcribe_audio()   [placeholder: app/services/speech_to_text.py]
        -> parse_intent()       [placeholder: app/services/intent_parser.py]
        -> validate against THIS contractor's crew
        -> INSERT into transactions        <- implemented
        -> 200 with what was written

The two AI steps are seams: this module imports them by name so tests (and a
different provider later) can substitute them without the endpoint changing.
"""

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select

from app.dependencies import CurrentContractor, DbSession
from app.models.enums import UserRole
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.transaction import TransactionRead
from app.schemas.voice import ParsedEntry, VoiceEntryResponse
from app.services.intent_parser import IntentParserUnavailable, parse_intent
from app.services.ledger import calculate_balance
from app.services.notifications import notify_transaction, should_notify
from app.services.speech_to_text import SpeechToTextUnavailable, transcribe_audio

router = APIRouter(prefix="/api", tags=["voice"])

# A held-button recording is seconds long. Anything much larger is a mistake or
# an attempt to make the transcription bill interesting.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# expo-audio records m4a/aac; the rest are here so a web build or a future
# recorder does not need an endpoint change.
ALLOWED_CONTENT_TYPES = {
    "audio/m4a",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/ogg",
    "video/mp4",  # what iOS sometimes labels an m4a container
}

# Whisper will happily produce fluent text from a half-second of noise, so a
# transcript this short is treated as "nothing was said".
MIN_TRANSCRIPT_CHARS = 3


@router.post(
    "/voice-entry",
    response_model=VoiceEntryResponse,
    status_code=status.HTTP_200_OK,
    summary="Record cashbook entries from spoken Hindi",
)
async def voice_entry(
    contractor: CurrentContractor,
    db: DbSession,
    background: BackgroundTasks,
    audio: UploadFile = File(..., description="Recording, typically .m4a from expo-audio"),
    worker_id: int | None = Form(
        default=None,
        description=(
            "Who to attribute entries to when the speaker names nobody. Send it "
            "when recording was started from a specific worker's row."
        ),
    ),
    language: str = Form(default="hi", description="ISO-639-1 code for the spoken language"),
) -> VoiceEntryResponse:
    """Transcribe a recording, extract transactions, and write them.

    Returns 200 in three shapes (see VoiceEntryResponse.status): entries were
    written, entries need confirmation, or nothing usable was heard. Only real
    failures — bad upload, provider down — raise.
    """
    # --- 1. validate the upload -------------------------------------------
    if audio.content_type and audio.content_type.split(";")[0] not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio type: {audio.content_type}",
        )

    payload = await audio.read()
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The recording was empty"
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Recording is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )

    # --- 2. the contractor's crew -----------------------------------------
    # Loaded before transcription because it is both the prompt context for the
    # parser and the allow-list the results are checked against.
    crew = list(
        db.scalars(
            select(User)
            .where(User.contractor_id == contractor.id, User.role == UserRole.WORKER)
            .order_by(User.name)
        ).all()
    )
    if not crew:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a worker before recording entries",
        )

    crew_ids = {worker.id for worker in crew}
    if worker_id is not None and worker_id not in crew_ids:
        # Same 404-not-403 treatment as the rest of the contractor routes: do
        # not confirm that another contractor's worker exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")

    # --- 3. speech to text [PLACEHOLDER] ----------------------------------
    try:
        transcript = (
            await transcribe_audio(
                audio=payload,
                filename=audio.filename or "recording.m4a",
                content_type=audio.content_type,
                language=language,
            )
        ).strip()
    except SpeechToTextUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        return VoiceEntryResponse(
            status="not_understood",
            transcript=transcript,
            message="I could not hear anything. Please try again.",
        )

    # --- 4. transcript to intent [PLACEHOLDER] ----------------------------
    today = date_type.today()
    try:
        intent = await parse_intent(
            transcript=transcript,
            workers=crew,
            today=today,
            default_worker_id=worker_id,
        )
    except IntentParserUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # --- 5. validate what came back ---------------------------------------
    # The model is untrusted input. Everything it produced is re-checked here:
    # a hallucinated worker id, a negative amount or a future date must not
    # reach the ledger.
    ready: list[ParsedEntry] = []
    problems: list[str] = list(intent.problems)

    for entry in intent.entries:
        resolved_id = entry.worker_id or worker_id

        if resolved_id is None:
            problems.append(
                f"Who was the {entry.type.value} of {entry.amount} for? No name was mentioned."
            )
            ready.append(entry)
            continue

        if resolved_id not in crew_ids:
            problems.append(f"Worker {resolved_id} is not on your crew.")
            continue

        if entry.amount <= Decimal("0"):
            problems.append(f"Ignored an entry with a non-positive amount ({entry.amount}).")
            continue

        if entry.date > today:
            problems.append(f"Ignored an entry dated in the future ({entry.date}).")
            continue

        ready.append(entry.model_copy(update={"worker_id": resolved_id}))

    if not ready:
        return VoiceEntryResponse(
            status="not_understood",
            transcript=transcript,
            message="I heard you, but could not find a transaction in that.",
            problems=problems,
        )

    # Anything unresolved sends the whole utterance to confirmation rather than
    # writing half of it. Money entries should be all-or-nothing: a contractor
    # who said two things and saw one recorded has no way to tell which.
    if problems or any(entry.worker_id is None for entry in ready):
        return VoiceEntryResponse(
            status="needs_confirmation",
            transcript=transcript,
            message="Please check these before I save them.",
            pending=ready,
            problems=problems,
        )

    # --- 6. write to the database -----------------------------------------
    # One transaction for the whole utterance: either every entry lands or none
    # does, so "600 wage and 200 advance" can never be half-recorded.
    created = [
        Transaction(
            worker_id=entry.worker_id,
            type=entry.type,
            amount=entry.amount,
            date=entry.date,
            note=entry.note or _default_note(transcript),
        )
        for entry in ready
    ]

    db.add_all(created)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    for row in created:
        db.refresh(row)

    # Spoken entries notify on the same rules as typed ones.
    by_id = {worker.id: worker for worker in crew}
    for row in created:
        if should_notify(row):
            worker = by_id[row.worker_id]
            balance = calculate_balance(db, worker.id).balance
            background.add_task(notify_transaction, row, worker, contractor, balance)

    return VoiceEntryResponse(
        status="recorded",
        transcript=transcript,
        message=_summarise(created, crew),
        created=[TransactionRead.model_validate(row) for row in created],
    )


def _default_note(transcript: str) -> str:
    """Keep what was said, so a surprising row can be traced back to the words."""
    note = f"Voice: {transcript}"
    return note[:255]


def _summarise(created: list[Transaction], crew: list[User]) -> str:
    """A one-line confirmation the app can read back out loud."""
    names = {worker.id: worker.name.split(" ")[0] for worker in crew}
    parts = [
        f"{row.type.value} of {row.amount} for {names.get(row.worker_id, 'worker')}"
        for row in created
    ]
    return "Recorded " + ", ".join(parts) + "."
