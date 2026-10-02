idf_component_register(SRCS "main.c" "ble_adv.c" "findmy_keys.c" "uart_cmd.c"
                    PRIV_REQUIRES bt nvs_flash mbedtls esp_timer esp_hw_support
                    INCLUDE_DIRS ".")

# Slot length in seconds (official Find My accessories use 900)
add_compile_definitions(FM_SLOT_SECONDS=120)
