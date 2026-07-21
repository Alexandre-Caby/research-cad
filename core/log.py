import logging
import os

from core import config

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured: set[str] = set()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    level = os.environ.get("PIPELINE_LOG_LEVEL", "INFO")
    logger.setLevel(level)

    formatter = logging.Formatter(_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    log_path = os.path.join(config.STORAGE_DIR, "pipeline.log")
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _configured.add(name)
    return logger
