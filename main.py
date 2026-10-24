import logging
import os

from api.main import build_app

# uvicorn configures only its own `uvicorn.*` loggers; without a root handler
# the application's INFO records (webhook routing decisions, worker stats)
# never reach the console. basicConfig is a no-op when a real logging config
# (pytest, `--log-config`) has already installed handlers, so this only
# affects bare `uvicorn main:app` runs. LOG_LEVEL overrides the default.
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

app = build_app()
