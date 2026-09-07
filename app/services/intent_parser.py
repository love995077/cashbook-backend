"""Transcript -> structured transactions.

PLACEHOLDER — `parse_intent` raises until a provider is wired up.

------------------------------------------------------------------------------
WHY AN LLM AND NOT A REGEX
------------------------------------------------------------------------------
The input is code-switched Hindi/English in an unpredictable script, with money
words that are not digits:

    "Aaj Suresh ko 600 rupaye wage aur 200 rupaye advance diya"
    "सुरेश को छह सौ मज़दूरी दी"
    "kal Anita ko dedh hazaar diya"          -> 1500, and *yesterday*
    "Mohan ka hisaab kar diya, paune char sau"  -> 375, a payment

A rule engine covering that is a losing fight. An LLM with the crew list in the
prompt handles names, numbers and relative dates in one pass.

------------------------------------------------------------------------------
IMPLEMENTING THIS
------------------------------------------------------------------------------
Use structured/JSON output so the response parses deterministically rather than
being scraped out of prose. Every current provider supports this — OpenAI's
`response_format={"type": "json_schema", ...}`, Anthropic's tool-use, Gemini's
`response_schema`. Hand it `ParsedIntent.model_json_schema()` so the contract
lives in one place (app/schemas/voice.py) instead of being restated in a prompt.

Sketch:

    payload = await client.parse(
        model=settings.llm_model,
        system=build_system_prompt(workers, today),
        user=transcript,
        schema=ParsedIntent.model_json_schema(),
    )
    return ParsedIntent.model_validate(payload)

The prompt must carry:
  * The contractor's crew — id and name for each worker. The model returns
    worker_id, never a name, so nothing downstream has to fuzzy-match. Include
    only THIS contractor's workers: it keeps the prompt small and means a
    hallucinated id cannot point at someone else's worker (the endpoint
    re-checks ownership anyway — never trust the model for authorization).
  * Today's date, so "aaj"/"kal"/"parson" resolve. Note "kal" means both
    yesterday and tomorrow in Hindi; for a cashbook, past is the safe reading,
    and a future-dated wage should become a `problem`, not a silent guess.
  * The transaction vocabulary, because the words people use do not map
    one-to-one onto the enum:
        wage    - mazdoori, dihaadi, majuri, "kaam ke paise"
        advance - advance, peshgi, udhaar, "aage diya"
        payment - hisaab, chukta, "pura kar diya", settlement
  * An instruction to emit MULTIPLE entries when it hears multiple amounts,
    and to leave worker_id null (with a `problem`) rather than guessing when no
    name was spoken or two workers share a first name.

------------------------------------------------------------------------------
THE RULE THAT MATTERS
------------------------------------------------------------------------------
This writes money into a ledger that decides what a worker is paid. A wrong
guess is worse than a question. Make the model report uncertainty in
`problems` and let the endpoint route to confirmation — never let it round,
assume a worker, or invent an amount to produce a clean answer.
"""

from datetime import date as date_type

from app.models.user import User
from app.schemas.voice import ParsedIntent

from app.config import get_settings

settings = get_settings()


class IntentParserUnavailable(RuntimeError):
    """No LLM provider is configured, or the provider failed."""


async def parse_intent(
    transcript: str,
    workers: list[User],
    today: date_type,
    default_worker_id: int | None = None,
) -> ParsedIntent:
    """Turn a transcript into transactions to write.

    Args:
        transcript: Text from speech-to-text.
        workers: The calling contractor's crew — the only workers the model may
            reference, and the id/name pairs it resolves names against.
        today: Reference date for "aaj", "kal" and friends.
        default_worker_id: Who to attribute entries to when the speaker names
            nobody. Set when recording started from a specific worker's row;
            None when recording from the dashboard, in which case an unnamed
            worker is a problem to confirm, not a guess to make.

    Raises:
        IntentParserUnavailable: Always, until a provider is implemented.
    """
    raise IntentParserUnavailable(
        "Intent parsing is not configured yet. Implement parse_intent() in "
        "app/services/intent_parser.py and set LLM_PROVIDER in .env."
    )
