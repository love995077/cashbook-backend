"""Speech-to-text seam.

PLACEHOLDER — `transcribe_audio` raises until a provider is wired up. The
router calls it through this module so a test (or a different provider) can
swap the implementation without touching the endpoint.

------------------------------------------------------------------------------
IMPLEMENTING THIS
------------------------------------------------------------------------------
The audio arrives as bytes from a phone: expo-audio records .m4a (AAC) on both
iOS and Android with the HIGH_QUALITY preset. Whisper accepts m4a directly, so
no transcoding is needed for the common path.

Option A — OpenAI's hosted Whisper (fastest to get working):

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio, content_type),
        language=language,          # "hi" — passing it beats autodetect on
                                    # short clips and avoids Hindi being read
                                    # as Urdu/Nepali
        temperature=0,
    )
    return result.text

Option B — Whisper running locally (no audio leaves the machine, which matters
for wage records; slower on CPU, needs ffmpeg on PATH):

    import whisper                       # openai-whisper
    model = whisper.load_model(settings.stt_model)   # "small" handles Hindi
    result = model.transcribe(str(path), language=language, fp16=False)
    return result["text"]

    faster-whisper is the better local option in practice — same accuracy,
    several times quicker, and it will run on CPU without much fuss.

------------------------------------------------------------------------------
THINGS THAT WILL BITE
------------------------------------------------------------------------------
* Code-switching. Real speech here is Hindi with English money words mixed in
  ("600 rupaye wage aur 200 advance"). Whisper handles this, but it may return
  Devanagari, Latin, or a mix of both, and the digits may come back as words
  ("chhe sau"). The intent parser downstream must not assume a script or that
  numerals are numeric — that is why parsing is an LLM step and not a regex.
* Numbers. "sau" (100), "hazaar" (1000), "dedh" (1.5), "sava"/"paune" (quarter
  past/less) all show up when people talk about money. An LLM handles these far
  better than any rule set.
* Silence and noise. A construction site is loud. Whisper hallucinates fluent
  text on near-silent input, so guard with a minimum duration on the client and
  treat a suspiciously short transcript as "not understood" rather than acting
  on it.
* Cost and latency. Hosted Whisper is roughly a second or two for a short clip
  plus upload time. If this is used dozens of times a day per contractor, a
  local model stops looking slow.
"""

from app.config import get_settings

settings = get_settings()


class SpeechToTextUnavailable(RuntimeError):
    """No transcription provider is configured, or the provider failed."""


async def transcribe_audio(
    audio: bytes,
    filename: str,
    content_type: str | None = None,
    language: str = "hi",
) -> str:
    """Return the spoken text.

    Args:
        audio: Raw bytes of the uploaded recording.
        filename: Original filename; providers use the extension to pick a demuxer.
        content_type: MIME type reported by the client, if any.
        language: ISO-639-1 code. Defaults to Hindi.

    Raises:
        SpeechToTextUnavailable: Always, until a provider is implemented.
    """
    raise SpeechToTextUnavailable(
        "Speech-to-text is not configured yet. Implement transcribe_audio() in "
        "app/services/speech_to_text.py and set STT_PROVIDER in .env."
    )
