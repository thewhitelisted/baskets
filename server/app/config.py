import os
from dataclasses import dataclass
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = SERVER_DIR / "data" / "baskets.db"
CORPORATE_ACTIONS = SERVER_DIR / "data" / "corporate_actions.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    sec_user_agent: str | None
    corporate_actions_path: Path

    @property
    def yfinance_cache_dir(self) -> Path:
        return self.db_path.parent / "yfinance-cache"

    @classmethod
    def from_env(cls) -> "Settings":
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        return cls(
            db_path=Path(os.environ.get("BASKETS_DB") or DEFAULT_DB),
            sec_user_agent=user_agent or None,
            corporate_actions_path=CORPORATE_ACTIONS,
        )
