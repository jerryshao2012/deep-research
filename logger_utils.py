"""Centralized logging configuration utility."""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def setup_logger(
        name: Optional[str] = None,
        level: Optional[str] = None,
        format_string: Optional[str] = None,
) -> logging.Logger:
    """
    Setup and configure a logger with centralized configuration.
    
    Args:
        name: Logger name (typically __name__ of the calling module)
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
              Defaults to LOG_LEVEL env var or "INFO"
        format_string: Custom log format string
                      Defaults to standard format with timestamp
    
    Returns:
        Configured logger instance
    
    Example:
        from logger_utils import setup_logger
        logger = setup_logger(__name__)
        logger.info("Application started")
    """
    # Get log level from parameter, env var, or default to INFO
    log_level = level or os.getenv("LOG_LEVEL", "INFO").upper()

    # Default format if not provided
    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Configure root logger only once
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format=format_string,  # type: ignore[arg-type]
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    # Get logger for the specific module
    logger = logging.getLogger(name)

    return logger


def get_log_level_from_env(default: str = "INFO") -> int:
    """
    Get numeric log level from environment variable.
    
    Args:
        default: Default level string if env var not set
    
    Returns:
        Numeric logging level constant
    """
    level_str = os.getenv("LOG_LEVEL", default).upper()
    return getattr(logging, level_str, logging.INFO)
