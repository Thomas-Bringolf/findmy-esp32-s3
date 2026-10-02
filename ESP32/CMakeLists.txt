cmake_minimum_required(VERSION 3.5)

set(SUPPORTED_TARGETS esp32s3)

# The console PIN is no longer baked in at build time: it lives in NVS
# (see main/findmy_keys.c) and is rotated with the PIN command.

set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(openhaystack)
