"""Gunicorn configuration for the Cashbook API.

Gunicorn supervises the processes; each worker runs uvicorn's ASGI loop.
Everything here can be overridden by environment variable, so a container can
be retuned without a rebuild.
"""

import multiprocessing
import os

# --- where it listens --------------------------------------------------------
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# --- workers -----------------------------------------------------------------
worker_class = "uvicorn.workers.UvicornWorker"


def _default_workers() -> int:
    """(2 x cores) + 1, the usual starting point, capped at 8.

    The cap matters here: every worker opens its own SQLAlchemy pool, so worker
    count multiplies straight into MySQL connections. With the default pool
    settings, 8 workers can hold 8 x (pool_size + max_overflow) connections —
    check `max_connections` on the server before raising this.
    """
    return min((multiprocessing.cpu_count() * 2) + 1, 8)


workers = int(os.getenv("GUNICORN_WORKERS", _default_workers()))

# Recycle workers periodically so a slow leak cannot accumulate. The jitter
# stops every worker restarting at the same moment.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", 100))

# --- timeouts ----------------------------------------------------------------
# Generous, because /api/voice-entry waits on speech-to-text and an LLM call.
# Lower it once those move to a background job.
timeout = int(os.getenv("GUNICORN_TIMEOUT", 120))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", 30))

# Must exceed the idle timeout of anything in front (nginx, ALB) so the
# connection is closed at this end first.
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", 5))

# --- logging -----------------------------------------------------------------
# To stdout/stderr: the container runtime owns log collection.
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "-")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "-")
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

# %({x-forwarded-for}i)s is the real client when running behind a proxy;
# %(h)s would only ever show the proxy.
access_log_format = os.getenv(
    "GUNICORN_ACCESS_LOG_FORMAT",
    '%({x-forwarded-for}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s',
)

# --- process naming ----------------------------------------------------------
proc_name = "cashbook-api"

# Preloading would share the app across workers and save memory, but it also
# runs the FastAPI lifespan before forking, which would open a database engine
# in the parent and hand copies of its sockets to every child.
preload_app = False


def on_starting(server):
    server.log.info("Cashbook API starting with %s worker(s)", workers)


def worker_int(worker):
    worker.log.info("Worker %s interrupted, shutting down", worker.pid)
