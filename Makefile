# codework/Python/xrandrw/Makefile
PREFIX ?= $(HOME)/.local
BIN    ?= $(PREFIX)/bin
SYSD   ?= $(HOME)/.config/systemd/user
CONF   ?= $(HOME)/.config

TARGET := xrandrw
UNIT   := systemd/xrandrw.service
CONF_SAMPLE := xrandrw.conf.sample

.PHONY: all install uninstall enable disable

all:
	@echo "Targets: install, uninstall, enable, disable"

install:
	pipx install --force .
	install -Dm644 $(CONF_SAMPLE) $(CONF)/xrandrw.conf.sample
	install -Dm644 $(UNIT) $(SYSD)/xrandrw.service
	@echo "Run: systemctl --user daemon-reload"

uninstall:
	- systemctl --user stop xrandrw.service || true
	- systemctl --user disable xrandrw.service || true
	- rm -f $(SYSD)/xrandrw.service
	- rm -f $(BIN)/$(TARGET)
	- rm -f $(CONF)/xrandrw.conf.sample
	@echo "Run: systemctl --user daemon-reload"

enable:
	systemctl --user daemon-reload
	systemctl --user enable --now xrandrw.service

disable:
	systemctl --user disable --now xrandrw.service || true
	systemctl --user daemon-reload

