#!/usr/bin/env python3
# Copyright 2022 DMTF. All rights reserved.
# License: BSD 3-Clause License. For full text see link:
# https://github.com/DMTF/Redfish-Service-Validator/blob/master/LICENSE.md

from redfish_service_validator.RedfishServiceValidator import main, my_logger
import sys

if __name__ == '__main__':
    try:
        status_code, lastResultsPage, exit_string = main()
        sys.exit(status_code)
    except Exception as e:
        my_logger.exception("Program finished prematurely: %s", e)
