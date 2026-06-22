#define _POSIX_C_SOURCE 200809L

/*
 * layout_rewriter.c — Type-aware SQLite page layout rewriter
 *
 * Moves all interior B-tree pages to the front of the file (low page
 * numbers), followed by leaf pages, then everything else.  Updates every
 * internal page-number reference: child pointers in interior pages, overflow
 * next-page pointers, freelist trunk chains, and the DB-header freelist
 * pointer.
 *
 * Because sqlite_master stores each table/index root-page number as a plain
 * integer inside a record, those values cannot be patched at the binary level
 * without re-encoding SQLite's record format.  Instead the tool prints SQL
 * on stdout; pipe it into sqlite3 to finish the fixup:
 *
 *   ./layout_rewriter input.db output.db > fix.sql
 *   sqlite3 output.db < fix.sql
 *   sqlite3 output.db "PRAGMA integrity_check;"
 *
 * Build:
 *   gcc -O2 -Wall -o layout_rewriter layout_rewriter.c
 *
 * Assumptions:
 *   - Standard 4 KB page size (works for any size; page_size is read from
 *     the header).
 *   - No WAL mode (journal_mode = DELETE or OFF).
 *   - For cleanest results, VACUUM the input DB first so the freelist is
 *     empty.  The tool handles non-empty freelists correctly regardless.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

/* ── Big-endian helpers ───────────────────────────────────────────────── */

static uint16_t rd_be16(const uint8_t *p) {
    return ((uint16_t)p[0] << 8) | p[1];
}
static uint32_t rd_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] <<  8) |  (uint32_t)p[3];
}
static void wr_be32(uint8_t *p, uint32_t v) {
    p[0] = (v >> 24) & 0xFF;
    p[1] = (v >> 16) & 0xFF;
    p[2] = (v >>  8) & 0xFF;
    p[3] =  v        & 0xFF;
}

/* ── Page types (mirrors classify_pages.c) ────────────────────────────── */

typedef enum {
    PT_UNKNOWN = 0,
    PT_INTERIOR_INDEX,
    PT_INTERIOR_TABLE,
    PT_LEAF_INDEX,
    PT_LEAF_TABLE,
    PT_FREELIST_TRUNK,
    PT_FREELIST_LEAF,
    PT_OVERFLOW,
    PT_LOCK_PAGE,
} page_type_t;

static int is_interior(page_type_t t) {
    return t == PT_INTERIOR_TABLE || t == PT_INTERIOR_INDEX;
}
static int is_leaf(page_type_t t) {
    return t == PT_LEAF_TABLE || t == PT_LEAF_INDEX;
}

/* ── Page classification (ported from classify_pages.c) ──────────────── */

static page_type_t *classify_all(int fd, uint32_t page_size,
                                  uint32_t page_count, uint32_t first_freelist) {
    page_type_t *types = calloc(page_count + 1, sizeof(*types));
    uint8_t     *buf   = malloc(page_size);
    if (!types || !buf) { perror("alloc classify"); exit(1); }

    /* Walk freelist trunk chain, mark trunk + leaf pages. */
    uint32_t trunk  = first_freelist;
    uint32_t safety = page_count + 1;
    while (trunk != 0 && trunk <= page_count && safety-- > 0) {
        off_t off = (off_t)(trunk - 1) * page_size;
        if (pread(fd, buf, page_size, off) != (ssize_t)page_size) break;
        types[trunk] = PT_FREELIST_TRUNK;

        uint32_t next   = rd_be32(buf);
        uint32_t n_leaf = rd_be32(buf + 4);
        uint32_t cap    = (page_size / 4) - 2;
        if (n_leaf > cap) n_leaf = cap;
        for (uint32_t i = 0; i < n_leaf; i++) {
            uint32_t lp = rd_be32(buf + 8 + i * 4);
            if (lp >= 1 && lp <= page_count)
                types[lp] = PT_FREELIST_LEAF;
        }
        trunk = next;
    }

    /* Lock-byte page at file offset 0x40000000. */
    uint32_t lock_pg = (uint32_t)(1073741824ULL / page_size) + 1;
    if (lock_pg >= 1 && lock_pg <= page_count)
        types[lock_pg] = PT_LOCK_PAGE;

    /* Classify remaining pages by the B-tree flag byte. */
    for (uint32_t pn = 1; pn <= page_count; pn++) {
        if (types[pn] != PT_UNKNOWN) continue;
        /* Page 1's B-tree header starts at offset 100 (after the DB header). */
        off_t flag_off = (pn == 1) ? 100 : (off_t)(pn - 1) * page_size;
        uint8_t flag;
        if (pread(fd, &flag, 1, flag_off) != 1) continue;
        switch (flag) {
            case 0x02: types[pn] = PT_INTERIOR_INDEX; break;
            case 0x05: types[pn] = PT_INTERIOR_TABLE; break;
            case 0x0A: types[pn] = PT_LEAF_INDEX;     break;
            case 0x0D: types[pn] = PT_LEAF_TABLE;     break;
            default:   types[pn] = PT_OVERFLOW;       break;
        }
    }

    free(buf);
    return types;
}

/* ── Pointer-remapping helpers ────────────────────────────────────────── */

/*
 * Rewrite every child pointer in an interior B-tree page using mapping[].
 * hdr_off is the byte offset of the B-tree page header within the page
 * buffer: 100 for page 1 (preceded by the 100-byte DB header), 0 otherwise.
 */
static void remap_interior(uint8_t *buf, uint32_t page_size,
                            uint32_t hdr_off, const uint32_t *mapping,
                            uint32_t page_count) {
    /* Rightmost child pointer sits at B-tree header byte 8. */
    uint32_t rmost = rd_be32(buf + hdr_off + 8);
    if (rmost >= 1 && rmost <= page_count && mapping[rmost])
        wr_be32(buf + hdr_off + 8, mapping[rmost]);

    /*
     * Cell pointer array starts immediately after the 12-byte interior
     * page header.  Each entry is a 2-byte offset from the page start
     * to the cell content.  The first 4 bytes of every interior cell
     * (table or index) are the left child page number.
     */
    uint16_t n_cells = rd_be16(buf + hdr_off + 3);
    for (uint16_t i = 0; i < n_cells; i++) {
        uint32_t ptr_off = hdr_off + 12 + (uint32_t)i * 2;
        if (ptr_off + 2 > page_size) break;
        uint16_t cell_off = rd_be16(buf + ptr_off);
        if (cell_off < 4 || (uint32_t)cell_off + 4 > page_size) continue;
        uint32_t lchild = rd_be32(buf + cell_off);
        if (lchild >= 1 && lchild <= page_count && mapping[lchild])
            wr_be32(buf + cell_off, mapping[lchild]);
    }
}

/* First 4 bytes of an overflow page are the next-page pointer. */
static void remap_overflow(uint8_t *buf, const uint32_t *mapping,
                            uint32_t page_count) {
    uint32_t next = rd_be32(buf);
    if (next >= 1 && next <= page_count && mapping[next])
        wr_be32(buf, mapping[next]);
}

/* Freelist trunk: next-trunk pointer + array of leaf page numbers. */
static void remap_freelist_trunk(uint8_t *buf, uint32_t page_size,
                                  const uint32_t *mapping, uint32_t page_count) {
    uint32_t next = rd_be32(buf);
    if (next >= 1 && next <= page_count && mapping[next])
        wr_be32(buf, mapping[next]);

    uint32_t n_leaf = rd_be32(buf + 4);
    uint32_t cap    = (page_size / 4) - 2;
    if (n_leaf > cap) n_leaf = cap;
    for (uint32_t i = 0; i < n_leaf; i++) {
        uint32_t lp = rd_be32(buf + 8 + i * 4);
        if (lp >= 1 && lp <= page_count && mapping[lp])
            wr_be32(buf + 8 + i * 4, mapping[lp]);
    }
}

/* ── main ─────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    if (argc != 3) {
        fprintf(stderr,
            "Usage: %s input.db output.db\n"
            "\n"
            "Writes a layout-optimised copy of input.db to output.db,\n"
            "then prints SQL on stdout to fix sqlite_master rootpages.\n"
            "\n"
            "Full workflow:\n"
            "  ./layout_rewriter input.db output.db > fix.sql\n"
            "  sqlite3 output.db < fix.sql\n"
            "  sqlite3 output.db \"PRAGMA integrity_check;\"\n",
            argv[0]);
        return 1;
    }

    /* ── Read DB header ───────────────────────────────────────────────── */
    int fd_in = open(argv[1], O_RDONLY);
    if (fd_in < 0) { perror("open input"); return 1; }

    uint8_t hdr[100];
    if (pread(fd_in, hdr, 100, 0) != 100) {
        fprintf(stderr, "error: cannot read 100-byte DB header\n"); return 1;
    }
    if (memcmp(hdr, "SQLite format 3\0", 16) != 0) {
        fprintf(stderr, "error: not a SQLite3 database file\n"); return 1;
    }

    uint16_t ps_raw     = rd_be16(hdr + 16);
    uint32_t page_size  = (ps_raw == 1) ? 65536u : (uint32_t)ps_raw;
    uint32_t page_count = rd_be32(hdr + 28);
    uint32_t first_free = rd_be32(hdr + 32);

    fprintf(stderr, "Input:  %s\n", argv[1]);
    fprintf(stderr, "  page_size=%u  page_count=%u  first_freelist=%u\n",
            page_size, page_count, first_free);

    /* ── Classify pages ───────────────────────────────────────────────── */
    page_type_t *types = classify_all(fd_in, page_size, page_count, first_free);

    /* Tally per-type counts (for diagnostic output). */
    uint32_t n_int_nonp1 = 0, n_leaf = 0, n_rest = 0;
    for (uint32_t pn = 2; pn <= page_count; pn++) {
        if      (is_interior(types[pn])) n_int_nonp1++;
        else if (is_leaf(types[pn]))     n_leaf++;
        else                             n_rest++;
    }
    fprintf(stderr, "  interior (excl. page 1): %u  leaf: %u  other: %u\n",
            n_int_nonp1, n_leaf, n_rest);

    /* ── Build old→new and new→old mappings ───────────────────────────── */
    /*
     * Slot assignment (1-indexed):
     *   page 1              → 1        (DB header lives here; must not move)
     *   interior pages 2..  → 2 .. N_i+1
     *   leaf pages          → N_i+2 .. N_i+1+N_leaf
     *   overflow / freelist → remaining slots
     *
     * Pages within each group keep their original relative order so that
     * tree traversal order is preserved.
     */
    uint32_t *old_to_new = calloc(page_count + 1, sizeof(uint32_t));
    uint32_t *new_to_old = calloc(page_count + 1, sizeof(uint32_t));
    if (!old_to_new || !new_to_old) { perror("alloc mapping"); return 1; }

    old_to_new[1] = 1;
    new_to_old[1] = 1;

    uint32_t slot_int  = 2;
    uint32_t slot_leaf = 2 + n_int_nonp1;
    uint32_t slot_rest = slot_leaf + n_leaf;

    for (uint32_t pn = 2; pn <= page_count; pn++) {
        uint32_t new_pn;
        if      (is_interior(types[pn])) new_pn = slot_int++;
        else if (is_leaf(types[pn]))     new_pn = slot_leaf++;
        else                             new_pn = slot_rest++;
        old_to_new[pn] = new_pn;
        new_to_old[new_pn] = pn;
    }

    /* ── Write reordered output file ──────────────────────────────────── */
    int fd_out = open(argv[2], O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd_out < 0) { perror("open output"); return 1; }

    uint8_t *page_buf = malloc(page_size);
    if (!page_buf) { perror("alloc page_buf"); return 1; }

    uint32_t n_moved = 0;
    for (uint32_t new_pn = 1; new_pn <= page_count; new_pn++) {
        uint32_t old_pn = new_to_old[new_pn];
        off_t    rd_off = (off_t)(old_pn - 1) * page_size;
        off_t    wr_off = (off_t)(new_pn - 1) * page_size;

        if (pread(fd_in, page_buf, page_size, rd_off) != (ssize_t)page_size) {
            fprintf(stderr, "error: read old page %u\n", old_pn);
            return 1;
        }

        /* B-tree header starts at offset 100 within page 1. */
        uint32_t hdr_off = (old_pn == 1) ? 100u : 0u;

        /* Patch all page-number references inside this page. */
        switch (types[old_pn]) {
            case PT_INTERIOR_TABLE:
            case PT_INTERIOR_INDEX:
                remap_interior(page_buf, page_size, hdr_off,
                               old_to_new, page_count);
                break;
            case PT_OVERFLOW:
                remap_overflow(page_buf, old_to_new, page_count);
                break;
            case PT_FREELIST_TRUNK:
                remap_freelist_trunk(page_buf, page_size,
                                     old_to_new, page_count);
                break;
            default:
                break;
        }

        /* Page 1 also carries the 100-byte DB header with its own pointers. */
        if (new_pn == 1) {
            if (first_free != 0 && old_to_new[first_free])
                wr_be32(page_buf + 32, old_to_new[first_free]);
            /* Bump file-change counter so SQLite discards any cached data. */
            wr_be32(page_buf + 24, rd_be32(page_buf + 24) + 1);
        }

        if (pwrite(fd_out, page_buf, page_size, wr_off) != (ssize_t)page_size) {
            fprintf(stderr, "error: write new page %u\n", new_pn);
            return 1;
        }

        if (old_pn != new_pn) n_moved++;
    }

    close(fd_in);
    close(fd_out);
    fprintf(stderr, "Output: %s  (%u pages written, %u relocated)\n",
            argv[2], page_count, n_moved);

    /* ── Emit SQL to fix sqlite_master rootpage values ────────────────── */
    /*
     * sqlite_master holds each table/index root-page number as a plain
     * integer in its 'rootpage' column.  After the binary rewrite those
     * values are stale.  PRAGMA writable_schema lets us UPDATE them directly
     * without going through the normal schema-change path.
     *
     * We emit one UPDATE per page that moved; rows whose rootpage does not
     * match any old_pgno are unaffected (so it is safe to emit updates for
     * non-root pages too — they simply match zero rows).
     */
    printf("-- Generated by layout_rewriter for: %s\n", argv[2]);
    printf("-- Apply with: sqlite3 %s < this_file.sql\n\n", argv[2]);
    printf("PRAGMA writable_schema = ON;\n");
    for (uint32_t old_pn = 1; old_pn <= page_count; old_pn++) {
        uint32_t new_pn = old_to_new[old_pn] ? old_to_new[old_pn] : old_pn;
        if (new_pn != old_pn)
            printf("UPDATE sqlite_master SET rootpage = %u "
                   "WHERE rootpage = %u;\n", new_pn, old_pn);
    }
    printf("PRAGMA writable_schema = OFF;\n");
    printf("PRAGMA integrity_check;\n");

    free(page_buf);
    free(types);
    free(old_to_new);
    free(new_to_old);
    return 0;
}
