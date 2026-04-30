from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="fleet")
    postgres_user: str = Field(default="fleet")
    postgres_password: str = Field(default="")

    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379)

    discord_bot_token: str = Field(default="")
    discord_guild_id: str = Field(default="")
    discord_alert_channel_id: str = Field(default="")
    discord_daily_report_channel_id: str = Field(default="")
    discord_owner_user_id: str = Field(default="")

    telegram_bot_token: str = Field(default="")
    telegram_owner_user_id: str = Field(default="")

    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_from_number: str = Field(default="")
    twilio_to_number: str = Field(default="")

    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="")
    smtp_to: str = Field(default="")

    anthropic_api_key: str = Field(default="")

    daily_report_hour_local: int = Field(default=7)
    daily_report_timezone: str = Field(default="America/New_York")

    deployed_capital_usd: float = Field(default=0.0)
    vpn_trigger_threshold_usd: float = Field(default=50000.0)

    cloudflare_tunnel_token: str = Field(default="")

    heartbeat_interval_seconds: int = Field(default=30)
    heartbeat_alert_after_seconds: int = Field(default=120)
    heartbeat_restart_after_seconds: int = Field(default=300)
    reconciliation_interval_seconds: int = Field(default=300)
    reconciliation_drift_threshold_pct: float = Field(default=0.5)
    consecutive_loss_halt_count: int = Field(default=8)
    consecutive_loss_halt_hours: int = Field(default=24)

    fleet_daily_dd_halt_pct: float = Field(default=10.0)
    fleet_total_dd_kill_pct: float = Field(default=25.0)
    fleet_auto_resume_hours: int = Field(default=48)
    fleet_auto_resume_btc_stability_pct: float = Field(default=5.0)

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
