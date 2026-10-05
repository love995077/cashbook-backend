from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from the environment or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A full SQLAlchemy URL. When set it wins over the DB_* parts below, which
    # is how a container stack or a managed provider is pointed at: they hand
    # you one connection string rather than separate fields.
    #
    # PostgreSQL is the production engine. `postgres://` and `postgresql://`
    # (the forms providers print) are both accepted and mapped onto the
    # psycopg 3 driver - see `sqlalchemy_url`.
    #
    # MySQL is still understood, for the database the app is migrating away
    # from: `mysql+pymysql://...`. That URL must not carry `?ssl-mode=REQUIRED`
    # - a mysql-client option PyMySQL rejects outright. TLS for either engine
    # is configured through db_ssl / db_ssl_ca below and applied via
    # connect_args.
    database_url: str = ""

    # The parts, used only when database_url is empty.
    db_driver: str = "postgresql+psycopg"
    db_user: str = "postgres"
    db_password: str = ""
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "cash_book"
    db_echo: bool = False

    # Connection pool, per process. Every gunicorn worker opens its own, so
    # GUNICORN_WORKERS x (pool_size + max_overflow) must stay under the
    # server's max_connections (100 on a stock PostgreSQL).
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Connect over TLS. A managed MySQL requires it and always got it when a
    # database_url was supplied, which stays true for MySQL. PostgreSQL gets
    # TLS only when asked: the production database is a container on the same
    # private network as the API and does not offer it.
    db_ssl: bool = False
    # Path to the provider's CA certificate. Without it the connection is
    # still encrypted, but the server's certificate is not verified against a
    # known authority — set this for anything carrying real data.
    db_ssl_ca: str = ""

    # The calendar the business runs on. "Today" - the day attendance is
    # marked for, the newest date a ledger entry may carry, the end of a
    # payroll period - is a date in THIS zone, never the server's. A
    # container's clock is UTC, and an Indian site marking the morning's
    # attendance at 00:30-05:29 IST would otherwise be told the day had not
    # happened yet. See app/core/clock.py.
    business_timezone: str = "Asia/Kolkata"

    # Create the schema and tables on startup. Convenient in development;
    # switch OFF in production, where every gunicorn worker would otherwise
    # race to run CREATE DATABASE / create_all on boot. Run the schema step
    # once as a separate job instead (see docker-compose.yml).
    auto_create_tables: bool = True

    # Auth. jwt_secret MUST be overridden in production.
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # one week

    # Comma-separated list of allowed browser origins, or "*" for any.
    cors_origins: str = "*"

    # Voice entry (Phase 6). Both pipelines are placeholders until a
    # provider is implemented — see app/services/speech_to_text.py and
    # app/services/intent_parser.py.
    stt_provider: str = ""  # "openai" | "whisper_local" | "" (disabled)
    stt_model: str = "small"
    llm_provider: str = ""  # "" (disabled) until chosen
    llm_model: str = ""
    openai_api_key: str = ""

    # Media uploads (Phase 9): site progress photos and voice notes.
    #
    # The local backend writes under media_root and the app serves it at
    # media_url_prefix. That is real, working storage — but it is a DIRECTORY,
    # so it needs a persistent volume (see docker-compose.yml) and it does not
    # survive being scaled to a second container. Point media_backend at "s3"
    # once either of those matters; see app/services/storage.py.
    media_backend: str = "local"  # "local" | "s3" (s3 not implemented yet)
    media_root: str = "media"
    media_url_prefix: str = "/media"

    # A phone camera JPEG is 2-5 MB; the ceiling leaves room for a large one
    # without letting somebody fill the disk with a single request.
    max_image_bytes: int = 12 * 1024 * 1024
    # A voice note is a few seconds. Deliberately smaller than the voice-entry
    # limit, which allows a longer dictation.
    max_audio_note_bytes: int = 5 * 1024 * 1024

    # S3 / R2 / Spaces, read only when media_backend == "s3".
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""  # set for R2 / Spaces; blank for AWS
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_public_base_url: str = ""  # CDN or bucket URL used to build public links

    # Notifications (Phase 8). Placeholder until a provider is implemented
    # in app/services/notifications.py.
    notifications_enabled: bool = False
    notify_provider: str = ""  # "twilio" | "whatsapp_cloud" | "" (disabled)
    # An advance at or above this triggers a message. Default is roughly
    # five days at a typical day rate.
    large_advance_threshold: float = 3000.0
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""

    @property
    def uses_managed_url(self) -> bool:
        """True when a full connection string was supplied."""
        return bool(self.database_url)

    @property
    def sqlalchemy_url(self) -> str:
        """The URL the engine connects with."""
        if self.database_url:
            return normalise_database_url(self.database_url)
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def dialect(self) -> str:
        """'postgresql', 'mysql' or 'sqlite' - the engine, not the driver."""
        return dialect_of(self.sqlalchemy_url)

    @property
    def connect_args(self) -> dict[str, Any]:
        """Driver arguments, chiefly TLS. Shaped for whichever engine is in use.

        PyMySQL takes an `ssl` dict rather than a URL parameter: an empty dict
        turns TLS on without verifying the server certificate, adding "ca"
        verifies it against that authority. psycopg takes libpq's `sslmode`.

        The two engines differ on when TLS is assumed, deliberately. A managed
        MySQL URL always meant TLS and still does. PostgreSQL in this
        deployment is a container on the API's private network with no TLS
        listener, so it is encrypted only when `db_ssl` asks for it.
        """
        return connect_args_for(
            self.sqlalchemy_url, self.db_ssl, self.db_ssl_ca, managed=self.uses_managed_url
        )

    @property
    def media_path(self) -> Path:
        """media_root as an absolute path.

        Resolved against the project root rather than the current working
        directory, so `uvicorn` started from anywhere writes to one place.
        """
        root = Path(self.media_root)
        if not root.is_absolute():
            root = Path(__file__).resolve().parent.parent / root
        # Always resolved, even when already absolute: the storage backend's
        # path-traversal guard compares a resolved target against this root,
        # and an unresolved root (a symlink, or a Windows 8.3 short name)
        # would never compare equal and would reject every legitimate write.
        return root.resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def server_url(self) -> str:
        """Same server as sqlalchemy_url, but without a database selected.

        Used to create a local MySQL database if it does not exist yet. Only
        meaningful for a server you administer — a managed provider, or the
        PostgreSQL container (which creates its database from POSTGRES_DB),
        hands you a database that already exists.
        """
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}"
        )

    @property
    def safe_url(self) -> str:
        """sqlalchemy_url with the password removed, for logs and CLI output."""
        from sqlalchemy.engine import make_url

        try:
            return make_url(self.sqlalchemy_url).render_as_string(hide_password=True)
        except Exception:
            return "<unparseable database url>"


def normalise_database_url(url: str) -> str:
    """Map the URL forms providers print onto the drivers this app ships.

    `postgres://` (Heroku-style) and a bare `postgresql://` would both make
    SQLAlchemy reach for psycopg2, which is not installed; psycopg 3 is. A
    bare `mysql://` would reach for mysqlclient; PyMySQL is what is installed.
    Anything that already names a driver is left exactly as given.
    """
    for bare, driver in (
        ("postgres://", "postgresql+psycopg://"),
        ("postgresql://", "postgresql+psycopg://"),
        ("mysql://", "mysql+pymysql://"),
    ):
        if url.startswith(bare):
            return driver + url[len(bare) :]
    return url


def dialect_of(url: str) -> str:
    """The engine a URL points at, without the driver: 'postgresql' etc."""
    return normalise_database_url(url).split(":", 1)[0].split("+", 1)[0]


def connect_args_for(
    url: str, ssl_on: bool = False, ssl_ca: str = "", managed: bool = False
) -> dict[str, Any]:
    """Driver arguments for `url`. See `Settings.connect_args`.

    A module function rather than only a property so the migration tooling
    can build arguments for a URL that is not the app's own - the MySQL
    database being copied FROM, say - without inheriting the app's TLS rules
    for the other engine. Feeding PyMySQL's `ssl` dict to psycopg (or to
    `sqlite3.connect`) fails on connect, which is the trap this avoids.

    `managed` says the URL was handed over whole (DATABASE_URL) rather than
    composed from parts. For MySQL that has always meant a managed provider,
    and therefore TLS.
    """
    dialect = dialect_of(url)

    if dialect == "mysql":
        if not (ssl_on or managed):
            return {}
        ssl: dict[str, Any] = {}
        if ssl_ca:
            ssl["ca"] = ssl_ca
        return {"ssl": ssl}

    if dialect == "postgresql":
        args: dict[str, Any] = {
            # Fail fast rather than hang a gunicorn worker on an unreachable
            # database; pool_pre_ping turns the failure into a reconnect.
            "connect_timeout": 10,
        }
        if ssl_on:
            args["sslmode"] = "verify-full" if ssl_ca else "require"
            if ssl_ca:
                args["sslrootcert"] = ssl_ca
        return args

    if dialect == "sqlite":
        # One in-memory database shared across the threads TestClient and the
        # threadpool use.
        return {"check_same_thread": False}

    return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
