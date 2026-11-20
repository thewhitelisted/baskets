import logging
from datetime import UTC, datetime

from app.config import Settings
from app.corporate_actions import load_corporate_actions
from app.edgar import DISABLED_MESSAGE, EdgarClient
from app.providers.yahoo import YahooProvider
from app.service import Service
from app.store import Store

log = logging.getLogger(__name__)


def build_service(settings: Settings) -> Service:
    """Production wiring. Raises CorporateActionsError if the curated file is invalid."""
    actions = load_corporate_actions(settings.corporate_actions_path)
    edgar = None
    if settings.sec_user_agent is None:
        log.warning("%s; EDGAR shares and rename detection are off", DISABLED_MESSAGE)
    else:
        edgar = EdgarClient(settings.sec_user_agent)
    return Service(
        store=Store(settings.db_path),
        providers={"yahoo": YahooProvider(settings.yfinance_cache_dir)},
        edgar=edgar,
        corporate_actions=actions,
        now=lambda: datetime.now(UTC),
    )
