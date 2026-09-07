from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from the environment or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A full SQLAlchemy URL. When set it wins over the DB_* parts below, which
    # is how managed providers (Aiven, PlanetScale, RDS) are pointed at: they
    # hand you one connection string rather than separate fields.
    #
    # NOTE the URL must not carry `?ssl-mode=REQUIRED`. That is a mysql-client
    # option; PyMySQL rejects it outright ("unexpected keyword argument
    # 'ssl-mode'"). TLS is configured through db_ssl / db_ssl_ca below and
    # applied via connect_args.
    database_url: str = ""

    db_user: str = "root"
    db_password: str = ""
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "cash_book"
    db_echo: bool = False

    # Connect over TLS. Managed MySQL requires it; a local server usually is not
    # set up for it, so this defaults on only when a database_url is supplied.
    db_ssl: bool = False
    # Path to the provider's CA certificate (Aiven: "CA certificate" on the
    # service Overview page). Without it the connection is still encrypted, but
    # the server's certificate is not verified against a known authority — set
    # this for anything carrying real data.
    db_ssl_ca: str = ""

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
            return self.database_url
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def connect_args(self) -> dict[str, Any]:
        """Driver arguments, chiefly TLS.

        PyMySQL takes an `ssl` dict rather than a URL parameter. An empty dict
        turns TLS on without verifying the server certificate; adding "ca"
        verifies it against that authority.
        """
        if not (self.db_ssl or self.uses_managed_url):
            return {}

        ssl: dict[str, Any] = {}
        if self.db_ssl_ca:
            ssl["ca"] = self.db_ssl_ca
        return {"ssl": ssl}

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

        Used to create the database if it does not exist yet. Only meaningful
        for a server you administer — a managed provider hands you a database
        that already exists and an account without CREATE DATABASE rights.
        """
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
