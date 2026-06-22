#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"

typedef struct {
    uint32_t number;
    char type[32];
    uint64_t offset;
    int residency;
    int rank;
    int op_index;
    int success;
} page_t;

typedef struct {
    int index;
    uint64_t offset, length;
    uint32_t first_page, last_page;
    int backend_calls;
    uint64_t bytes_completed;
    bool success;
    int error_number;
} operation_t;

static void fail(const char *message) {
    fprintf(stderr, "prefetch_runner: %s\n", message);
    exit(2);
}

static char *read_all(const char *path) {
    FILE *file = fopen(path, "rb");
    if (!file) fail("cannot open job JSON");
    if (fseek(file, 0, SEEK_END) || ftell(file) < 0) fail("cannot size job JSON");
    long size = ftell(file);
    rewind(file);
    char *data = malloc((size_t)size + 1);
    if (!data || fread(data, 1, (size_t)size, file) != (size_t)size) fail("cannot read job JSON");
    data[size] = 0;
    fclose(file);
    return data;
}

static cJSON *required(cJSON *parent, const char *name) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(parent, name);
    if (!item) fail("job JSON is missing a required field");
    return item;
}

static const char *string_field(cJSON *parent, const char *name) {
    cJSON *item = required(parent, name);
    if (!cJSON_IsString(item) || !item->valuestring) fail("job JSON string field is invalid");
    return item->valuestring;
}

static int nullable_int(cJSON *parent, const char *name, int *is_null) {
    cJSON *item = required(parent, name);
    if (cJSON_IsNull(item)) { *is_null = 1; return 0; }
    if (!cJSON_IsNumber(item) || item->valuedouble != item->valueint) fail("job JSON integer field is invalid");
    *is_null = 0;
    return item->valueint;
}

static uint32_t sqlite_page_size(int fd) {
    unsigned char header[100];
    if (pread(fd, header, sizeof header, 0) != (ssize_t)sizeof header || memcmp(header, "SQLite format 3\0", 16))
        fail("database has an invalid SQLite header");
    uint32_t size = ((uint32_t)header[16] << 8) | header[17];
    if (size == 1) size = 65536;
    if (size < 512 || size > 65536 || (size & (size - 1))) fail("invalid SQLite page size");
    return size;
}

static page_t *load_classification(const char *path, size_t *count) {
    FILE *file = fopen(path, "r");
    if (!file) fail("cannot open classification CSV");
    char line[256];
    if (!fgets(line, sizeof line, file) || strcmp(line, "page_number,page_type,file_offset\n") != 0)
        fail("classification CSV header is invalid");
    size_t used = 0, capacity = 256;
    page_t *pages = calloc(capacity, sizeof *pages);
    while (fgets(line, sizeof line, file)) {
        page_t page = {0};
        if (sscanf(line, "%" SCNu32 ",%31[^,],%" SCNu64, &page.number, page.type, &page.offset) != 3)
            fail("classification CSV row is invalid");
        if (used == capacity) { capacity *= 2; pages = realloc(pages, capacity * sizeof *pages); }
        if (!pages) fail("out of memory");
        pages[used++] = page;
    }
    fclose(file);
    *count = used;
    return pages;
}

static void load_profile(const char *path, page_t *pages, size_t count) {
    FILE *file = fopen(path, "r");
    if (!file) fail("cannot open training profile CSV");
    char line[512];
    if (!fgets(line, sizeof line, file)) fail("training profile is empty");
    while (fgets(line, sizeof line, file)) {
        uint32_t number; char type[32]; uint64_t offset; int residency, runs; double rate;
        if (sscanf(line, "%" SCNu32 ",%31[^,],%" SCNu64 ",%d,%d,%lf", &number, type, &offset, &residency, &runs, &rate) != 6)
            fail("training profile row is invalid");
        if (number == 0 || number > count || pages[number - 1].number != number || pages[number - 1].offset != offset)
            fail("training profile does not match classification");
        pages[number - 1].residency = residency;
    }
    fclose(file);
}

static bool interior(const page_t *p) { return !strcmp(p->type, "interior_table") || !strcmp(p->type, "interior_index"); }
static bool leaf(const page_t *p) { return !strcmp(p->type, "leaf_table") || !strcmp(p->type, "leaf_index"); }
static int by_offset(const void *a, const void *b) {
    const page_t *pa = *(page_t *const *)a, *pb = *(page_t *const *)b;
    return pa->offset < pb->offset ? -1 : pa->offset > pb->offset;
}
static int by_rank(const void *a, const void *b) {
    const page_t *pa = *(page_t *const *)a, *pb = *(page_t *const *)b;
    if (pa->residency != pb->residency) return pb->residency - pa->residency;
    return pa->number < pb->number ? -1 : pa->number > pb->number;
}

static uint64_t elapsed_us(struct timespec a, struct timespec b) {
    return (uint64_t)(b.tv_sec - a.tv_sec) * 1000000u + (uint64_t)(b.tv_nsec - a.tv_nsec) / 1000u;
}

static void add_error(cJSON *errors, uint64_t offset, uint64_t length, int err) {
    cJSON *item = cJSON_CreateObject();
    cJSON_AddNumberToObject(item, "offset", (double)offset);
    cJSON_AddNumberToObject(item, "length", (double)length);
    cJSON_AddNumberToObject(item, "errno", err);
    cJSON_AddStringToObject(item, "message", strerror(err));
    cJSON_AddItemToArray(errors, item);
}

static void write_json(const char *path, cJSON *root) {
    char *text = cJSON_Print(root);
    FILE *file = fopen(path, "w");
    if (!text || !file || fputs(text, file) == EOF || fputc('\n', file) == EOF || fclose(file)) fail("cannot write result JSON");
    free(text);
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "--job")) { fprintf(stderr, "Usage: %s --job <job.json>\n", argv[0]); return 2; }
    char *job_text = read_all(argv[2]);
    cJSON *job = cJSON_Parse(job_text);
    if (!job) fail("invalid job JSON");
    const char *cell_id = string_field(job, "cell_id");
    const char *backend = string_field(job, "backend");
    const char *strategy = string_field(job, "strategy");
    const char *variant = string_field(job, "variant");
    cJSON *memory_condition = required(job, "memory_condition");
    if (!cJSON_IsObject(memory_condition)) fail("invalid memory condition");
    cJSON *database = required(job, "database"), *classification = required(job, "classification");
    const char *db_path = string_field(database, "path"), *class_path = string_field(classification, "path");
    cJSON *profile = required(job, "training_profile"), *parameters = required(job, "parameters"), *output = required(job, "output");
    const char *result_path = string_field(output, "result_json"), *selected_path = string_field(output, "selected_pages_csv");
    int null_n, null_ik, null_lk;
    int n = nullable_int(parameters, "n", &null_n), ik = nullable_int(parameters, "interior_k", &null_ik), lk = nullable_int(parameters, "leaf_k", &null_lk);
    cJSON *chunk_item = required(parameters, "pread_chunk_bytes");
    if (!cJSON_IsNumber(chunk_item) || chunk_item->valuedouble <= 0) fail("invalid pread chunk size");
    uint64_t chunk_size = (uint64_t)chunk_item->valuedouble;
    int fd = open(db_path, O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st)) fail("cannot open database");
    uint32_t page_size = sqlite_page_size(fd);
    if (chunk_size % page_size) fail("pread chunk size is not SQLite-page aligned");
    size_t page_count;
    page_t *pages = load_classification(class_path, &page_count);
    if ((uint64_t)st.st_size != (uint64_t)page_count * page_size) fail("classification page count does not match database");
    if (!cJSON_IsNull(profile)) load_profile(string_field(profile, "path"), pages, page_count);

    page_t **eligible_i = malloc(page_count * sizeof *eligible_i), **eligible_l = malloc(page_count * sizeof *eligible_l);
    page_t **selected = malloc(page_count * sizeof *selected);
    size_t ni = 0, nl = 0, ns = 0;
    for (size_t i = 0; i < page_count; ++i) { if (interior(&pages[i])) eligible_i[ni++] = &pages[i]; if (leaf(&pages[i])) eligible_l[nl++] = &pages[i]; }
    if (!strcmp(strategy, "range_interior")) {
        memcpy(selected, eligible_i, ni * sizeof *selected); ns = ni; qsort(selected, ns, sizeof *selected, by_offset);
    } else if (!strcmp(strategy, "offset_topk_interior")) {
        if (null_n || n <= 0 || (size_t)n > ni) fail("invalid offset top-k N");
        qsort(eligible_i, ni, sizeof *eligible_i, by_offset); memcpy(selected, eligible_i, (size_t)n * sizeof *selected); ns = (size_t)n;
    } else if (!strcmp(strategy, "residency_topk")) {
        if (null_ik || null_lk || ik < 0 || lk < 0 || (size_t)ik > ni || (size_t)lk > nl || (ik == 0 && lk == 0)) fail("invalid residency top-k values");
        qsort(eligible_i, ni, sizeof *eligible_i, by_rank); qsort(eligible_l, nl, sizeof *eligible_l, by_rank);
        for (int i = 0; i < ik; ++i) { eligible_i[i]->rank = i + 1; selected[ns++] = eligible_i[i]; }
        for (int i = 0; i < lk; ++i) { eligible_l[i]->rank = i + 1; selected[ns++] = eligible_l[i]; }
        qsort(selected, ns, sizeof *selected, by_offset);
    } else fail("unknown strategy");
    if (!ns) fail("strategy selected no pages");
    for (size_t i = 0; i < ns; ++i) if (!selected[i]->rank) selected[i]->rank = (int)i + 1;

    size_t max_ops = !strcmp(strategy, "range_interior") ? ns + 1 : ns;
    operation_t *ops = calloc(max_ops, sizeof *ops); size_t op_count = 0, extent_count = 0;
    if (!strcmp(strategy, "range_interior")) {
        size_t begin = 0;
        while (begin < ns) {
            ++extent_count;
            size_t end = begin + 1;
            while (end < ns && selected[end]->offset == selected[end - 1]->offset + page_size) ++end;
            uint64_t off = selected[begin]->offset, bytes = (end - begin) * (uint64_t)page_size;
            if (!strcmp(backend, "pread")) {
                for (uint64_t used = 0; used < bytes; used += chunk_size) {
                    uint64_t length = bytes - used < chunk_size ? bytes - used : chunk_size;
                    size_t first = begin + (size_t)(used / page_size), last = first + (size_t)(length / page_size);
                    ops[op_count] = (operation_t){(int)op_count, off + used, length, selected[first]->number, selected[last - 1]->number, 0, 0, false, 0};
                    for (size_t i = first; i < last; ++i) selected[i]->op_index = (int)op_count;
                    ++op_count;
                }
            } else {
                ops[op_count] = (operation_t){(int)op_count, off, bytes, selected[begin]->number, selected[end - 1]->number, 0, 0, false, 0};
                for (size_t i = begin; i < end; ++i) selected[i]->op_index = (int)op_count;
                ++op_count;
            }
            begin = end;
        }
    } else {
        for (size_t i = 0; i < ns; ++i) { selected[i]->op_index = (int)i; ops[i] = (operation_t){(int)i, selected[i]->offset, page_size, selected[i]->number, selected[i]->number, 0, 0, false, 0}; }
        op_count = ns; extent_count = ns;
    }

    cJSON *errors = cJSON_CreateArray();
    int attempted = 0, succeeded = 0, failed_calls = 0, short_reads = 0, operation_failures = 0;
    uint64_t bytes_requested = 0, bytes_completed = 0;
    struct timespec start, finish;
    void *mapping = MAP_FAILED, *buffer = NULL;
    if (!strcmp(backend, "madvise")) {
        long os_page = sysconf(_SC_PAGESIZE);
        if (os_page <= 0) fail("cannot determine OS page size");
        for (size_t i = 0; i < op_count; ++i) if (ops[i].offset % (uint64_t)os_page || ops[i].offset + ops[i].length > (uint64_t)st.st_size) fail("madvise extent is invalid");
        mapping = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_SHARED, fd, 0);
        if (mapping == MAP_FAILED) fail("mmap failed");
        clock_gettime(CLOCK_MONOTONIC, &start);
        for (size_t i = 0; i < op_count; ++i) {
            operation_t *op = &ops[i]; ++attempted; ++op->backend_calls; bytes_requested += op->length;
            if (madvise((char *)mapping + op->offset, (size_t)op->length, MADV_WILLNEED) == 0) { op->success = true; op->bytes_completed = op->length; ++succeeded; bytes_completed += op->length; }
            else { op->error_number = errno; ++failed_calls; ++operation_failures; add_error(errors, op->offset, op->length, errno); }
        }
        clock_gettime(CLOCK_MONOTONIC, &finish);
        munmap(mapping, (size_t)st.st_size);
    } else if (!strcmp(backend, "pread")) {
        buffer = malloc((size_t)chunk_size); if (!buffer) fail("cannot allocate pread buffer");
        clock_gettime(CLOCK_MONOTONIC, &start);
        for (size_t i = 0; i < op_count; ++i) {
            operation_t *op = &ops[i]; uint64_t done = 0; bytes_requested += op->length;
            while (done < op->length) {
                ++attempted; ++op->backend_calls;
                size_t requested = (size_t)(op->length - done);
                ssize_t got = pread(fd, (char *)buffer + done, requested, (off_t)(op->offset + done));
                if (got > 0) { ++succeeded; if ((size_t)got < requested) ++short_reads; done += (uint64_t)got; continue; }
                if (got < 0 && errno == EINTR) { ++failed_calls; add_error(errors, op->offset + done, op->length - done, errno); continue; }
                if (got == 0) { ++succeeded; ++short_reads; op->error_number = 0; }
                else { ++failed_calls; op->error_number = errno; add_error(errors, op->offset + done, op->length - done, errno); }
                break;
            }
            op->bytes_completed = done; bytes_completed += done;
            if (done == op->length) op->success = true; else ++operation_failures;
            for (size_t p = 0; p < ns; ++p) if (selected[p]->op_index == (int)i && selected[p]->offset + page_size <= op->offset + done) selected[p]->success = 1;
        }
        clock_gettime(CLOCK_MONOTONIC, &finish); free(buffer);
    } else fail("unknown backend");
    if (!strcmp(backend, "madvise")) for (size_t p = 0; p < ns; ++p) selected[p]->success = ops[selected[p]->op_index].success;
    close(fd);

    FILE *csv = fopen(selected_path, "w"); if (!csv) fail("cannot write selected pages CSV");
    fprintf(csv, "page_number,page_type,file_offset,length,residency_count,selection_rank,io_operation_index,prefetch_succeeded\n");
    for (size_t i = 0; i < ns; ++i) fprintf(csv, "%" PRIu32 ",%s,%" PRIu64 ",%u,%d,%d,%d,%d\n", selected[i]->number, selected[i]->type, selected[i]->offset, page_size, selected[i]->residency, selected[i]->rank, selected[i]->op_index, selected[i]->success);
    if (fclose(csv)) fail("cannot close selected pages CSV");

    uint64_t duration = elapsed_us(start, finish);
    cJSON *result = cJSON_CreateObject();
    cJSON_AddNumberToObject(result, "schema_version", 1); cJSON_AddStringToObject(result, "cell_id", cell_id);
    cJSON_AddStringToObject(result, "status", operation_failures ? "failed" : "completed"); cJSON_AddStringToObject(result, "backend", backend);
    cJSON_AddStringToObject(result, "strategy", strategy); cJSON_AddStringToObject(result, "variant", variant);
    cJSON_AddItemToObject(result, "memory_condition", cJSON_Duplicate(memory_condition, 1));
    if (null_n) cJSON_AddNullToObject(result, "n"); else cJSON_AddNumberToObject(result, "n", n);
    if (null_ik) cJSON_AddNullToObject(result, "interior_k"); else cJSON_AddNumberToObject(result, "interior_k", ik);
    if (null_lk) cJSON_AddNullToObject(result, "leaf_k"); else cJSON_AddNumberToObject(result, "leaf_k", lk);
    cJSON_AddNumberToObject(result, "eligible_interior_count", (double)ni); cJSON_AddNumberToObject(result, "eligible_leaf_count", (double)nl);
    int selected_i = 0, selected_l = 0; for (size_t i = 0; i < ns; ++i) { selected_i += interior(selected[i]); selected_l += leaf(selected[i]); }
    cJSON_AddNumberToObject(result, "selected_interior_count", selected_i); cJSON_AddNumberToObject(result, "selected_leaf_count", selected_l);
    cJSON_AddNumberToObject(result, "selected_unique_page_count", (double)ns); cJSON_AddNumberToObject(result, "extent_count", (double)extent_count);
    cJSON_AddNumberToObject(result, "syscall_attempted_count", attempted); cJSON_AddNumberToObject(result, "syscall_succeeded_count", succeeded); cJSON_AddNumberToObject(result, "syscall_failed_count", failed_calls);
    cJSON_AddNumberToObject(result, "bytes_requested", (double)bytes_requested); cJSON_AddNumberToObject(result, "bytes_completed", (double)bytes_completed); cJSON_AddNumberToObject(result, "short_read_count", short_reads);
    cJSON_AddNumberToObject(result, "prefetch_elapsed_us", (double)duration);
    cJSON_AddNumberToObject(result, !strcmp(backend, "madvise") ? "madvise_dispatch_us" : "pread_elapsed_us", (double)duration);
    cJSON_AddNumberToObject(result, "pread_chunk_bytes", (double)chunk_size); cJSON_AddStringToObject(result, "selected_pages_csv", selected_path);
    cJSON_AddItemToObject(result, "errors", errors); cJSON *op_array = cJSON_AddArrayToObject(result, "io_operations");
    for (size_t i = 0; i < op_count; ++i) { cJSON *o = cJSON_CreateObject(); cJSON_AddNumberToObject(o, "operation_index", ops[i].index); cJSON_AddNumberToObject(o, "offset", (double)ops[i].offset); cJSON_AddNumberToObject(o, "length", (double)ops[i].length); cJSON_AddNumberToObject(o, "first_page", ops[i].first_page); cJSON_AddNumberToObject(o, "last_page", ops[i].last_page); cJSON_AddNumberToObject(o, "backend_calls", ops[i].backend_calls); cJSON_AddNumberToObject(o, "bytes_completed", (double)ops[i].bytes_completed); cJSON_AddBoolToObject(o, "success", ops[i].success); if (ops[i].error_number) cJSON_AddNumberToObject(o, "errno", ops[i].error_number); else cJSON_AddNullToObject(o, "errno"); cJSON_AddItemToArray(op_array, o); }
    write_json(result_path, result);
    cJSON_Delete(result); cJSON_Delete(job); free(job_text); free(pages); free(eligible_i); free(eligible_l); free(selected); free(ops);
    return operation_failures ? 1 : 0;
}
