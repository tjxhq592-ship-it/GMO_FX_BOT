import logging
import sys
from typing import Optional


def configure_logging(log_file: str = "trade_log.txt", level: int = logging.INFO) -> None:
    """Configure root logging: file handler (utf-8) and console handler.

    This function is safe to call multiple times; subsequent calls will not add duplicate handlers.
    """
    root = logging.getLogger()
    if root.handlers:
        # assume already configured
        return

    root.setLevel(level)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%y/%m/%d %H:%M:%S")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler for development
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    root.addHandler(ch)
