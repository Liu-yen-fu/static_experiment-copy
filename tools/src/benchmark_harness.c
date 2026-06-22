/*
 * SQLite cold-start benchmark harness.
 *
 * This runner accepts generated YCSB-style transaction workloads:
 *   read <id>
 *   update <id>
 *   insert <id>
 *   scan <id> <len>
 *   readmodifywrite <id>
 *
 * It uses Linux mmap/mincore/madvise to observe and cool the database file's
 * page-cache residency, then executes each workload transaction through SQLite
 * prepared statements while recording per-operation latency and page faults.
 */

#define _GNU_SOURCE

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdarg.h>
#include <sqlite3.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

typedef enum {
    COLD_ADVICE_NONE = 0,
    COLD_ADVICE_COLD,
    COLD_ADVICE_PAGEOUT,
    COLD_ADVICE_DONTNEED
} cold_advice_t;

typedef enum {
    SQLITE_OPEN_BEFORE_COLD = 0,
    SQLITE_OPEN_AFTER_COLD
} sqlite_open_timing_t;

typedef enum {
    SCHEMA_INIT_BEFORE_COLD = 0,
    SCHEMA_INIT_AFTER_COLD
} schema_init_timing_t;

typedef enum {
    OP_READ = 0,
    OP_UPDATE,
    OP_INSERT,
    OP_SCAN,
    OP_READMODIFYWRITE
} op_type_t;

typedef struct {
    op_type_t type;
    uint32_t target_id;
    uint32_t scan_len;
} workload_op_t;

typedef struct {
    workload_op_t *ops;
    size_t count;
    size_t capacity;
} workload_t;

typedef struct {
    const char *db_path;
    const char *output_csv;
    const char *record_dir;
    const char *workload_path;
    int64_t mmap_size;
    const char *drop_caches_script;
    const char *post_cold_script;
    bool drop_caches_use_sudo;
    cold_advice_t cold_advice;
    sqlite_open_timing_t sqlite_open_timing;
    schema_init_timing_t schema_init_timing;
    bool debug;
} options_t;

typedef struct {
    int fd;
    size_t file_size;
    size_t os_page_size;
    size_t os_page_count;
    size_t sqlite_page_size;
    uint32_t sqlite_page_count;
    void *mapping;
} mapped_db_t;

typedef struct {
    sqlite3 *db;
    sqlite3_stmt *read_stmt;
    sqlite3_stmt *update_stmt;
    sqlite3_stmt *insert_stmt;
    sqlite3_stmt *scan_stmt;
    bool opened;
    bool schema_initialized;
} sqlite_ctx_t;

typedef struct {
    uint64_t total_ns;
    uint64_t first_query_ns;
    long total_majflt;
    long total_minflt;
} benchmark_summary_t;

static uint16_t rd_be16(const uint8_t *p) {
    return ((uint16_t)p[0] << 8) | p[1];
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s --workload <file> [options]\n"
            "\n"
            "Workload format:\n"
            "  One operation per line:\n"
            "    read <id>\n"
            "    update <id>\n"
            "    insert <id>\n"
            "    scan <id> <len>\n"
            "    readmodifywrite <id>\n"
            "  Blank lines and lines starting with # are ignored.\n"
            "\n"
            "Options:\n"
            "  --db <file>                     SQLite DB path (default: test.db).\n"
            "  --output <file>                 Per-operation CSV output path (default: benchmark_harness_operations.csv).\n"
            "  --record-dir <dir>              Run record directory (default: benchmark_harness_runs).\n"
            "  --workload <file>               Pre-generated workload text file.\n"
            "  --mmap-size <bytes>             PRAGMA mmap_size target (default: file size).\n"
            "  --drop-caches-script <file>     Run root-only helper after madvise to drop Linux page cache.\n"
            "  --drop-caches-use-sudo          Run drop-cache helper via sudo -n.\n"
            "  --post-cold-script <file>       Run helper after cold/drop-caches and before SQLite query setup.\n"
            "  --cold-advice none              Do not run madvise/drop-caches; measure current cache state.\n"
            "  --cold-advice cold              Use MADV_COLD only.\n"
            "  --cold-advice pageout           Use MADV_COLD then MADV_PAGEOUT.\n"
            "  --cold-advice dontneed          Use MADV_COLD, MADV_PAGEOUT, then MADV_DONTNEED (default).\n"
            "  --sqlite-open-timing before-cold|after-cold    Default: before-cold.\n"
            "  --schema-init-timing before-cold|after-cold    Default: before-cold.\n"
            "  --debug                         Print sync, madvise, drop-caches, and SQLite timing diagnostics.\n",
            prog);
}

static int64_t parse_i64(const char *s, const char *flag) {
    char *end = NULL;
    long long value = strtoll(s, &end, 10);
    if (end == s || *end != '\0') {
        fprintf(stderr, "error: invalid integer for %s: %s\n", flag, s);
        exit(1);
    }
    return (int64_t)value;
}

static void trim_whitespace(char *s) {
    char *start = s;
    size_t len;

    while (*start != '\0' && isspace((unsigned char)*start)) {
        ++start;
    }
    if (start != s) {
        memmove(s, start, strlen(start) + 1);
    }

    len = strlen(s);
    while (len > 0 && isspace((unsigned char)s[len - 1])) {
        s[--len] = '\0';
    }
}

static void parse_args(int argc, char **argv, options_t *opts) {
    memset(opts, 0, sizeof(*opts));
    opts->db_path = "test.db";
    opts->output_csv = "benchmark_harness_operations.csv";
    opts->record_dir = "benchmark_harness_runs";
    opts->cold_advice = COLD_ADVICE_DONTNEED;
    opts->sqlite_open_timing = SQLITE_OPEN_BEFORE_COLD;
    opts->schema_init_timing = SCHEMA_INIT_BEFORE_COLD;

    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        if (strcmp(arg, "--db") == 0 && i + 1 < argc) {
            opts->db_path = argv[++i];
        } else if (strcmp(arg, "--output") == 0 && i + 1 < argc) {
            opts->output_csv = argv[++i];
        } else if (strcmp(arg, "--record-dir") == 0 && i + 1 < argc) {
            opts->record_dir = argv[++i];
        } else if (strcmp(arg, "--workload") == 0 && i + 1 < argc) {
            opts->workload_path = argv[++i];
        } else if (strcmp(arg, "--mmap-size") == 0 && i + 1 < argc) {
            opts->mmap_size = parse_i64(argv[++i], "--mmap-size");
        } else if (strcmp(arg, "--drop-caches-script") == 0 && i + 1 < argc) {
            opts->drop_caches_script = argv[++i];
        } else if (strcmp(arg, "--drop-caches-use-sudo") == 0) {
            opts->drop_caches_use_sudo = true;
        } else if (strcmp(arg, "--post-cold-script") == 0 && i + 1 < argc) {
            opts->post_cold_script = argv[++i];
        } else if (strcmp(arg, "--cold-advice") == 0 && i + 1 < argc) {
            const char *mode = argv[++i];
            if (strcmp(mode, "none") == 0) {
                opts->cold_advice = COLD_ADVICE_NONE;
            } else if (strcmp(mode, "cold") == 0) {
                opts->cold_advice = COLD_ADVICE_COLD;
            } else if (strcmp(mode, "pageout") == 0) {
                opts->cold_advice = COLD_ADVICE_PAGEOUT;
            } else if (strcmp(mode, "dontneed") == 0) {
                opts->cold_advice = COLD_ADVICE_DONTNEED;
            } else {
                fprintf(stderr, "error: unknown --cold-advice mode: %s\n", mode);
                exit(1);
            }
        } else if (strcmp(arg, "--sqlite-open-timing") == 0 && i + 1 < argc) {
            const char *mode = argv[++i];
            if (strcmp(mode, "before-cold") == 0) {
                opts->sqlite_open_timing = SQLITE_OPEN_BEFORE_COLD;
            } else if (strcmp(mode, "after-cold") == 0) {
                opts->sqlite_open_timing = SQLITE_OPEN_AFTER_COLD;
            } else {
                fprintf(stderr, "error: unknown --sqlite-open-timing mode: %s\n", mode);
                exit(1);
            }
        } else if (strcmp(arg, "--schema-init-timing") == 0 && i + 1 < argc) {
            const char *mode = argv[++i];
            if (strcmp(mode, "before-cold") == 0) {
                opts->schema_init_timing = SCHEMA_INIT_BEFORE_COLD;
            } else if (strcmp(mode, "after-cold") == 0) {
                opts->schema_init_timing = SCHEMA_INIT_AFTER_COLD;
            } else {
                fprintf(stderr, "error: unknown --schema-init-timing mode: %s\n", mode);
                exit(1);
            }
        } else if (strcmp(arg, "--debug") == 0) {
            opts->debug = true;
        } else {
            usage(argv[0]);
            exit(1);
        }
    }

    if (opts->workload_path == NULL) {
        usage(argv[0]);
        exit(1);
    }

    if (opts->sqlite_open_timing == SQLITE_OPEN_AFTER_COLD &&
        opts->schema_init_timing == SCHEMA_INIT_BEFORE_COLD) {
        fprintf(stderr,
                "error: --schema-init-timing before-cold requires --sqlite-open-timing before-cold\n");
        exit(1);
    }
}

static void die_errno(const char *what) {
    perror(what);
    exit(1);
}

static void die_sqlite(sqlite3 *db, const char *what, int rc) {
    fprintf(stderr, "error: %s failed: rc=%d msg=%s\n",
            what, rc, db ? sqlite3_errmsg(db) : "(no db)");
    exit(1);
}

static void emit_line_to(FILE *record, bool echo_stderr, const char *fmt, ...) {
    va_list ap;
    va_list ap_copy;

    va_start(ap, fmt);
    va_copy(ap_copy, ap);
    if (echo_stderr) {
        vfprintf(stderr, fmt, ap);
    }
    if (record != NULL) {
        vfprintf(record, fmt, ap_copy);
        fflush(record);
    }
    va_end(ap_copy);
    va_end(ap);
}

static void emit_line(FILE *record, const char *fmt, ...) {
    va_list ap;
    va_list ap_copy;

    va_start(ap, fmt);
    va_copy(ap_copy, ap);
    vfprintf(stderr, fmt, ap);
    if (record != NULL) {
        vfprintf(record, fmt, ap_copy);
        fflush(record);
    }
    va_end(ap_copy);
    va_end(ap);
}

static void write_record_line(FILE *record, const char *fmt, ...) {
    va_list ap;

    if (record == NULL) {
        return;
    }

    va_start(ap, fmt);
    vfprintf(record, fmt, ap);
    va_end(ap);
    fflush(record);
}

static void exec_sql(sqlite3 *db, const char *sql) {
    char *errmsg = NULL;
    int rc = sqlite3_exec(db, sql, NULL, NULL, &errmsg);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "error: sqlite3_exec failed: %s: %s\n",
                sql, errmsg ? errmsg : "(no message)");
        sqlite3_free(errmsg);
        exit(1);
    }
}

static const char *cold_advice_name(cold_advice_t advice) {
    switch (advice) {
        case COLD_ADVICE_COLD:
            return "cold";
        case COLD_ADVICE_NONE:
            return "none";
        case COLD_ADVICE_PAGEOUT:
            return "pageout";
        case COLD_ADVICE_DONTNEED:
            return "dontneed";
    }
    return "unknown";
}

static FILE *open_run_record(const options_t *opts, char *path_buf,
                             size_t path_buf_size) {
    if (mkdir(opts->record_dir, 0777) != 0 && errno != EEXIST) {
        die_errno("mkdir(record-dir)");
    }

    time_t now = time(NULL);
    struct tm tm_now;
    if (now == (time_t)-1 || localtime_r(&now, &tm_now) == NULL) {
        die_errno("localtime_r");
    }

    char timestamp[32];
    if (strftime(timestamp, sizeof(timestamp), "%Y%m%d-%H%M%S", &tm_now) == 0) {
        fprintf(stderr, "error: failed to format run record timestamp\n");
        exit(1);
    }

    for (unsigned attempt = 0; attempt < 1000; ++attempt) {
        int written;
        if (attempt == 0) {
            written = snprintf(path_buf, path_buf_size, "%s/run-%s-%ld.log",
                               opts->record_dir, timestamp, (long)getpid());
        } else {
            written = snprintf(path_buf, path_buf_size, "%s/run-%s-%ld-%u.log",
                               opts->record_dir, timestamp, (long)getpid(),
                               attempt);
        }
        if (written < 0 || (size_t)written >= path_buf_size) {
            fprintf(stderr, "error: run record path is too long\n");
            exit(1);
        }

        int fd = open(path_buf, O_WRONLY | O_CREAT | O_EXCL, 0666);
        if (fd >= 0) {
            FILE *record = fdopen(fd, "w");
            if (record == NULL) {
                int saved_errno = errno;
                close(fd);
                errno = saved_errno;
                die_errno("fdopen(record)");
            }
            return record;
        }
        if (errno != EEXIST) {
            die_errno("open(record)");
        }
    }

    fprintf(stderr, "error: failed to create unique run record in %s\n",
            opts->record_dir);
    exit(1);
}

static int format_unique_output_path(const char *requested, unsigned attempt,
                                     char *path_buf, size_t path_buf_size) {
    if (attempt == 0) {
        return snprintf(path_buf, path_buf_size, "%s", requested);
    }

    const char *last_slash = strrchr(requested, '/');
    const char *last_backslash = strrchr(requested, '\\');
    const char *last_sep = last_slash;
    if (last_backslash != NULL && (last_sep == NULL || last_backslash > last_sep)) {
        last_sep = last_backslash;
    }

    const char *name_start = last_sep == NULL ? requested : last_sep + 1;
    const char *dot = strrchr(name_start, '.');
    if (dot == NULL || dot == name_start) {
        return snprintf(path_buf, path_buf_size, "%s-%u", requested, attempt);
    }

    size_t prefix_len = (size_t)(dot - requested);
    return snprintf(path_buf, path_buf_size, "%.*s-%u%s",
                    (int)prefix_len, requested, attempt, dot);
}

static FILE *open_unique_output_csv(const char *requested, char *path_buf,
                                    size_t path_buf_size) {
    for (unsigned attempt = 0; attempt < 1000; ++attempt) {
        int written =
            format_unique_output_path(requested, attempt, path_buf, path_buf_size);
        if (written < 0 || (size_t)written >= path_buf_size) {
            fprintf(stderr, "error: output CSV path is too long\n");
            exit(1);
        }

        int fd = open(path_buf, O_WRONLY | O_CREAT | O_EXCL, 0666);
        if (fd >= 0) {
            FILE *out = fdopen(fd, "w");
            if (out == NULL) {
                int saved_errno = errno;
                close(fd);
                errno = saved_errno;
                die_errno("fdopen(output)");
            }
            return out;
        }
        if (errno != EEXIST) {
            die_errno("open(output)");
        }
    }

    fprintf(stderr, "error: failed to create unique output CSV for %s\n",
            requested);
    exit(1);
}

static mapped_db_t map_db_file(const char *db_path) {
    mapped_db_t dbmap;
    memset(&dbmap, 0, sizeof(dbmap));

    long os_page_size_long = sysconf(_SC_PAGESIZE);
    if (os_page_size_long <= 0) {
        die_errno("sysconf(_SC_PAGESIZE)");
    }
    dbmap.os_page_size = (size_t)os_page_size_long;

    dbmap.fd = open(db_path, O_RDONLY);
    if (dbmap.fd < 0) {
        die_errno("open");
    }

    struct stat st;
    if (fstat(dbmap.fd, &st) != 0) {
        die_errno("fstat");
    }
    if (st.st_size < 100) {
        fprintf(stderr, "error: file too small to be a SQLite db\n");
        exit(1);
    }

    dbmap.file_size = (size_t)st.st_size;
    dbmap.os_page_count =
        (dbmap.file_size + dbmap.os_page_size - 1) / dbmap.os_page_size;
    dbmap.mapping = mmap(NULL, dbmap.file_size, PROT_READ, MAP_SHARED, dbmap.fd, 0);
    if (dbmap.mapping == MAP_FAILED) {
        die_errno("mmap");
    }

    const uint8_t *hdr = (const uint8_t *)dbmap.mapping;
    if (memcmp(hdr, "SQLite format 3\0", 16) != 0) {
        fprintf(stderr, "error: not a SQLite database\n");
        exit(1);
    }

    uint16_t raw_page_size = rd_be16(hdr + 16);
    dbmap.sqlite_page_size =
        (raw_page_size == 1) ? 65536u : (size_t)raw_page_size;
    dbmap.sqlite_page_count =
        (uint32_t)((dbmap.file_size + dbmap.sqlite_page_size - 1) /
                   dbmap.sqlite_page_size);

    return dbmap;
}

static void unmap_db_file(mapped_db_t *dbmap) {
    if (dbmap->mapping && dbmap->mapping != MAP_FAILED) {
        munmap(dbmap->mapping, dbmap->file_size);
    }
    if (dbmap->fd >= 0) {
        close(dbmap->fd);
    }
}

static void fill_mincore_vec(const mapped_db_t *dbmap, unsigned char *vec) {
    if (mincore(dbmap->mapping, dbmap->file_size, vec) != 0) {
        die_errno("mincore");
    }
}

static size_t count_resident_sqlite_pages(const mapped_db_t *dbmap) {
    unsigned char *vec = calloc(dbmap->os_page_count, sizeof(unsigned char));
    if (vec == NULL) {
        die_errno("calloc");
    }

    fill_mincore_vec(dbmap, vec);

    size_t resident_count = 0;
    for (uint32_t page_no = 1; page_no <= dbmap->sqlite_page_count; ++page_no) {
        uint64_t sqlite_begin =
            (uint64_t)(page_no - 1) * dbmap->sqlite_page_size;
        uint64_t sqlite_end = sqlite_begin + dbmap->sqlite_page_size;
        size_t first_os_page = (size_t)(sqlite_begin / dbmap->os_page_size);
        size_t last_os_page = (size_t)((sqlite_end - 1) / dbmap->os_page_size);
        bool resident = true;

        for (size_t os_idx = first_os_page; os_idx <= last_os_page; ++os_idx) {
            if ((vec[os_idx] & 1U) == 0) {
                resident = false;
                break;
            }
        }
        if (resident) {
            ++resident_count;
        }
    }

    free(vec);
    return resident_count;
}

static void append_resident_range(uint32_t **starts, uint32_t **ends,
                                  size_t *count, size_t *capacity,
                                  uint32_t start, uint32_t end) {
    if (*count == *capacity) {
        size_t new_capacity = *capacity == 0 ? 16 : *capacity * 2;
        uint32_t *new_starts = malloc(new_capacity * sizeof(uint32_t));
        uint32_t *new_ends = malloc(new_capacity * sizeof(uint32_t));
        if (new_starts == NULL || new_ends == NULL) {
            free(new_starts);
            free(new_ends);
            fprintf(stderr, "error: failed to grow resident range arrays\n");
            exit(1);
        }
        if (*count > 0) {
            memcpy(new_starts, *starts, *count * sizeof(uint32_t));
            memcpy(new_ends, *ends, *count * sizeof(uint32_t));
        }
        free(*starts);
        free(*ends);
        *starts = new_starts;
        *ends = new_ends;
        *capacity = new_capacity;
    }

    (*starts)[*count] = start;
    (*ends)[*count] = end;
    ++(*count);
}

static void report_resident_sqlite_page_distribution(const mapped_db_t *dbmap,
                                                     const char *label,
                                                     FILE *record,
                                                     bool echo_stderr) {
    unsigned char *vec = calloc(dbmap->os_page_count, sizeof(unsigned char));
    if (vec == NULL) {
        die_errno("calloc");
    }

    fill_mincore_vec(dbmap, vec);

    size_t resident_count = 0;
    uint32_t first_resident = 0;
    uint32_t last_resident = 0;
    uint32_t *range_starts = NULL;
    uint32_t *range_ends = NULL;
    size_t range_count = 0;
    size_t range_capacity = 0;
    bool in_run = false;
    uint32_t run_start = 0;
    size_t resident_in_first_1pct = 0;
    size_t resident_in_first_5pct = 0;
    size_t resident_in_first_10pct = 0;
    uint32_t limit_1pct = (dbmap->sqlite_page_count + 99u) / 100u;
    uint32_t limit_5pct = (dbmap->sqlite_page_count + 19u) / 20u;
    uint32_t limit_10pct = (dbmap->sqlite_page_count + 9u) / 10u;

    for (uint32_t page_no = 1; page_no <= dbmap->sqlite_page_count; ++page_no) {
        uint64_t sqlite_begin =
            (uint64_t)(page_no - 1) * dbmap->sqlite_page_size;
        uint64_t sqlite_end = sqlite_begin + dbmap->sqlite_page_size;
        size_t first_os_page = (size_t)(sqlite_begin / dbmap->os_page_size);
        size_t last_os_page = (size_t)((sqlite_end - 1) / dbmap->os_page_size);
        bool resident = true;

        for (size_t os_idx = first_os_page; os_idx <= last_os_page; ++os_idx) {
            if ((vec[os_idx] & 1U) == 0) {
                resident = false;
                break;
            }
        }

        if (resident) {
            ++resident_count;
            if (first_resident == 0) {
                first_resident = page_no;
            }
            last_resident = page_no;
            if (page_no <= limit_1pct) {
                ++resident_in_first_1pct;
            }
            if (page_no <= limit_5pct) {
                ++resident_in_first_5pct;
            }
            if (page_no <= limit_10pct) {
                ++resident_in_first_10pct;
            }
            if (!in_run) {
                in_run = true;
                run_start = page_no;
            }
        } else if (in_run) {
            append_resident_range(&range_starts, &range_ends, &range_count,
                                  &range_capacity, run_start, page_no - 1);
            in_run = false;
        }
    }

    if (in_run) {
        append_resident_range(&range_starts, &range_ends, &range_count,
                              &range_capacity, run_start,
                              dbmap->sqlite_page_count);
    }

    emit_line_to(record, echo_stderr,
                 "%s resident-page distribution: count=%zu first=%" PRIu32
                 " last=%" PRIu32 " first_1%%=%zu first_5%%=%zu first_10%%=%zu\n",
                 label, resident_count, first_resident, last_resident,
                 resident_in_first_1pct, resident_in_first_5pct,
                 resident_in_first_10pct);

    size_t stderr_ranges = range_count < 8 ? range_count : 8;
    for (size_t i = 0; i < range_count; ++i) {
        if (record != NULL) {
            write_record_line(record, "%s resident range[%zu]=%" PRIu32 "-%" PRIu32 "\n",
                              label, i, range_starts[i], range_ends[i]);
        }
        if (echo_stderr && i < stderr_ranges) {
            fprintf(stderr, "%s resident range[%zu]=%" PRIu32 "-%" PRIu32 "\n",
                    label, i, range_starts[i], range_ends[i]);
        }
    }
    if (echo_stderr && range_count > stderr_ranges) {
        fprintf(stderr, "%s resident ranges truncated: total=%zu\n",
                label, range_count);
    }

    free(range_starts);
    free(range_ends);
    free(vec);
}

static void sync_db_pages(const mapped_db_t *dbmap, bool debug) {
    errno = 0;
    if (msync(dbmap->mapping, dbmap->file_size, MS_SYNC) != 0) {
        int saved_errno = errno;
        if (debug) {
            fprintf(stderr,
                    "warn: msync(MS_SYNC) failed: addr=%p len=%zu errno=%d (%s)\n",
                    dbmap->mapping, dbmap->file_size, saved_errno,
                    strerror(saved_errno));
        }
    } else if (debug) {
        fprintf(stderr, "msync(MS_SYNC) succeeded: addr=%p len=%zu\n",
                dbmap->mapping, dbmap->file_size);
    }

    errno = 0;
    if (fsync(dbmap->fd) != 0) {
        int saved_errno = errno;
        if (debug) {
            fprintf(stderr, "warn: fsync(db fd) failed: fd=%d errno=%d (%s)\n",
                    dbmap->fd, saved_errno, strerror(saved_errno));
        }
    } else if (debug) {
        fprintf(stderr, "fsync(db fd) succeeded: fd=%d\n", dbmap->fd);
    }
}

static void run_madvise_step(const mapped_db_t *dbmap, int advice_flag,
                             const char *advice_name, bool debug) {
    uintptr_t addr_value = (uintptr_t)dbmap->mapping;
    size_t addr_mod_page = addr_value % dbmap->os_page_size;
    size_t len_mod_page = dbmap->file_size % dbmap->os_page_size;

    errno = 0;
    if (madvise(dbmap->mapping, dbmap->file_size, advice_flag) != 0) {
        int saved_errno = errno;
        fprintf(stderr,
                "error: madvise(%s) failed: addr=%p len=%zu os_page_size=%zu "
                "addr_mod_page=%zu len_mod_page=%zu errno=%d (%s)\n",
                advice_name, dbmap->mapping, dbmap->file_size,
                dbmap->os_page_size, addr_mod_page, len_mod_page,
                saved_errno, strerror(saved_errno));
        exit(1);
    }

    if (debug) {
        fprintf(stderr,
                "madvise(%s) succeeded: addr=%p len=%zu os_page_size=%zu "
                "addr_mod_page=%zu len_mod_page=%zu\n",
                advice_name, dbmap->mapping, dbmap->file_size,
                dbmap->os_page_size, addr_mod_page, len_mod_page);
    }
}

static void apply_cold_advice(const mapped_db_t *dbmap, cold_advice_t advice,
                              bool debug) {
    if (advice == COLD_ADVICE_NONE) {
        if (debug) {
            fprintf(stderr, "cold advice disabled; skipping madvise steps\n");
        }
        return;
    }

    run_madvise_step(dbmap, MADV_COLD, "MADV_COLD", debug);

    if (advice >= COLD_ADVICE_PAGEOUT) {
#ifdef MADV_PAGEOUT
        run_madvise_step(dbmap, MADV_PAGEOUT, "MADV_PAGEOUT", debug);
#else
        if (debug) {
            fprintf(stderr, "warn: MADV_PAGEOUT unsupported at build time; skipping pageout step\n");
        }
#endif
    }

    if (advice >= COLD_ADVICE_DONTNEED) {
        run_madvise_step(dbmap, MADV_DONTNEED, "MADV_DONTNEED", debug);
    }
}

static void run_helper_script(const char *script_path, const char *kind,
                              bool use_sudo, bool debug, FILE *record) {
    pid_t pid;
    int status;

    if (script_path == NULL) {
        return;
    }
    if (script_path[0] == '\0') {
        fprintf(stderr, "error: --%s path must not be empty\n", kind);
        exit(1);
    }
    if (use_sudo && geteuid() == 0) {
        use_sudo = false;
    }

    if (debug) {
        fprintf(stderr, "running %s helper%s: %s\n", kind,
                use_sudo ? " via sudo -n" : "", script_path);
    }

    pid = fork();
    if (pid < 0) {
        die_errno("fork(helper-script)");
    }

    if (pid == 0) {
        if (use_sudo) {
            char *const argv[] = {
                (char *)"sudo",
                (char *)"-n",
                (char *)script_path,
                NULL,
            };
            execvp("sudo", argv);
            fprintf(stderr, "error: execvp(sudo -n %s) failed: errno=%d (%s)\n",
                    script_path, errno, strerror(errno));
        } else {
            char *const argv[] = {(char *)script_path, NULL};
            execv(script_path, argv);
            fprintf(stderr, "error: execv(%s) failed: errno=%d (%s)\n",
                    script_path, errno, strerror(errno));
        }
        _exit(127);
    }

    if (waitpid(pid, &status, 0) < 0) {
        die_errno("waitpid(helper-script)");
    }

    if (WIFEXITED(status)) {
        int code = WEXITSTATUS(status);
        if (code != 0) {
            fprintf(stderr,
                    "error: %s helper failed%s with exit code %d: %s\n",
                    kind, use_sudo ? " via sudo -n" : "", code, script_path);
            exit(1);
        }
    } else if (WIFSIGNALED(status)) {
        fprintf(stderr,
                "error: %s helper%s was killed by signal %d: %s\n",
                kind, use_sudo ? " via sudo -n" : "", WTERMSIG(status),
                script_path);
        exit(1);
    } else {
        fprintf(stderr, "error: %s helper%s ended unexpectedly: %s\n",
                kind, use_sudo ? " via sudo -n" : "", script_path);
        exit(1);
    }

    write_record_line(record, "%s_result=success\n", kind);
    if (debug) {
        fprintf(stderr, "%s helper%s succeeded: %s\n", kind,
                use_sudo ? " via sudo -n" : "", script_path);
    }
}

static void workload_push(workload_t *workload, workload_op_t op) {
    if (workload->count == workload->capacity) {
        size_t new_capacity = workload->capacity == 0 ? 1024 : workload->capacity * 2;
        workload_op_t *new_ops =
            realloc(workload->ops, new_capacity * sizeof(workload_op_t));
        if (new_ops == NULL) {
            die_errno("realloc(workload)");
        }
        workload->ops = new_ops;
        workload->capacity = new_capacity;
    }
    workload->ops[workload->count++] = op;
}

static const char *op_type_name(op_type_t type) {
    switch (type) {
        case OP_READ:
            return "read";
        case OP_UPDATE:
            return "update";
        case OP_INSERT:
            return "insert";
        case OP_SCAN:
            return "scan";
        case OP_READMODIFYWRITE:
            return "readmodifywrite";
    }
    return "unknown";
}

static workload_t load_workload(const char *path) {
    FILE *fp = fopen(path, "r");
    char line[256];
    size_t lineno = 0;
    workload_t workload;
    memset(&workload, 0, sizeof(workload));

    if (fp == NULL) {
        perror(path);
        exit(1);
    }

    while (fgets(line, sizeof(line), fp) != NULL) {
        char op_text[32];
        unsigned long long id = 0;
        unsigned long long scan_len = 0;
        int parsed;
        workload_op_t op;
        ++lineno;

        trim_whitespace(line);
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }

        memset(&op, 0, sizeof(op));
        parsed = sscanf(line, "%31s %llu %llu", op_text, &id, &scan_len);
        if (parsed < 2) {
            fprintf(stderr, "error: invalid workload line %zu in %s: %s\n",
                    lineno, path, line);
            exit(1);
        }

        if (strcmp(op_text, "read") == 0) {
            op.type = OP_READ;
        } else if (strcmp(op_text, "update") == 0) {
            op.type = OP_UPDATE;
        } else if (strcmp(op_text, "insert") == 0) {
            op.type = OP_INSERT;
        } else if (strcmp(op_text, "scan") == 0) {
            op.type = OP_SCAN;
            if (parsed != 3 || scan_len == 0 || scan_len > UINT32_MAX) {
                fprintf(stderr,
                        "error: scan requires positive length on line %zu in %s\n",
                        lineno, path);
                exit(1);
            }
            op.scan_len = (uint32_t)scan_len;
        } else if (strcmp(op_text, "readmodifywrite") == 0) {
            op.type = OP_READMODIFYWRITE;
        } else {
            fprintf(stderr,
                    "error: unsupported workload op '%s' on line %zu in %s\n",
                    op_text, lineno, path);
            exit(1);
        }

        if (id > UINT32_MAX) {
            fprintf(stderr, "error: workload id out of range on line %zu in %s\n",
                    lineno, path);
            exit(1);
        }
        op.target_id = (uint32_t)id;
        if (op.type != OP_SCAN && parsed != 2) {
            fprintf(stderr,
                    "error: operation '%s' expects exactly one id on line %zu in %s\n",
                    op_text, lineno, path);
            exit(1);
        }
        workload_push(&workload, op);
    }

    fclose(fp);

    if (workload.count == 0) {
        fprintf(stderr, "error: workload file is empty: %s\n", path);
        exit(1);
    }

    return workload;
}

static void free_workload(workload_t *workload) {
    free(workload->ops);
    workload->ops = NULL;
    workload->count = 0;
    workload->capacity = 0;
}

static void sqlite_open_if_needed(sqlite_ctx_t *ctx, const options_t *opts) {
    if (ctx->opened) {
        return;
    }

    int rc = sqlite3_open_v2(opts->db_path, &ctx->db,
                             SQLITE_OPEN_READWRITE | SQLITE_OPEN_NOMUTEX, NULL);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_open_v2", rc);
    }

    rc = sqlite3_busy_timeout(ctx->db, 5000);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_busy_timeout", rc);
    }

    char pragma_sql[256];
    snprintf(pragma_sql, sizeof(pragma_sql), "PRAGMA mmap_size = %" PRId64 ";",
             opts->mmap_size);
    exec_sql(ctx->db, pragma_sql);
    exec_sql(ctx->db, "PRAGMA cache_size = 0;");
    ctx->opened = true;
}

static void sqlite_init_schema_if_needed(sqlite_ctx_t *ctx) {
    if (ctx->schema_initialized) {
        return;
    }

    int rc = sqlite3_prepare_v2(ctx->db,
                                "SELECT payload FROM items WHERE id = ?1;",
                                -1, &ctx->read_stmt, NULL);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_prepare_v2(read)", rc);
    }

    rc = sqlite3_prepare_v2(ctx->db,
                            "UPDATE items SET payload = randomblob(100) WHERE id = ?1;",
                            -1, &ctx->update_stmt, NULL);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_prepare_v2(update)", rc);
    }

    rc = sqlite3_prepare_v2(ctx->db,
                            "INSERT INTO items(id, k1, k2, payload) "
                            "VALUES(?1, printf('group_%04d', ?1 % 1000), "
                            "printf('tag_%06d', ?1), randomblob(100)) "
                            "ON CONFLICT(id) DO UPDATE SET "
                            "k1 = excluded.k1, k2 = excluded.k2, payload = excluded.payload;",
                            -1, &ctx->insert_stmt, NULL);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_prepare_v2(insert)", rc);
    }

    rc = sqlite3_prepare_v2(ctx->db,
                            "SELECT payload FROM items WHERE id >= ?1 "
                            "ORDER BY id LIMIT ?2;",
                            -1, &ctx->scan_stmt, NULL);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, "sqlite3_prepare_v2(scan)", rc);
    }

    ctx->schema_initialized = true;
}

static void sqlite_close_ctx(sqlite_ctx_t *ctx) {
    if (ctx->read_stmt != NULL) {
        sqlite3_finalize(ctx->read_stmt);
    }
    if (ctx->update_stmt != NULL) {
        sqlite3_finalize(ctx->update_stmt);
    }
    if (ctx->insert_stmt != NULL) {
        sqlite3_finalize(ctx->insert_stmt);
    }
    if (ctx->scan_stmt != NULL) {
        sqlite3_finalize(ctx->scan_stmt);
    }
    if (ctx->db != NULL) {
        sqlite3_close(ctx->db);
    }
}

static uint64_t timespec_diff_ns(const struct timespec *start,
                                 const struct timespec *end) {
    time_t sec = end->tv_sec - start->tv_sec;
    long nsec = end->tv_nsec - start->tv_nsec;
    if (nsec < 0) {
        --sec;
        nsec += 1000000000L;
    }
    return (uint64_t)sec * 1000000000ull + (uint64_t)nsec;
}

static long rusage_delta(long after, long before) {
    return after - before;
}

static void write_csv_header(FILE *out) {
    fprintf(out,
            "op_no,op_type,target_id,rows_returned,bytes_returned,elapsed_ns,"
            "majflt_delta,minflt_delta\n");
}

static void reset_and_bind_id(sqlite_ctx_t *ctx, sqlite3_stmt *stmt,
                              uint32_t target_id, const char *what) {
    int rc;

    sqlite3_reset(stmt);
    sqlite3_clear_bindings(stmt);
    rc = sqlite3_bind_int64(stmt, 1, (sqlite3_int64)target_id);
    if (rc != SQLITE_OK) {
        die_sqlite(ctx->db, what, rc);
    }
}

static void run_select_stmt(sqlite_ctx_t *ctx, sqlite3_stmt *stmt,
                            int *rows_returned, int *bytes_returned,
                            const char *what) {
    int rc;

    while ((rc = sqlite3_step(stmt)) == SQLITE_ROW) {
        const void *blob = sqlite3_column_blob(stmt, 0);
        int n = sqlite3_column_bytes(stmt, 0);
        if (blob != NULL || n == 0) {
            *bytes_returned += n;
        }
        ++(*rows_returned);
    }
    if (rc != SQLITE_DONE) {
        die_sqlite(ctx->db, what, rc);
    }
}

static benchmark_summary_t run_benchmark(sqlite_ctx_t *ctx,
                                         const workload_t *workload,
                                         FILE *out) {
    benchmark_summary_t summary;
    memset(&summary, 0, sizeof(summary));

    for (size_t i = 0; i < workload->count; ++i) {
        const workload_op_t *op = &workload->ops[i];
        int rc;
        int rows_returned = 0;
        int bytes_returned = 0;
        struct rusage ru_before;
        struct rusage ru_after;
        struct timespec ts_before;
        struct timespec ts_after;
        const char *op_name = op_type_name(op->type);

        if (getrusage(RUSAGE_SELF, &ru_before) != 0) {
            die_errno("getrusage(before)");
        }
        if (clock_gettime(CLOCK_MONOTONIC, &ts_before) != 0) {
            die_errno("clock_gettime(before)");
        }

        switch (op->type) {
            case OP_READ:
                reset_and_bind_id(ctx, ctx->read_stmt, op->target_id,
                                  "sqlite3_bind_int64(read)");
                run_select_stmt(ctx, ctx->read_stmt, &rows_returned,
                                &bytes_returned, "sqlite3_step(read)");
                break;
            case OP_UPDATE:
                reset_and_bind_id(ctx, ctx->update_stmt, op->target_id,
                                  "sqlite3_bind_int64(update)");
                rc = sqlite3_step(ctx->update_stmt);
                if (rc != SQLITE_DONE) {
                    die_sqlite(ctx->db, "sqlite3_step(update)", rc);
                }
                rows_returned = sqlite3_changes(ctx->db);
                break;
            case OP_INSERT:
                reset_and_bind_id(ctx, ctx->insert_stmt, op->target_id,
                                  "sqlite3_bind_int64(insert)");
                rc = sqlite3_step(ctx->insert_stmt);
                if (rc != SQLITE_DONE) {
                    die_sqlite(ctx->db, "sqlite3_step(insert)", rc);
                }
                rows_returned = sqlite3_changes(ctx->db);
                break;
            case OP_SCAN:
                reset_and_bind_id(ctx, ctx->scan_stmt, op->target_id,
                                  "sqlite3_bind_int64(scan id)");
                rc = sqlite3_bind_int64(ctx->scan_stmt, 2,
                                        (sqlite3_int64)op->scan_len);
                if (rc != SQLITE_OK) {
                    die_sqlite(ctx->db, "sqlite3_bind_int64(scan len)", rc);
                }
                run_select_stmt(ctx, ctx->scan_stmt, &rows_returned,
                                &bytes_returned, "sqlite3_step(scan)");
                break;
            case OP_READMODIFYWRITE:
                reset_and_bind_id(ctx, ctx->read_stmt, op->target_id,
                                  "sqlite3_bind_int64(rmw read)");
                run_select_stmt(ctx, ctx->read_stmt, &rows_returned,
                                &bytes_returned, "sqlite3_step(rmw read)");
                reset_and_bind_id(ctx, ctx->update_stmt, op->target_id,
                                  "sqlite3_bind_int64(rmw update)");
                rc = sqlite3_step(ctx->update_stmt);
                if (rc != SQLITE_DONE) {
                    die_sqlite(ctx->db, "sqlite3_step(rmw update)", rc);
                }
                break;
        }

        if (clock_gettime(CLOCK_MONOTONIC, &ts_after) != 0) {
            die_errno("clock_gettime(after)");
        }
        if (getrusage(RUSAGE_SELF, &ru_after) != 0) {
            die_errno("getrusage(after)");
        }

        uint64_t elapsed_ns = timespec_diff_ns(&ts_before, &ts_after);
        long majflt_delta = rusage_delta(ru_after.ru_majflt, ru_before.ru_majflt);
        long minflt_delta = rusage_delta(ru_after.ru_minflt, ru_before.ru_minflt);

        if (i == 0) {
            summary.first_query_ns = elapsed_ns;
        }
        summary.total_ns += elapsed_ns;
        summary.total_majflt += majflt_delta;
        summary.total_minflt += minflt_delta;

        fprintf(out, "%zu,%s,%" PRIu32 ",%d,%d,%" PRIu64 ",%ld,%ld\n",
                i + 1, op_name, op->target_id, rows_returned, bytes_returned,
                elapsed_ns, majflt_delta, minflt_delta);
    }

    return summary;
}

int main(int argc, char **argv) {
    options_t opts;
    sqlite_ctx_t sqlite_ctx;
    mapped_db_t dbmap;
    workload_t workload;
    benchmark_summary_t summary;
    FILE *record;
    FILE *out;
    char record_path[4096];
    char output_path[4096];

    parse_args(argc, argv, &opts);
    memset(&sqlite_ctx, 0, sizeof(sqlite_ctx));
    record = open_run_record(&opts, record_path, sizeof(record_path));

    workload = load_workload(opts.workload_path);
    dbmap = map_db_file(opts.db_path);
    if (opts.mmap_size == 0) {
        opts.mmap_size = (int64_t)dbmap.file_size;
    }
    out = open_unique_output_csv(opts.output_csv, output_path,
                                 sizeof(output_path));

    if (opts.sqlite_open_timing == SQLITE_OPEN_BEFORE_COLD) {
        sqlite_open_if_needed(&sqlite_ctx, &opts);
    }
    if (opts.schema_init_timing == SCHEMA_INIT_BEFORE_COLD) {
        if (!sqlite_ctx.opened) {
            sqlite_open_if_needed(&sqlite_ctx, &opts);
        }
        sqlite_init_schema_if_needed(&sqlite_ctx);
    }

    write_record_line(record, "benchmark_harness run record\n");
    write_record_line(record, "record_path=%s\n", record_path);
    write_record_line(record, "db=%s\n", opts.db_path);
    write_record_line(record, "workload=%s\n", opts.workload_path);
    write_record_line(record, "output=%s\n", output_path);
    write_record_line(record, "cold_advice=%s\n",
                      cold_advice_name(opts.cold_advice));
    write_record_line(record, "drop_caches_script=%s\n",
                      opts.drop_caches_script != NULL ?
                      opts.drop_caches_script : "(none)");
    write_record_line(record, "drop_caches_use_sudo=%s\n",
                      opts.drop_caches_use_sudo ? "true" : "false");
    write_record_line(record, "post_cold_script=%s\n",
                      opts.post_cold_script != NULL ?
                      opts.post_cold_script : "(none)");
    write_record_line(record, "sqlite_open_timing=%s\n",
                      opts.sqlite_open_timing == SQLITE_OPEN_BEFORE_COLD ?
                      "before-cold" : "after-cold");
    write_record_line(record, "schema_init_timing=%s\n",
                      opts.schema_init_timing == SCHEMA_INIT_BEFORE_COLD ?
                      "before-cold" : "after-cold");
    write_record_line(record,
                      "file_size=%zu sqlite_page_size=%zu sqlite_pages=%" PRIu32
                      " mmap_size=%" PRId64 " workload_ops=%zu\n\n",
                      dbmap.file_size, dbmap.sqlite_page_size,
                      dbmap.sqlite_page_count, opts.mmap_size, workload.count);
    fprintf(stderr, "benchmark record: %s\n", record_path);

    if (opts.debug) {
        fprintf(stderr,
                "db=%s workload=%s file_size=%zu sqlite_page_size=%zu sqlite_pages=%" PRIu32
                " mmap_size=%" PRId64 " workload_ops=%zu output=%s\n",
                opts.db_path, opts.workload_path, dbmap.file_size,
                dbmap.sqlite_page_size, dbmap.sqlite_page_count, opts.mmap_size,
                workload.count, output_path);
        fprintf(stderr,
                "sqlite_open_timing=%s schema_init_timing=%s\n",
                opts.sqlite_open_timing == SQLITE_OPEN_BEFORE_COLD ? "before-cold" : "after-cold",
                opts.schema_init_timing == SCHEMA_INIT_BEFORE_COLD ? "before-cold" : "after-cold");
    }

    size_t resident_before_cold = count_resident_sqlite_pages(&dbmap);
    emit_line(record, "resident SQLite pages before madvise: %zu/%" PRIu32 "\n",
              resident_before_cold, dbmap.sqlite_page_count);
    report_resident_sqlite_page_distribution(&dbmap, "before madvise", record,
                                             true);

    sync_db_pages(&dbmap, opts.debug);
    apply_cold_advice(&dbmap, opts.cold_advice, opts.debug);
    run_helper_script(opts.drop_caches_script, "drop_caches_script",
                      opts.drop_caches_use_sudo, opts.debug, record);
    run_helper_script(opts.post_cold_script, "post_cold_script",
                      false, opts.debug, record);

    if (opts.sqlite_open_timing == SQLITE_OPEN_AFTER_COLD) {
        sqlite_close_ctx(&sqlite_ctx);
        memset(&sqlite_ctx, 0, sizeof(sqlite_ctx));
        sqlite_open_if_needed(&sqlite_ctx, &opts);
    }
    if (opts.schema_init_timing == SCHEMA_INIT_AFTER_COLD) {
        if (!sqlite_ctx.opened) {
            sqlite_open_if_needed(&sqlite_ctx, &opts);
        }
        sqlite_init_schema_if_needed(&sqlite_ctx);
    } else if (!sqlite_ctx.schema_initialized) {
        sqlite_init_schema_if_needed(&sqlite_ctx);
    }

    size_t resident_after_cold = count_resident_sqlite_pages(&dbmap);
    emit_line(record, "resident SQLite pages after madvise:  %zu/%" PRIu32 "\n",
              resident_after_cold, dbmap.sqlite_page_count);
    report_resident_sqlite_page_distribution(&dbmap, "after madvise", record,
                                             true);

    write_csv_header(out);
    summary = run_benchmark(&sqlite_ctx, &workload, out);

    fclose(out);

    emit_line(record,
              "ops=%zu avg_latency_us=%.2f total_majflt=%ld total_minflt=%ld "
              "first_query_latency_us=%.2f\n",
              workload.count,
              (double)summary.total_ns / (double)workload.count / 1000.0,
              summary.total_majflt, summary.total_minflt,
              (double)summary.first_query_ns / 1000.0);

    size_t resident_after_run = count_resident_sqlite_pages(&dbmap);
    emit_line(record, "resident SQLite pages after run:      %zu/%" PRIu32 "\n",
              resident_after_run, dbmap.sqlite_page_count);
    report_resident_sqlite_page_distribution(&dbmap, "after run", record,
                                             false);

    sqlite_close_ctx(&sqlite_ctx);
    unmap_db_file(&dbmap);
    free_workload(&workload);
    fclose(record);
    return 0;
}
