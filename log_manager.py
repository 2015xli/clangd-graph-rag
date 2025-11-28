import logging
import sys

# --- 1. Custom Filter Classes ---

class DebugOnlyFilter(logging.Filter):
    """Passes ONLY messages with an exact level of DEBUG."""
    def filter(self, record):
        return record.levelno == logging.DEBUG


class InfoAndUpFilter(logging.Filter):
    """Passes ONLY messages with INFO and higher."""
    def filter(self, record):
        return record.levelno >= logging.INFO


# --- 2. Public initialization function ---

_initialized = False  # Prevent re-initializing when multiple modules import


def init_logging(log_file='debug.log'):
    global _initialized
    if _initialized:
        return  # Avoid double handlers and duplicated output

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Application logger (__main__ OR callers)
    app_logger = logging.getLogger(__name__)
    app_logger.setLevel(logging.DEBUG)

    # --- Handlers ---

    # Console handler: INFO and above
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(InfoAndUpFilter())
    stdout_handler.setFormatter(
        logging.Formatter('%(asctime)s - [%(levelname)s] %(message)s')
    )

    # File handler: DEBUG only
    file_handler = logging.FileHandler(log_file, mode='w')
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(DebugOnlyFilter())
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - [%(levelname)s] %(message)s')
    )

    # Attach handlers
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)

    _initialized = True
