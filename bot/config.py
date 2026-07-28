from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="P2P_", extra="ignore")

    bot_token: str = ""   # may be blank at first boot → set it in the web panel
    admin_ids: str = ""
    admin_chat_id: int | None = None
    db_path: str = "p2p.sqlite3"
    min_usd: float = 10
    max_usd: float = 10_000
    eta_text: str = "15-30 minutes"
    support_handle: str = "@support"
    # WhatsApp number for the website's floating support button (with country
    # code, digits only or +91… format). Blank = button hidden. Also settable
    # live from Telegram with /setwhatsapp.
    support_whatsapp: str = ""

    # TRON auto-scan (5s ≈ 17,280 calls/day per chain — within BscScan's free 100k/day)
    scan_interval_sec: int = 5
    deposit_ttl_min: int = 15          # the deposit "session": the quote expires this
                                       # many minutes after creation, then the user must
                                       # start a fresh payout (expired = gone; late payers
                                       # contact the admin, who settles manually)
    remind_min: int = 10               # nudge the user if no deposit after this (< ttl)
    scan_page_limit: int = 100         # transfers fetched per TronGrid page
    scan_max_pages: int = 10           # page cap per address per tick
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_key: str = ""             # optional TronGrid API key for higher limits
    tronscan_api: str = "https://apilist.tronscanapi.com/api"  # per-tx global lookup (claims)
    usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # mainnet USDT (TRC20)

    # BEP20 / BSC second chain (active once the BEP20 address + BscScan key are set)
    bscscan_url: str = "https://api.etherscan.io/v2/api"   # Etherscan v2 multichain
    bsc_chainid: int = 56              # BNB Smart Chain
    bscscan_key: str = ""              # BscScan/Etherscan API key (from env or panel)
    bep20_usdt_contract: str = "0x55d398326f99059fF775485246999027B3197955"  # USDT BEP20, 18 dp

    # Max simultaneously-open orders per user (awaiting/received/pending)
    open_orders_max: int = 3

    # Give every open order a unique deposit amount (unique cents) so the
    # scanner matches instantly with no amount collisions.
    unique_cents: bool = True

    # A claimed/refunded TXID's on-chain amount must be within this fee band of the
    # order's own amount (max of abs USDT and a fraction of it) — so one customer can't
    # claim another customer's mismatched deposit that merely landed at our address.
    claim_fee_band_abs: float = 3.0
    claim_fee_band_pct: float = 0.02

    # Public customer website (same process + DB as the bot; the interface for
    # ad traffic). Serve behind nginx + TLS on your domain; set port 0 to disable.
    site_host: str = "127.0.0.1"
    site_port: int = 8090
    site_orders_per_hour: int = 6      # per-IP new-order throttle (anti junk/ads abuse)

    # Web admin panel (optional; disabled unless a password is set)
    panel_password: str = ""           # required to enable the web panel
    panel_secret: str = ""             # cookie-signing secret (auto-derived if blank)
    panel_host: str = "127.0.0.1"      # 0.0.0.0 to reach it at the server IP:port
    panel_port: int = 8088
    panel_tls_cert: str = ""           # path to a cert (self-signed ok) → serves HTTPS
    panel_tls_key: str = ""            # path to the matching private key

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(",", " ").split() if x.strip().isdigit()]


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
