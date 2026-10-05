"""Smoke-test a running deployment over HTTP.

    python scripts/smoke_deployed.py https://moneymate.lovee.dpdns.org

Creates a throwaway contractor (a random phone number) and exercises the
paths that matter: sign-up, a site, a worker, a day marked present earning its
wage, the books check, the site overview, a PDF and a photo upload. It writes
real rows - that throwaway account's own - so point it at a database where a
test account is acceptable, and delete the account afterwards if not.

Exits non-zero on the first thing that is wrong.
"""

from __future__ import annotations

import json
import random
import sys
import urllib.request


def call(base: str, method: str, path: str, token: str | None = None, body=None, raw=False, files=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if files is not None:
        boundary = "----moneymatesmoke"
        parts = []
        for name, (filename, content, mime) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n"
            )
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return response.status, (content if raw else (json.loads(content) if content else None))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def expect(ok: bool, what: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + what)
    if not ok:
        sys.exit(1)


def main(base: str) -> None:
    base = base.rstrip("/")
    status, body = call(base, "GET", "/health")
    expect(status == 200 and body == {"status": "ok"}, "health")

    phone = "7" + "".join(random.choice("0123456789") for _ in range(9))
    status, body = call(base, "POST", "/auth/register", body={"name": "Smoke Test", "phone": phone, "password": "smoke123", "role": "admin"})
    expect(status == 201, f"register throwaway contractor {phone}")
    token = body["access_token"]

    status, site = call(base, "POST", "/contractor/sites", token, {"name": "Smoke Site"})
    expect(status == 201, "create a site")
    status, worker = call(base, "POST", "/contractor/workers", token, {"name": "Smoke Worker", "daily_wage": "600.00", "site_id": site["id"]})
    expect(status == 201, "add a worker posted to it")

    status, mark = call(base, "POST", "/contractor/attendance/bulk", token, {"marks": [{"worker_id": worker["id"], "status": "present"}], "site_id": site["id"]})
    expect(status == 201 and mark["created"] == 1, "mark the day present")

    status, ledger = call(base, "GET", f"/contractor/workers/{worker['id']}/ledger", token)
    expect(status == 200 and ledger["balance"]["total_wages"] == "600.00", "the day earned its wage (600.00)")

    status, audit = call(base, "GET", "/contractor/integrity/wages", token)
    expect(status == 200 and audit["consistent"] is True, "books check: ledger matches attendance")

    status, cards = call(base, "GET", "/contractor/sites/overview", token)
    expect(status == 200 and cards[0]["wages_earned"] == "600.00", "site overview carries the wage")

    status, pdf = call(base, "GET", f"/api/reports/business/pdf?site_id={site['id']}", token, raw=True)
    expect(status == 200 and pdf.startswith(b"%PDF"), "site PDF report")

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4560000000049454e44ae426082"
    )
    status, photo = call(base, "POST", f"/contractor/sites/{site['id']}/photos", token, files={"image": ("smoke.png", png, "image/png")})
    expect(status == 201, "photo upload")
    status, _ = call(base, "GET", photo["url"].replace(base, "") if photo["url"].startswith(base) else photo["url"], raw=True)
    expect(status == 200, "uploaded photo is served back")

    print(f"\nAll good. Throwaway contractor: {phone} / smoke123 (site {site['id']}, worker {worker['id']}).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
