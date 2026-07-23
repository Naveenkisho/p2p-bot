from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="P2P_", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    admin_chat_id: int | None = None
    db_path: str = "p2p.sqlite3"
    min_usd: float = 10
    max_usd: float = 10_000
    eta_text: str = "15-30 minutes"
    support_handle: str = "@support"

    # TRON auto-scan
    scan_interval_sec: int = 10
    deposit_ttl_min: int = 60          # awaiting_deposit orders expire after this
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_key: str = ""             # optional TronGrid API key for higher limits
    usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # mainnet USDT

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Payout services offered on the sell side. Rates are set live from chat
# (/setrate CDM 91) and stored in the DB; a service with no rate set is hidden.
SERVICES: dict[str, str] = {
    "UPI": "UPI",
    "IMPS": "IMPS instant",
    "CDM": "CDM",
    "CHEQUE": "Cheque transfer",
}
