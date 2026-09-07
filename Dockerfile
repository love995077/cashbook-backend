# syntax=docker/dockerfile:1

# Two stages so the compiler toolchain used to install dependencies never ships
# in the runtime image.
#
# Python 3.13 rather than 3.14: bcrypt, cryptography and reportlab all publish
# cp313 wheels, so the install is pure download. On 3.14 a missing wheel falls
# back to building from source, and bcrypt needs a Rust toolchain to do that.
ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------- builder ---
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Installed into a virtualenv so the whole dependency tree can be copied to the
# runtime stage as one directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own so the layer is reused whenever only source code changes.
COPY requirements.txt .
RUN pip install --require-hashes=false -r requirements.txt \
    && pip install "gunicorn>=23.0.0"

# ---------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Runs as an unprivileged user: a container process that does not need root
# should not have it.
RUN groupadd --system --gid 1001 cashbook \
    && useradd --system --uid 1001 --gid cashbook --create-home cashbook

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=cashbook:cashbook app/ ./app/
COPY --chown=cashbook:cashbook scripts/ ./scripts/
COPY --chown=cashbook:cashbook gunicorn.conf.py ./

USER cashbook

EXPOSE 8000

# Uses the app's own liveness route. Written with urllib so the image does not
# need curl installed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Gunicorn supervises uvicorn workers: process management and graceful restarts
# from gunicorn, the ASGI event loop from uvicorn. Configuration lives in
# gunicorn.conf.py so it can be tuned without rebuilding.
CMD ["gunicorn", "app.main:app", "--config", "gunicorn.conf.py"]
