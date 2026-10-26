from adapters.observability.config import LoggingSettings
from adapters.observability.logging import configure_logging
from api.main import build_app

# uvicorn configures only its own `uvicorn.*` loggers, and before it imports this
# module: configure_logging gives the root logger JSON handlers (stdout, plus
# LOG_DIR's rotated file) and routes uvicorn's server logs through them
# (docs/observability.md § Logs). LOG_LEVEL overrides the default INFO.
configure_logging(LoggingSettings.from_env(), service="api")

app = build_app()
