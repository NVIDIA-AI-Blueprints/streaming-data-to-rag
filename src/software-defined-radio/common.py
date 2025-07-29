######################################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
######################################################################################################

import os
import time
import socket
import logging

PARAM_FILE = os.path.join(os.path.dirname(__file__), 'params.yaml')
FRONTEND_URI = os.environ.get('FRONTEND_URI', 'localhost:6001')
DATABASE_URI = os.environ.get('DATABASE_URI', '0.0.0.0:8081')
ASR_URI = os.environ.get('ASR_URI', '0.0.0.0:50051')

def get_log_level(env_var_name='SDR_LOG_LEVEL', default='WARN'):
    """Safely get log level from environment variable with validation"""
    allowed_levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARN': logging.WARNING,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }

    level_str = os.environ.get(env_var_name, default).upper()
    if level_str not in allowed_levels:
        logger.warning(f"Invalid log level '{level_str}', using default '{default}'")
        level_str = default.upper()

    return allowed_levels[level_str]

LOG_LEVEL = get_log_level()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def setup_logging(name):
    """Build a logger that prints to screen within Holoscan pipeline"""
    class NameFuncFilter(logging.Filter):
        """Concatenate the logger name and function name, then limit characters"""
        def filter(self, record, nchar=30):
            record.name_func_combo = f"{record.name}.{record.funcName}"[:nchar]
            return True

    # Build a handler to make sure logging is printed to console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(LOG_LEVEL)
    console_handler.addFilter(NameFuncFilter())
    console_formatter = logging.Formatter(
        '[%(asctime)s] %(name_func_combo)-30s %(levelname)6s: %(message)s',
        datefmt='%I:%M:%S'
    )
    console_handler.setFormatter(console_formatter)

    # Create logger, add console handler, and return
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)
    logger.addHandler(console_handler)
    return logger

def wait_for_uri(uri, timeout=300, wait_sec=5):
    try:
        host, port = uri.split(':')
        port = int(port)
    except ValueError as e:
        logger.error(f"Invalid URI format. Expected format: 'host:port', got '{uri}'.")
        raise e

    start_time = time.time()
    while True:
        elapsed_time = time.time() - start_time

        if elapsed_time > timeout:
            logger.error(
                f"Timeout reached: {uri} is not open after {timeout} seconds."
                f"Waited {elapsed_time} seconds"
            )
            raise TimeoutError

        try:
            with socket.create_connection((host, port), timeout=timeout-elapsed_time):
                logger.info(f"{uri} is now open!")
                break
        except (ConnectionRefusedError, OSError, socket.timeout):
            # Wait a short period before trying again
            logger.warning(f"Waiting {wait_sec}s for application at {uri}")
            time.sleep(wait_sec)