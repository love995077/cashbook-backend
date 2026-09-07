"""End-to-end check of every multipart upload the mobile app makes.

Run from the project root:

    python scripts/smoke_uploads.py

Like scripts/smoke_phase5.py this builds its own in-memory SQLite engine and
overrides get_db, so it never touches the configured MySQL server. Media is
redirected to a temporary directory and deleted afterwards, so it leaves
nothing on disk either.

WHY THIS EXISTS
---------------
The mobile client used to hand the browser's FormData a
`{ uri, name, type }` object — the shape React Native's FormData understands
and streams off disk. The browser stringifies anything that is not a Blob, so
on the web build `image` arrived as ordinary form text and the route answered:

    image: Value error, Expected UploadFile, received: <class 'str'>

The second assertion below pins that exact failure so nobody "fixes" the route
into accepting a path string, which would store a filename instead of a photo.
The rest asserts the real file part still round-trips: contractor upload,
worker upload, listing, and the delete that removes the bytes.

All six uploads now build their part through `appendFile` in
mobile/src/api/upload.ts, so the other four are covered here too — with the
content types the WEB branch actually produces. That matters for audio: a
browser MediaRecorder labels its output "audio/webm;codecs=opus", parameters
and all, and the allow-list in app/services/storage.py is an exact-match dict.
It strips the parameters before looking up (`split(";")[0]`), which is the only
reason the web recording is storable at all — so it is asserted rather than
assumed.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date

# Point storage at a scratch directory BEFORE app.services.storage is imported:
# it reads settings at module import and caches the media root.
_media = tempfile.TemporaryDirectory()
from app.config import get_settings  # noqa: E402

get_settings().media_root = _media.name

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402  (registers the tables)
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def _db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _db
client = TestClient(app)

iso = date.today().isoformat()

# The smallest thing every decoder agrees is a PNG: 1x1, transparent.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)

r = client.post(
    "/auth/register",
    json={
        "name": "Ramesh Builders",
        "phone": "9990001111",
        "password": "secret123",
        "role": "admin",  # UserRole.CONTRACTOR is an alias of ADMIN
    },
)
assert r.status_code == 201, r.text
auth = {"Authorization": "Bearer " + r.json()["access_token"]}

r = client.post(
    "/contractor/sites", headers=auth, json={"name": "Tower B", "address": "MG Road"}
)
assert r.status_code == 201, r.text
site_id = r.json()["id"]

# 1. The shape the client sends now: a real file part.
r = client.post(
    f"/contractor/sites/{site_id}/photos",
    headers=auth,
    files={"image": ("slab.png", PNG, "image/png")},
    data={"date": iso, "description": "Slab poured, second floor"},
)
assert r.status_code == 201, r.text
photo = r.json()
assert photo["url"].endswith(".png"), photo["url"]
assert photo["date"] == iso, photo
assert photo["description"] == "Slab poured, second floor", photo
stored = Path(_media.name) / photo["url"].split("/media/", 1)[1]
assert stored.is_file() and stored.read_bytes() == PNG, stored
print("contractor upload ->", photo["url"])

# 2. The shape that broke: `image` as a plain string field.
r = client.post(
    f"/contractor/sites/{site_id}/photos",
    headers=auth,
    data={"image": "file:///var/mobile/.../IMG_0042.jpg", "date": iso},
)
assert r.status_code == 422, r.text
detail = r.json()["detail"]
assert any(
    item["loc"][-1] == "image" and "Expected UploadFile" in item["msg"] for item in detail
), detail
print("string image     -> 422", detail[0]["msg"])

# 3. A content type that is not on the storage allow-list.
r = client.post(
    f"/contractor/sites/{site_id}/photos",
    headers=auth,
    files={"image": ("payload.php", b"<?php ?>", "application/x-php")},
)
assert r.status_code == 415, r.text
print("bad media type   -> 415", r.json()["detail"])

# 4. The worker's own snap: no site id in the call, read off their record.
r = client.post(
    "/contractor/workers",
    headers=auth,
    json={
        "name": "Suresh Kumar",
        "phone": "9990002222",
        "password": "secret123",
        "daily_wage": "600.00",
        "site_id": site_id,
    },
)
assert r.status_code == 201, r.text

r = client.post(
    "/auth/login", json={"phone": "9990002222", "password": "secret123"}
)
assert r.status_code == 200, r.text
worker_auth = {"Authorization": "Bearer " + r.json()["access_token"]}

r = client.post(
    "/worker/site-photos",
    headers=worker_auth,
    files={"image": ("snap.png", PNG, "image/png")},
    data={"date": iso},
)
assert r.status_code == 201, r.text
snap = r.json()
print("worker snap      ->", snap["url"])

r = client.get("/worker/site-photos", headers=worker_auth)
assert r.status_code == 200, r.text
assert {row["id"] for row in r.json()} == {photo["id"], snap["id"]}, r.json()

r = client.get(f"/contractor/sites/{site_id}/photos", headers=auth)
assert r.status_code == 200, r.text
assert len(r.json()) == 2, r.json()

# 5. Deleting takes the bytes with it.
r = client.delete(f"/contractor/sites/{site_id}/photos/{photo['id']}", headers=auth)
assert r.status_code == 204, r.text
assert not stored.exists(), stored

r = client.get(f"/contractor/sites/{site_id}/photos", headers=auth)
assert [row["id"] for row in r.json()] == [snap["id"]], r.json()

# 6. The audio note, in the two shapes the two runtimes produce: an m4a from
#    the phone recorder, and a parameterised webm from the browser's.
for filename, content_type, expected in [
    ("note.m4a", "audio/m4a", ".m4a"),
    ("note.webm", "audio/webm;codecs=opus", ".webm"),
]:
    r = client.post(
        "/media/audio-notes",
        headers=worker_auth,
        files={"audio": (filename, b"not really audio, but bytes are bytes", content_type)},
    )
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["url"].endswith(expected), (content_type, note)
    # The parameters are stripped before the type is recorded.
    assert note["content_type"] == content_type.split(";")[0], note
    print(f"audio note       -> {content_type:24} {note['url']}")

# 7. The delivery challan and the diesel receipt: optional file parts that
#    travel alongside ordinary form fields.
r = client.post(
    f"/contractor/sites/{site_id}/material-receipts",
    headers=auth,
    data={
        "item_name": "Cement",
        "quantity_received": "2.500",
        "unit": "ton",
        "date": iso,
        "supplier": "Shree Traders",
    },
    files={"challan": ("challan.png", PNG, "image/png")},
)
assert r.status_code == 201, r.text
receipt = r.json()
assert receipt["challan_image_url"] and receipt["challan_image_url"].endswith(".png"), receipt
assert receipt["quantity_received"] == "2.500", receipt
print("challan          ->", receipt["challan_image_url"])

r = client.post(
    f"/contractor/sites/{site_id}/machinery",
    headers=auth,
    data={
        "machine_name": "JCB",
        "hours_operated": "6.500",
        "diesel_litres": "42.000",
        "fuel_cost": "3800.00",
        "date": iso,
    },
    files={"receipt": ("diesel.png", PNG, "image/png")},
)
assert r.status_code == 201, r.text
log = r.json()
assert log["receipt_image_url"] and log["receipt_image_url"].endswith(".png"), log
assert log["fuel_cost"] == "3800.00", log
print("fuel receipt     ->", log["receipt_image_url"])

# The same route with no file at all — the part is optional, and omitting it
# must not look like an empty upload.
r = client.post(
    f"/contractor/sites/{site_id}/machinery",
    headers=auth,
    data={"machine_name": "Mixer", "hours_operated": "3.000"},
)
assert r.status_code == 201, r.text
assert r.json()["receipt_image_url"] is None, r.json()
print("no receipt       -> 201, receipt_image_url null")

# 8. The voice entry. Its transcriber is a deliberate placeholder, so the most
#    this can prove is that the recording got PAST multipart validation — a 503
#    naming speech_to_text.py means the file part parsed and the crew scoping
#    ran. A 422 here would mean the upload itself was rejected.
r = client.post(
    "/api/voice-entry",
    headers=auth,
    files={"audio": ("entry.m4a", b"bytes standing in for a recording", "audio/m4a")},
    data={"language": "hi"},
)
assert r.status_code == 503, r.text
assert "speech_to_text" in r.json()["detail"], r.json()
print("voice entry      -> 503 (placeholder reached, upload validated)")

_media.cleanup()
print("\nall multipart uploads OK")
