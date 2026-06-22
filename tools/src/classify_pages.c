#define _POSIX_C_SOURCE 200809L

/*
 * classify_pages.c — SQLite database file page classifier
 *
 * Reads a .db file directly (no libsqlite linkage), parses the header,
 * walks the freelist, reads the b-tree flag byte of each remaining page,
 * and emits one CSV row per page: page_number,page_type,file_offset
 *
 * Build:  gcc -O2 -Wall -o classify_pages classify_pages.c
 * Run:    ./classify_pages test.db > classify_pages.csv
 *
 * References:
 *   https://www.sqlite.org/fileformat.html  (authoritative)
 *     §1.2  — database header layout (100 bytes)
 *     §1.3  — freelist structure
 *     §1.5  — b-tree page header flag byte
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>

/* Big-endian readers ??SQLite stores all multi-byte ints big-endian. */
static uint16_t rd_be16(const uint8_t *p) {
    return ((uint16_t)p[0] << 8) | p[1];
}
static uint32_t rd_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] <<  8) |  (uint32_t)p[3];
}

/* Every page we find falls into exactly one of these buckets. */
typedef enum {
    PT_UNKNOWN = 0,
    PT_INTERIOR_INDEX,   /* b-tree flag 0x02 */
    PT_INTERIOR_TABLE,   /* b-tree flag 0x05 */
    PT_LEAF_INDEX,       /* b-tree flag 0x0A */
    PT_LEAF_TABLE,       /* b-tree flag 0x0D */
    PT_FREELIST_TRUNK,
    PT_FREELIST_LEAF,
    PT_OVERFLOW,         /* anything that's none of the above */
    PT_LOCK_PAGE,        /* the reserved lock byte page */
    PT_COUNT
} page_type_t;

static const char *pt_name(page_type_t t) {
    switch (t) {
        case PT_INTERIOR_INDEX: return "interior_index";
        case PT_INTERIOR_TABLE: return "interior_table";
        case PT_LEAF_INDEX:     return "leaf_index";
        case PT_LEAF_TABLE:     return "leaf_table";
        case PT_FREELIST_TRUNK: return "freelist_trunk";
        case PT_FREELIST_LEAF:  return "freelist_leaf";
        case PT_OVERFLOW:       return "overflow";
        case PT_LOCK_PAGE:      return "lock_page";
        default:                return "unknown";
    }
}

int main(int argc, char *argv[]) {
    if (argc != 2) {
        fprintf(stderr, "Usage: %s <database.db>\n", argv[0]);
        return 1;
    }

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    /* --- 1. Read and validate the 100-byte database header --- */
    uint8_t hdr[100];
    if (read(fd, hdr, 100) != 100) {
        fprintf(stderr, "error: cannot read 100-byte header\n");
        return 1;
    }
    if (memcmp(hdr, "SQLite format 3\0", 16) != 0) {
        fprintf(stderr, "error: not a SQLite database (bad magic)\n");
        return 1;
    }

    /* page_size at offset 16 (big-endian 2 bytes). Value 1 means 65536. */
    uint16_t ps_raw    = rd_be16(hdr + 16);
    uint32_t page_size = (ps_raw == 1) ? 65536u : (uint32_t)ps_raw;

    /* page_count at offset 28 (big-endian 4 bytes). */
    uint32_t page_count      = rd_be32(hdr + 28);
    uint32_t first_freelist  = rd_be32(hdr + 32);  /* first trunk page */
    uint32_t freelist_total  = rd_be32(hdr + 36);  /* total freelist pages */

    fprintf(stderr, "page_size=%u  page_count=%u  "
                    "first_freelist_trunk=%u  freelist_total=%u\n",
            page_size, page_count, first_freelist, freelist_total);

    /* Allocate the type array 1-indexed: types[1..page_count] */
    page_type_t *types = calloc((size_t)page_count + 1, sizeof(*types));
    uint8_t     *buf   = malloc(page_size);
    if (!types || !buf) { perror("alloc"); return 1; }

    /* --- 2. Walk the freelist trunk chain, mark trunk + leaf pages --- */
    uint32_t trunk = first_freelist;
    uint32_t safety = page_count + 1;   /* break infinite loops on corrupt DB */
    while (trunk != 0 && trunk <= page_count && safety-- > 0) {
        off_t off = (off_t)(trunk - 1) * page_size;
        if (pread(fd, buf, page_size, off) != (ssize_t)page_size) {
            fprintf(stderr, "warn: cannot read trunk page %u\n", trunk);
            break;
        }
        types[trunk] = PT_FREELIST_TRUNK;

        uint32_t next   = rd_be32(buf);
        uint32_t n_leaf = rd_be32(buf + 4);
        /* Sanity: n_leaf can't exceed (page_size/4) - 2 */
        uint32_t cap = (page_size / 4) - 2;
        if (n_leaf > cap) n_leaf = cap;

        for (uint32_t i = 0; i < n_leaf; i++) {
            uint32_t leaf = rd_be32(buf + 8 + i * 4);
            if (leaf >= 1 && leaf <= page_count) {
                types[leaf] = PT_FREELIST_LEAF;
            }
        }
        trunk = next;
    }

    /* --- 3. Mark the lock-byte page (if it exists in this file) --- */
    /* Reserved lock byte lives at file offset 0x40000000 = 1073741824. */
    uint64_t lock_byte = 1073741824ULL;
    uint32_t lock_pg = (uint32_t)(lock_byte / page_size) + 1;
    if (lock_pg >= 1 && lock_pg <= page_count) {
        types[lock_pg] = PT_LOCK_PAGE;
    }

    /* --- 4. Classify remaining pages by b-tree flag byte --- */
    for (uint32_t pn = 1; pn <= page_count; pn++) {
        if (types[pn] != PT_UNKNOWN) continue;  /* already classified */

        /* Page 1's b-tree header starts at file offset 100 (after db hdr).
         * All other pages' b-tree header starts at page boundary. */
        off_t flag_off = (pn == 1) ? 100 : (off_t)(pn - 1) * page_size;

        uint8_t flag;
        if (pread(fd, &flag, 1, flag_off) != 1) {
            fprintf(stderr, "warn: cannot read flag of page %u\n", pn);
            continue;
        }
        switch (flag) {
            case 0x02: types[pn] = PT_INTERIOR_INDEX; break;
            case 0x05: types[pn] = PT_INTERIOR_TABLE; break;
            case 0x0A: types[pn] = PT_LEAF_INDEX;     break;
            case 0x0D: types[pn] = PT_LEAF_TABLE;     break;
            default:   types[pn] = PT_OVERFLOW;       break;
        }
    }

    /* --- 5. Emit CSV on stdout, stats on stderr --- */
    printf("page_number,page_type,file_offset\n");
    uint32_t counts[PT_COUNT] = {0};
    for (uint32_t pn = 1; pn <= page_count; pn++) {
        off_t off = (off_t)(pn - 1) * page_size;
        printf("%u,%s,%lld\n", pn, pt_name(types[pn]), (long long)off);
        counts[types[pn]]++;
    }

    uint32_t tot_int  = counts[PT_INTERIOR_TABLE] + counts[PT_INTERIOR_INDEX];
    uint32_t tot_leaf = counts[PT_LEAF_TABLE]     + counts[PT_LEAF_INDEX];
    uint32_t tot_free = counts[PT_FREELIST_TRUNK] + counts[PT_FREELIST_LEAF];

    fprintf(stderr, "\n=== Classification summary ===\n");
    fprintf(stderr, "Total pages:    %u\n", page_count);
    fprintf(stderr, "Interior:       %u (%.2f%%)\n",
            tot_int,  100.0 * tot_int  / page_count);
    fprintf(stderr, "  interior_table: %u\n", counts[PT_INTERIOR_TABLE]);
    fprintf(stderr, "  interior_index: %u\n", counts[PT_INTERIOR_INDEX]);
    fprintf(stderr, "Leaf:           %u (%.2f%%)\n",
            tot_leaf, 100.0 * tot_leaf / page_count);
    fprintf(stderr, "  leaf_table:     %u\n", counts[PT_LEAF_TABLE]);
    fprintf(stderr, "  leaf_index:     %u\n", counts[PT_LEAF_INDEX]);
    fprintf(stderr, "Freelist:       %u (%.2f%%)\n",
            tot_free, 100.0 * tot_free / page_count);
    fprintf(stderr, "Overflow/other: %u (%.2f%%)\n",
            counts[PT_OVERFLOW], 100.0 * counts[PT_OVERFLOW] / page_count);
    if (counts[PT_LOCK_PAGE])
        fprintf(stderr, "Lock page:      %u (page #%u)\n",
                counts[PT_LOCK_PAGE], lock_pg);

    free(buf);
    free(types);
    close(fd);
    return 0;
}

