from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XIE_", env_file=".env", extra="ignore")

    app_name: str = "XBRL Intelligence Engine"
    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+asyncpg://xie:xie@localhost:5432/xie"

    sec_user_agent: str = "XBRL-IE engineering.team@engineosol.com"

    raw_data_dir: Path = Path("data/raw")
    arelle_cache_dir: Path = Path("data/arelle_cache")

    # Mode C user-upload (Phase 7.5)
    upload_max_bytes: int = 50 * 1024 * 1024              # 50 MB hard cap on the request body
    upload_max_unzipped_bytes: int = 200 * 1024 * 1024    # 200 MB cap on decompressed ZIP payload
    upload_ttl_hours: int = 24                            # blob + DB rows TTL
    upload_rate_limit_per_hour: int = 10                  # per-IP throttle


settings = Settings()
