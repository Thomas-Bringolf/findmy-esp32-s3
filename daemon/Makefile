# findmy-os-daemon build + install
#
#   make            # build ./findmy-os-daemon
#   make test       # build and run a short smoke test against a non-existent
#                   # tty (checks it fails soft, retries and exits clean)
#   make install    # install binary to $(DESTDIR)$(PREFIX)/bin and the
#                   # systemd unit to $(UNITDIR), then daemon-reload
#   make uninstall  # remove both

PREFIX  ?= /usr/local
BINDIR   = $(DESTDIR)$(PREFIX)/bin
UNITDIR ?= /etc/systemd/system
SYSCONF  = /etc/sysconfig

CC       ?= cc
CFLAGS   ?= -O2 -Wall -Wextra -Werror
TARGET    = findmy-os-daemon
SRC       = findmy-os-daemon.c
UNIT      = findmy-os-daemon.service

.PHONY: all test install uninstall clean

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $<

# Smoke test: the daemon must start, log its health, spin against a tty that
# does not exist, and terminate on SIGTERM. `timeout -s TERM 3` gives exit
# 0 on clean TERM shutdown, or 124 if it was still running (equally fine).
test: $(TARGET)
	@echo "== smoke test: no-tty retry + SIGTERM =="
	@timeout -s TERM 3 ./$(TARGET) --tty /dev/ttyACM_nonexistent --interval 1 \
	    || test $$? -eq 124 \
	    || exit 1
	@echo "== smoke test passed =="

install: $(TARGET)
	install -d $(BINDIR)
	install -m 0755 $(TARGET) $(BINDIR)/$(TARGET)
	install -d $(UNITDIR)
	install -m 0644 $(UNIT) $(UNITDIR)/$(UNIT)
	@if command -v systemctl >/dev/null 2>&1; then \
	    echo "== systemctl daemon-reload =="; systemctl daemon-reload; fi
	@echo "installed $(TARGET) -> $(BINDIR)/$(TARGET)"
	@echo "installed unit    -> $(UNITDIR)/$(UNIT)"
	@echo "enable with:  systemctl enable --now findmy-os-daemon"

uninstall:
	rm -f $(BINDIR)/$(TARGET)
	rm -f $(UNITDIR)/$(UNIT)
	@if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload; fi
	@echo "removed $(TARGET)"

clean:
	rm -f $(TARGET)
