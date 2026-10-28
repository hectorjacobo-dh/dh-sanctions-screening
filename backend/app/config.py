"""Central configuration.

Every value is overridable through environment variables or a .env file so that
no credential or environment-specific path is ever committed to the repository.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # Absolute paths, NOT a bare ".env".
    #
    # A relative env_file resolves against the process working directory, which
    # differs between `uvicorn` run from backend/, the autostart task, and a
    # container. A .env placed at the project root — where .env.example lives
    # and where the README says to put it — was silently ignored, leaving the
    # app in no-login mode while appearing configured. A security setting that
    # fails to apply without saying so is worse than one that is absent.
    #
    # Root wins; backend/.env is accepted as a fallback. Both are logged at
    # startup via describe_config_sources().
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", BASE_DIR / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DH Group Sanctions Screening Engine"

    # Local timezone for display and for the human-readable report columns.
    # Storage, the audit hash chain and the authoritative report columns remain
    # UTC: the chain's canonical serialisation depends on it, and a UTC stamp is
    # what survives an audit across daylight-saving boundaries.
    display_timezone: str = "Europe/Berlin"

    # --- Persistence -----------------------------------------------------
    # SQLite by default for a self-contained local run. Point DATABASE_URL at
    # PostgreSQL for any shared/multi-user deployment.
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'screening.db').as_posix()}"
    data_dir: Path = BASE_DIR / "data"

    # Evidence storage. "local" writes to data/evidence (workstation, VM).
    # "gcs" writes to a bucket — required on any platform with an ephemeral
    # filesystem (Cloud Run, App Runner, Kubernetes without a volume), where
    # local files vanish on restart or scale-to-zero.
    evidence_backend: str = "local"
    evidence_bucket: str = ""
    evidence_prefix: str = "evidence"

    # --- Security --------------------------------------------------------
    # MUST be set to a strong random value in any non-local deployment.
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str = "CHANGE-ME-INSECURE-LOCAL-DEV-KEY"
    access_token_ttl_minutes: int = 8 * 60
    # Comma-separated origins allowed to call the API.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Bootstrap administrator created on first start when no users exist.
    bootstrap_admin_email: str = "admin@localhost"
    bootstrap_admin_password: str = ""  # empty -> a random one is printed at startup

    # Which address the server listens on.
    #   127.0.0.1 -> this machine only (safe default for local dev)
    #   0.0.0.0   -> reachable from the network (safe in containerized deployments)
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000

    # Networks permitted to reach the service, as comma-separated CIDRs.
    # Loopback is always allowed, so the operator's own use works anywhere.
    #
    # Defaults to the DH corporate range only. A laptop on café, hotel or home
    # Wi-Fi therefore serves nobody but itself, even while bound to 0.0.0.0 —
    # an internal compliance tool should not be discoverable on a stranger's
    # network. This is defence in depth, not authentication: a private address
    # is not proof of being inside DH.
    allowed_networks: str = "10.0.0.0/8"
    network_guard_enabled: bool = True

    # Single-operator local mode: skips the login screen entirely and attributes
    # every audit event to `local_operator`.
    #
    # Only valid while bound to loopback. Combining it with a network bind is
    # refused at startup in main.py, not merely warned about — it would hand
    # full admin to anyone who can reach the port.
    single_user_mode: bool = True
    local_operator: str = "local.operator@deliveryhero.com"

    # --- Matching engine -------------------------------------------------
    # Subject types screened by default. Vessels and aircraft are ingested and
    # remain fully searchable, but are excluded from batch screening: matching
    # restaurant legal names against ship names produces almost pure noise.
    # Add "Vessel,Aircraft" to screen them.
    screen_subject_types: str = "Individual,Entity,Unknown"

    match_threshold: int = 80          # % similarity required to raise an alert
    strong_match_threshold: int = 95   # % at/above which we band as "Strong"
    # Points deducted from the DISPLAYED score when the partner's country differs from
    # the designation's. Off by default: it never decided whether an alert was raised
    # (that is the name score alone), it only reordered the list and made a 95%
    # screen display "85". The country is shown as its own label instead.
    geo_contradiction_penalty: int = 0
    max_candidates_per_name: int = 400
    # Tokens appearing in more than this share of watchlist targets are too
    # common to be useful for blocking (e.g. "AL", "MOHAMMED", "COMPANY").
    blocking_max_token_document_frequency: float = 0.02

    # --- Watchlist sources ----------------------------------------------
    ofac_sdn_url: str = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    ofac_cons_url: str = (
        "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv"
    )
    eu_fsf_url: str = (
        "https://webgate.ec.europa.eu/fsd/fsf/public/files/"
        "csvFullSanctionsList/content?token=dG9rZW4tMjAxNw"
    )
    # FCDO UK Sanctions List. NOTE: the OFSI Consolidated List closed on
    # 28 January 2026 and is no longer updated — the UKSL is now the single
    # source for all UK designations, keyed on "Unique ID" rather than
    # "OFSI Group ID". Verified live 2026-09-09.
    uk_ofsi_url: str = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"
    # UN Security Council consolidated list. The legacy scsanctions.un.org host
    # now 404s; this is the current blob endpoint. Verified live 2026-09-09.
    un_consolidated_url: str = (
        "https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/EN/consolidated.xml"
    )

    http_timeout_seconds: int = 180
    # Cron-style refresh: every day at 03:15 UTC.
    refresh_cron_hour: int = 3
    refresh_cron_minute: int = 15
    # Refresh in the background at startup when the active snapshot is older
    # than this many hours. This is what makes "open the page and the lists
    # update themselves" work. Set to 0 to refresh on every start.
    refresh_if_stale_hours: int = 24
    refresh_on_startup: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def screen_subject_type_list(self) -> list[str]:
        return [s.strip() for s in self.screen_subject_types.split(",") if s.strip()]


def describe_config_sources() -> str:
    """Which .env files exist, for the startup banner."""
    candidates = [BASE_DIR / ".env", BASE_DIR / "backend" / ".env"]
    found = [str(p) for p in candidates if p.is_file()]
    return ", ".join(found) if found else "none (built-in defaults only)"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
