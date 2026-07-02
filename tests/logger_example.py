"""Example usage of centralized logger configuration."""

from logger_utils import setup_logger, get_log_level_from_env

# Example 1: Basic logger setup (recommended for most modules)
logger = setup_logger(__name__)

# Example 2: Logger with custom level override
debug_logger = setup_logger("debug_module", level="DEBUG")

# Example 3: Get numeric log level for advanced configurations
numeric_level = get_log_level_from_env()
print(f"Current log level: {numeric_level}")

# Usage examples
logger.debug("This is a debug message - only shown when LOG_LEVEL=DEBUG")
logger.info("This is an info message - shown by default")
logger.warning("This is a warning message")
logger.error("This is an error message")

# In your actual modules, simply use:
# from logger_utils import setup_logger
# logger = setup_logger(__name__)
