/*
 * residency_checker.c
 *
 * Reports whether each SQLite database page is currently resident in memory.
 * The program maps the database file, asks mincore() for OS-page residency,
 * then converts those OS-page bits into per-SQLite-page CSV rows.
 *
 * SQLite page size can be supplied by the caller instead of being read from
 * the database header. When omitted, it defaults to 4096 bytes. This keeps the
 * checker from touching mapped database bytes before mincore(), avoiding
 * self-inflicted residency of the header page.
 *
 * Usage:
 *   ./residency_checker <database.db> <output.csv>
 *   ./residency_checker <database.db> <sqlite-page-size> <output.csv>
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEFAULT_SQLITE_PAGE_SIZE 4096u

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s <database.db> <output.csv>\n"
            "       %s <database.db> <sqlite-page-size> <output.csv>\n",
            prog, prog);
}

static size_t parse_sqlite_page_size(const char *s) {
    char *end = NULL;
    unsigned long value;

    errno = 0;
    value = strtoul(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || value > SIZE_MAX) {
        fprintf(stderr, "error: invalid SQLite page size: %s\n", s);
        exit(1);
    }

    if (value < 512 || value > 65536 ||
        (value & (value - 1)) != 0) {
        fprintf(stderr, "error: invalid SQLite page size: %lu\n", value);
        exit(1);
    }

    return (size_t)value;
}

static int write_csv(FILE *out,
                     size_t sqlite_page_size,
                     uint32_t sqlite_page_count,
                     size_t os_page_size,
                     const unsigned char *vec,
                     size_t vec_len) {
    if (fprintf(out, "page_number,is_resident\n") < 0) {
        return -1;
    }

    for (uint32_t page_number = 1; page_number <= sqlite_page_count; ++page_number) {
        uint64_t sqlite_begin = (uint64_t)(page_number - 1) * sqlite_page_size;
        uint64_t sqlite_end = sqlite_begin + sqlite_page_size;
        size_t first_os_page = (size_t)(sqlite_begin / os_page_size);
        size_t last_os_page = (size_t)((sqlite_end - 1) / os_page_size);
        int is_resident = 1;

        if (last_os_page >= vec_len) {
            fprintf(stderr,
                    "error: page %" PRIu32 " maps past mincore vector\n",
                    page_number);
            return -1;
        }

        /* Treat a SQLite page as resident only when every covering OS page
         * reports resident. When SQLite pages are smaller than OS pages,
         * several SQLite pages may share the same residency bit. */
        for (size_t os_idx = first_os_page; os_idx <= last_os_page; ++os_idx) {
            if ((vec[os_idx] & 1U) == 0) {
                is_resident = 0;
                break;
            }
        }

        if (fprintf(out, "%" PRIu32 ",%d\n", page_number, is_resident) < 0) {
            return -1;
        }
    }

    return 0;
}

int main(int argc, char *argv[]) {
    if (argc != 3 && argc != 4) {
        usage(argv[0]);
        return 1;
    }

    const char *db_path = argv[1];
    size_t sqlite_page_size = DEFAULT_SQLITE_PAGE_SIZE;
    const char *csv_path = argv[2];
    if (argc == 4) {
        sqlite_page_size = parse_sqlite_page_size(argv[2]);
        csv_path = argv[3];
    }

    long os_page_size_long = sysconf(_SC_PAGESIZE);
    if (os_page_size_long <= 0) {
        perror("sysconf(_SC_PAGESIZE)");
        return 1;
    }
    size_t os_page_size = (size_t)os_page_size_long;

    int fd = open(db_path, O_RDONLY);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    struct stat st;
    if (fstat(fd, &st) != 0) {
        perror("fstat");
        close(fd);
        return 1;
    }

    if (st.st_size <= 0) {
        fprintf(stderr, "error: database file is empty\n");
        close(fd);
        return 1;
    }

    size_t file_size = (size_t)st.st_size;
    void *mapping = mmap(NULL, file_size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (mapping == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    uint32_t sqlite_page_count =
        (uint32_t)((file_size + sqlite_page_size - 1) / sqlite_page_size);
    size_t os_page_count = (file_size + os_page_size - 1) / os_page_size;

    unsigned char *vec = calloc(os_page_count, sizeof(unsigned char));
    if (vec == NULL) {
        perror("calloc");
        munmap(mapping, file_size);
        close(fd);
        return 1;
    }

    if (mincore(mapping, file_size, vec) != 0) {
        perror("mincore");
        free(vec);
        munmap(mapping, file_size);
        close(fd);
        return 1;
    }

    FILE *out = fopen(csv_path, "w");
    if (out == NULL) {
        perror("fopen");
        free(vec);
        munmap(mapping, file_size);
        close(fd);
        return 1;
    }

    int rc = write_csv(out, sqlite_page_size, sqlite_page_count,
                       os_page_size, vec, os_page_count);
    if (rc != 0) {
        if (ferror(out)) {
            perror("write output");
        }
        fclose(out);
        free(vec);
        munmap(mapping, file_size);
        close(fd);
        return 1;
    }

    if (fclose(out) != 0) {
        perror("fclose");
        free(vec);
        munmap(mapping, file_size);
        close(fd);
        return 1;
    }

    fprintf(stderr,
            "db=%s file_size=%zu sqlite_page_size=%zu sqlite_pages=%" PRIu32
            " os_page_size=%zu os_pages=%zu output=%s\n",
            db_path, file_size, sqlite_page_size, sqlite_page_count,
            os_page_size, os_page_count, csv_path);

    free(vec);
    munmap(mapping, file_size);
    close(fd);
    return 0;
}

