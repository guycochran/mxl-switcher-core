CXX ?= c++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?= -Iinclude
BUILD := build
MXL_CFLAGS ?= $(shell pkg-config --cflags libmxl)
MXL_LIBS ?= $(shell pkg-config --libs libmxl)
CORE_OBJ := $(BUILD)/core.o
CONTROL_OBJ := $(BUILD)/control_api.o
HTTP_OBJ := $(BUILD)/http_server.o
SKIN_OBJ := $(BUILD)/reference_skin.o
CONFIG_OBJ := $(BUILD)/live_config.o
READ_WINDOW_OBJ := $(BUILD)/read_window.o

.PHONY: all test demo soak live live-test-source live-syntax clean

all: $(BUILD)/mxl-core-demo $(BUILD)/core-tests $(BUILD)/control-api-tests $(BUILD)/http-server-tests $(BUILD)/live-config-tests

$(BUILD):
	mkdir -p $(BUILD)

$(CORE_OBJ): src/core.cpp include/mxl_core/core.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(CONTROL_OBJ): src/control_api.cpp include/mxl_core/control_api.hpp include/mxl_core/core.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(HTTP_OBJ): src/http_server.cpp include/mxl_core/http_server.hpp include/mxl_core/control_api.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(SKIN_OBJ): src/reference_skin.cpp include/mxl_core/reference_skin.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(CONFIG_OBJ): src/live_config.cpp include/mxl_core/live_config.hpp include/mxl_core/core.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(READ_WINDOW_OBJ): src/read_window.cpp include/mxl_core/read_window.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(BUILD)/mxl-core-demo: apps/mxl-core-demo.cpp $(CORE_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/core-tests: tests/core_tests.cpp $(CORE_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/control-api-tests: tests/control_api_tests.cpp $(CORE_OBJ) $(CONTROL_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/http-server-tests: tests/http_server_tests.cpp $(CORE_OBJ) $(CONTROL_OBJ) $(HTTP_OBJ) $(SKIN_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/live-config-tests: tests/live_config_tests.cpp $(CONFIG_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/read-window-tests: tests/read_window_tests.cpp $(READ_WINDOW_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

test: $(BUILD)/core-tests $(BUILD)/control-api-tests $(BUILD)/http-server-tests $(BUILD)/live-config-tests $(BUILD)/read-window-tests
	./$(BUILD)/core-tests
	./$(BUILD)/control-api-tests
	./$(BUILD)/http-server-tests
	./$(BUILD)/live-config-tests
	./$(BUILD)/read-window-tests
	python3 -m unittest tools/test_mxl_core_nmos.py
	python3 -m unittest tools/test_mxl_discovery_planner.py
	python3 -m unittest tools/test_mxl_rebind_apply.py

demo: $(BUILD)/mxl-core-demo
	./$(BUILD)/mxl-core-demo

soak: $(BUILD)/core-tests
	./$(BUILD)/core-tests --soak 1000000

# Build this target on Linux after installing the official MXL SDK.
live: $(CORE_OBJ) $(CONTROL_OBJ) $(HTTP_OBJ) $(SKIN_OBJ) $(CONFIG_OBJ) $(READ_WINDOW_OBJ) src/mxl_backend.cpp apps/mxl-core-live.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(MXL_CFLAGS) $(CXXFLAGS) $^ $(MXL_LIBS) -o $(BUILD)/mxl-core-live

live-test-source: apps/mxl-test-source.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(MXL_CFLAGS) $(CXXFLAGS) $< $(MXL_LIBS) -o $(BUILD)/mxl-test-source

# Local API compatibility check against an MXL source checkout. Usage:
# make live-syntax MXL_INCLUDE=/path/to/mxl/lib/include
MXL_INCLUDE ?= ../mxl-sdk/lib/include
live-syntax: | $(BUILD)
	$(CXX) $(CPPFLAGS) -I$(MXL_INCLUDE) $(CXXFLAGS) -fsyntax-only src/read_window.cpp src/mxl_backend.cpp apps/mxl-core-live.cpp

PREFIX ?= /usr/local
SYSTEMD_UNIT_DIR ?= /usr/lib/systemd/system
EXAMPLE_DIR ?= $(PREFIX)/share/doc/mxl-switcher-core
.PHONY: install-live
install-live: $(BUILD)/mxl-core-live
	install -Dm755 $(BUILD)/mxl-core-live $(DESTDIR)$(PREFIX)/bin/mxl-core-live
	install -Dm644 packaging/mxl-switcher-core.service $(DESTDIR)$(SYSTEMD_UNIT_DIR)/mxl-switcher-core.service
	install -Dm644 packaging/mxl-switcher-core.conf.example $(DESTDIR)$(EXAMPLE_DIR)/mxl-switcher-core.conf.example

clean:
	rm -rf $(BUILD)

.PHONY: check-scripts
check-scripts:
	sh -n scripts/run-acceptance.sh
	sh -n scripts/run-ndi-pilot-gate.sh
