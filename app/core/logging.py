import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# These libraries log full request URLs at INFO, which would leak SAS tokens (AGENTS.md section 2).
_QUIET_LOGGERS = ("httpx", "httpcore", "azure")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
