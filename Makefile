CC ?= gcc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra
PYTHON ?= python3
SRC := tools/src
BIN := tools/bin
VENDOR := tools/vendor/cjson
SQLITE := tools/vendor/sqlite

.PHONY: all clean check

all: $(BIN)/benchmark_harness $(BIN)/classify_pages $(BIN)/residency_checker \
	$(BIN)/layout_rewriter $(BIN)/prefetch_runner $(BIN)/drop_caches.sh

$(BIN):
	mkdir -p $@

$(BIN)/benchmark_harness: $(SRC)/benchmark_harness.c $(SQLITE)/sqlite3.c $(SQLITE)/sqlite3.h $(SQLITE)/sqlite3ext.h | $(BIN)
	$(CC) $(CFLAGS) -I$(SQLITE) -o $@ $(SRC)/benchmark_harness.c $(SQLITE)/sqlite3.c -lm -ldl -lpthread

$(BIN)/classify_pages: $(SRC)/classify_pages.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ $<

$(BIN)/residency_checker: $(SRC)/residency_checker.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ $<

$(BIN)/layout_rewriter: $(SRC)/layout_rewriter.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ $<

$(BIN)/prefetch_runner: $(SRC)/prefetch_runner.c $(VENDOR)/cJSON.c $(VENDOR)/cJSON.h | $(BIN)
	$(CC) $(CFLAGS) -I$(VENDOR) -o $@ $(SRC)/prefetch_runner.c $(VENDOR)/cJSON.c

$(BIN)/drop_caches.sh: $(SRC)/drop_caches.sh | $(BIN)
	cp $< $@
	chmod 0755 $@

check:
	$(PYTHON) -m py_compile $(SRC)/*.py

clean:
	rm -f $(BIN)/*
