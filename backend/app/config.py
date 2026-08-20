from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "digycorp-cx-agent"
    environment: str = "local"
    # Set per-VM. Kept deliberately open here; tighten once the FE origin is known.
    cors_origins: list[str] = ["*"]


settings = Settings()
