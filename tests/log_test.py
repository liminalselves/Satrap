from satrap import logger

logger.info("This is an info message.", std_out=True, save_to_file=True)
logger.debug("This is a debug message.", std_out=True, save_to_file=True)
logger.warning("This is a warning message.", std_out=True, save_to_file=True)
logger.error("This is an error message.", std_out=True, save_to_file=True)
logger.critical("This is a critical message.", std_out=True, save_to_file=True)
