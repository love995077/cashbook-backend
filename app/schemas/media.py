from pydantic import BaseModel


class UploadedMedia(BaseModel):
    """What an upload endpoint hands back.

    `path` is the value to send back when attaching this recording to a
    transaction, an advance request or a dispute. `url` is for playing it.
    Clients should treat `path` as opaque — it encodes who uploaded the file,
    and the attach endpoints check that.
    """

    path: str
    url: str
    content_type: str
    size_bytes: int
