CXX ?= c++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?= -Iinclude
BUILD := build
CORE_OBJ := $(BUILD)/core.o
CONTROL_OBJ := $(BUILD)/control_api.o

.PHONY: all test demo soak live live-syntax clean

all: $(BUILD)/mxl-core-demo $(BUILD)/core-tests $(BUILD)/control-api-tests

$(BUILD):
	mkdir -p $(BUILD)

$(CORE_OBJ): src/core.cpp include/mxl_core/core.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(CONTROL_OBJ): src/control_api.cpp include/mxl_core/control_api.hpp include/mxl_core/core.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

$(BUILD)/mxl-core-demo: apps/mxl-core-demo.cpp $(CORE_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/core-tests: tests/core_tests.cpp $(CORE_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

$(BUILD)/control-api-tests: tests/control_api_tests.cpp $(CORE_OBJ) $(CONTROL_OBJ) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ -o $@

test: $(BUILD)/core-tests $(BUILD)/control-api-tests
	./$(BUILD)/core-tests
	./$(BUILD)/control-api-tests

demo: $(BUILD)/mxl-core-demo
	./$(BUILD)/mxl-core-demo

soak: $(BUILD)/core-tests
	./$(BUILD)/core-tests --soak 1000000

# Build this target on Linux after installing the official MXL SDK.
live: $(CORE_OBJ) src/mxl_backend.cpp apps/mxl-core-live.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $^ $$(pkg-config --cflags --libs libmxl) -o $(BUILD)/mxl-core-live

# Local API compatibility check against an MXL source checkout. Usage:
# make live-syntax MXL_INCLUDE=/path/to/mxl/lib/include
MXL_INCLUDE ?= ../mxl-sdk/lib/include
live-syntax: | $(BUILD)
	$(CXX) $(CPPFLAGS) -I$(MXL_INCLUDE) $(CXXFLAGS) -fsyntax-only src/mxl_backend.cpp apps/mxl-core-live.cpp

clean:
	rm -rf $(BUILD)
