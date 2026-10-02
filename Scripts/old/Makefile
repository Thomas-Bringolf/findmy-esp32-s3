# ESP32-S3 Find My beacon - script shortcuts
# Usage:
#   make help              show this overview
#   make pair [ID=..] [PORT=..]   pair a beacon over UART
#   make sync [ID=..] [PORT=..]   sync the beacon's slot counter
#   make test                       run the console protocol edge cases
#   make power [SECS=60]            measure the awake/sleep duty cycle
#   make retrieve                   fetch new reports (saved session)
#   make retrieve-loop              background loop: fetch every ~2 min
#   make watch                      retrieve + poll every 2 min
#   make monitor                    live dashboard
#   make verify                     check the advertisement on air
#   make scan [SECONDS=15]          raw BLE scan
#
# Override defaults: make pair ID=kitchen-tag PORT=/dev/ttyACM1

PORT      ?= /dev/ttyACM0
ID        ?= esp32-s3-test
WATCH     ?= 120
SCAN_SECS ?= 15
SECS      ?= 60
DEVICE    ?=

.PHONY: help pair sync sync-ble devices retrieve retrieve-loop watch monitor verify scan test power

help:
	@echo "ESP32-S3 Find My beacon tooling"
	@echo ""
	@echo "  make pair [ID=$(ID)] [PORT=$(PORT)]  pair a beacon over UART"
	@echo "  make sync [ID=$(ID)] [PORT=$(PORT)]  sync the beacon's slot counter (USB)"
	@echo "  make sync-ble [DEVICE=$(DEVICE)]     sync the slot counter via BLE scan"
	@echo "  make devices                         list paired devices"
	@echo "  make test                            console protocol edge cases (resets the device)"
	@echo "  make power [SECS=$(SECS)]            awake/sleep duty cycle from the PWR telemetry"
	@echo "  make retrieve                        fetch new reports (saved session)"
	@echo "  make retrieve-loop                   background retrieval loop (state/retrieve_loop.log)"
	@echo "  make watch [WATCH=$(WATCH)]          retrieve + keep polling"
	@echo "  make monitor                         live dashboard (Ctrl-C to quit)"
	@echo "  make verify                          verify the advertisement on air"
	@echo "  make scan [SCAN_SECS=$(SCAN_SECS)]   raw BLE scan for Find My packets"
	@echo ""
	@echo "Docs: README.md here, ../README.md and ../docs/"

pair:
	@python3 pair_device.py --port $(PORT) --id $(ID)

sync:
	@python3 pair_device.py --sync --id $(ID) --port $(PORT)

sync-ble:
	@python3 sync_ble.py $(if $(DEVICE),--device $(DEVICE))

devices:
	@python3 pair_device.py --list

retrieve:
	@python3 retrieve_rotating.py

retrieve-loop:
	@./retrieve_loop.sh

watch:
	@python3 retrieve_rotating.py --watch $(WATCH)

monitor:
	@python3 monitor.py

verify:
	@python3 verify_beacon.py

test:
	@python3 test_uart.py --port $(PORT) --id $(ID)

power:
	@python3 measure_power.py --reset --dbg-sec 60 --seconds $(SECS)

scan:
	@python3 scan_findmy.py $(SCAN_SECS)
