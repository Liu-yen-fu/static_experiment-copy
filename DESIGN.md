# Static SQLite Layout 與 Prefetch 實驗規格

## 1. 範圍

本系統用於執行靜態 SQLite database layout 與 prefetch 實驗。

第一版規格如下：

- 使用既有 `benchmark_harness` 執行 cold-start query measurement。
- 不執行寫入負載。
- 不使用 checkpoint。
- 不修改 `benchmark_harness` 的 cold boundary 與 query measurement 邏輯。
- 正式執行環境為 Linux。
- 暫不納入原策略 2f。
- 所有 experiment 參數由 JSON config 提供。
- Config 缺少必要欄位或內容不合法時，不得開始 experiment。

## 2. 專案目錄

專案根目錄為 `static_experiment/`：

```text
static_experiment/
├─ DESIGN.md
├─ Makefile
├─ tools/
│  ├─ src/
│  │  ├─ prefetch_runner.c
│  │  ├─ orchestrator.py
│  │  ├─ build_layouts.py
│  │  ├─ aggregate_training.py
│  │  ├─ summarize_results.py
│  │  ├─ plot_tradeoff.py
│  │  └─ generate_report.py
│  ├─ vendor/
│  │  └─ cjson/
│  │     ├─ cJSON.c
│  │     └─ cJSON.h
│  ├─ bin/
│  └─ manifest.json
├─ configs/
│  └─ smoke.json
├─ data/
│  ├─ layouts/
│  │  ├─ original/
│  │  ├─ vacuum/
│  │  └─ rewrite/
│  ├─ workloads/
│  └─ training_profiles/
└─ experiments/
   └─ <experiment-id>/
      ├─ config.json
      ├─ manifest.json
      ├─ state.json
      ├─ cells/
      ├─ logs/
      ├─ plots/
      ├─ report.md
      └─ results/
         ├─ all_raw.csv
         └─ <workload-type>/
            ├─ layout_comparisons/
            │  └─ <memory-condition>.csv
            ├─ original/
            │  ├─ memory_comparison.csv
            │  └─ memory_conditions/
            │     └─ <memory-condition>/
            │        ├─ baseline/
            │        │  ├─ raw.csv
            │        │  └─ summary.csv
            │        ├─ backend_comparison.csv
            │        └─ backends/
            │           ├─ madvise/
            │           │  ├─ strategy_comparison.csv
            │           │  └─ <strategy-key>/
            │           │     ├─ raw.csv
            │           │     └─ summary.csv
            │           └─ pread/
            │              ├─ strategy_comparison.csv
            │              └─ <strategy-key>/
            │                 ├─ raw.csv
            │                 └─ summary.csv
            ├─ vacuum/
            └─ rewrite/
```

`data/workloads/` 必須包含完整的 workload 集合、`README.md` 與 `SUMMARY.csv`。Experiment 不得複製個別 workload；實際選用的 workload 清單必須記錄於 experiment manifest。

## 3. 工具職責

### 3.1 `prefetch_runner.c`

負責：

- 讀取 prefetch job JSON。
- 讀取 SQLite page size。
- 載入 page classification 與 training profile。
- 根據策略選取 pages。
- 產生 per-page 或 contiguous-range I/O extents。
- 使用 `madvise` 或 `pread` 執行 prefetch。
- 記錄 prefetch syscall、byte count 與 elapsed time。
- 輸出 selected-pages CSV 與 prefetch-result JSON。

不得負責：

- Drop cache。
- Training workload。
- Query workload。
- Residency check。
- 統計彙整。

### 3.2 `orchestrator.py`

負責：

- 讀取 experiment config。
- 執行完整 preflight validation。
- 固定 training 與 measurement workload 清單。
- 產生 experiment manifest。
- 執行 training workload 與 residency snapshot。
- 呼叫 training profile 聚合工具。
- 展開 layout、workload、memory condition、strategy、backend 與 repetition cells。
- 產生 prefetch job JSON 與 post-cold script。
- 呼叫 `benchmark_harness`。
- 管理 timeout、失敗、resume 與 cell completion。
- 解析 harness run record。
- 合併 prefetch、query 與 residency 結果。
- 產生 raw results、summary、trade-off 圖與Markdown報告。

### 3.3 `build_layouts.py`

為一次性 provisioning 工具，負責建立：

- Original layout。
- VACUUM layout。
- Type-aware rewrite layout。
- 各 layout 的 `classify.csv` 與 `metadata.json`。

此工具不屬於 experiment，不由 orchestrator 自動呼叫，不使用 provisioning JSON。

正式介面：

```bash
python3 tools/src/build_layouts.py \
  --source <source.db> \
  --output-dir data/layouts
```

### 3.4 `aggregate_training.py`

負責將同一 layout 與 workload type 的多份逐頁 residency CSV 聚合成：

- `residency_counts.csv`
- `profile.json`

### 3.5 `summarize_results.py`

負責：

- 從 cell JSON 與 raw results 產生 measurement-level summary。
- 產生 strategy-level summary。
- 產生跨全部workload types、layouts、memory conditions、strategies與backends的`results/all_raw.csv`。
- 計算 query latency improvement。
- 計算指定統計量。

### 3.6 `plot_tradeoff.py`

負責根據 summary 產生：

- 每個configured backend各一份`plots/tradeoff_<backend>.png`
- `plots/tradeoff_points.csv`

### 3.7 `generate_report.py`

負責讀取experiment config、manifest、分類式results、cell狀態與trade-off artifacts，產生人類可直接閱讀的：

```text
experiments/<experiment-id>/report.md
```

報告內容必須符合第25節的Markdown報告契約。

## 4. Build

工具 source 必須集中於 `static_experiment/tools/src/`，並以該目錄版本為 canonical source。

`cJSON` 必須以現成 library source vendored 於 `tools/vendor/cjson/`，不得自行實作 JSON parser，亦不得要求執行環境預先安裝 system cJSON package。

正式 build 指令：

```bash
cd static_experiment
make
```

Makefile 必須建立：

```text
tools/bin/benchmark_harness
tools/bin/classify_pages
tools/bin/residency_checker
tools/bin/layout_rewriter
tools/bin/prefetch_runner
```

## 5. Layout Provisioning

三種 layout 必須由同一份 canonical source DB 獨立建立：

```text
canonical source
├─ original/database.db
├─ vacuum/database.db
└─ rewrite/database.db
```

### 5.1 Original

將 canonical source DB 複製為：

```text
data/layouts/original/database.db
```

### 5.2 VACUUM

從 original DB 的獨立副本執行 `VACUUM`，輸出：

```text
data/layouts/vacuum/database.db
```

### 5.3 Rewrite

直接以 original DB 作為 `layout_rewriter` 輸入，不先執行 VACUUM。輸出：

```text
data/layouts/rewrite/database.db
data/layouts/rewrite/fix.sql
```

`fix.sql` 必須套用至 rewritten DB。

### 5.4 Layout Metadata

每個 layout 目錄必須包含：

```text
database.db
classify.csv
metadata.json
```

Rewrite 目錄另含 `fix.sql`。

`metadata.json` 至少記錄：

- Layout 名稱。
- Source DB SHA-256。
- Output DB SHA-256。
- File size。
- SQLite page size。
- SQLite page count。
- Freelist count。
- Classification SHA-256。
- Transformation 類型。
- Transformation tool SHA-256 或 SQLite version。
- Fix SQL SHA-256（rewrite only）。

Layout provisioning 只執行一次。Experiment 只使用已存在的 layout artifacts。

## 6. Workloads

Workload 必須來自 `data/workloads/`。

支援下列 12 種 workload type：

```text
read_uniform_full
read_uniform_window
read_uniform_tail
read_zipf_full
read_zipf_window
read_zipf_tail
scan_uniform_full
scan_uniform_window
scan_uniform_tail
scan_zipf_full
scan_zipf_window
scan_zipf_tail
```

預設 smoke workload type 為：

```text
read_zipf_full
```

每種 type 的 index 分區如下：

```text
001–025: training pool
026–050: measurement pool
```

Training 與 measurement 不得跨 workload type 配對。

## 7. Workload 抽樣

Experiment config 必須提供一個 sampling seed。

Orchestrator 必須：

1. 使用該 seed 建立單一 pseudo-random generator。
2. 按 config 中 workload type 的順序處理。
3. 對每個 type 先抽 training files，再抽 measurement files。
4. 以不重複抽樣方式選取 files。
5. 保留抽樣順序。
6. 將選取結果、順序與 file SHA-256 寫入 manifest。

Resume 時不得重新抽樣。

預設 smoke 數量：

```text
training files: 5
measurement files: 5
measurement repetitions: 1
```

所有 layout 與 strategy 必須使用相同的 measurement file 清單與順序。

## 8. Training

Layout-only 與 structure-based strategy 不需要 training profile。Residency-ranked strategy 必須使用 training profile。

每個layout、workload type、memory condition與training file的流程：

```text
drop cache
→ benchmark_harness 執行完整 training workload
→ benchmark_harness 結束
→ residency_checker 立即產生逐頁 residency CSV
```

每個 training workload 執行前必須重新清空 cache。

### 8.1 Training Residency Snapshot

Training profile 使用外部 `residency_checker` 產生的逐頁 CSV。若啟用 memory limit，`residency_checker` 在 memory-limited scope 結束後立即執行。

Snapshot 必須保存於：

```text
data/training_profiles/<layout>/<workload-type>/<memory-condition>/<profile-id>/snapshots/
```

`profile-id` 必須為下列內容依固定順序串接後計算的 SHA-256：

- Database SHA-256。
- Classification SHA-256。
- Memory condition名稱、enabled狀態與正規化後的MemoryMax bytes。
- 依抽樣順序排列的 training workload SHA-256 清單。
- Aggregation schema version。
- `aggregate_training.py` SHA-256。

不同 `profile-id` 必須使用不同目錄，不得互相覆寫。相同 `profile-id` 的完整 profile artifacts 允許直接重用。Experiment manifest 必須記錄實際使用的 `profile-id`。

### 8.2 Aggregation

每頁的排名依據為：

```text
residency_count(page)
= 該page在多少個training workload結束後為resident
```

此數值定義為 access-count approximation。

排名順序固定為：

```text
residency_count DESC
page_number ASC
```

Eligible page types：

```text
interior = interior_table + interior_index
leaf = leaf_table + leaf_index
```

所有 table 與 index 均納入。

### 8.3 Training Profile CSV

`residency_counts.csv` 欄位固定為：

```csv
page_number,page_type,file_offset,residency_count,training_run_count,residency_rate
```

### 8.4 Training Profile JSON

`profile.json` 至少記錄：

- Layout 名稱。
- Database SHA-256。
- Workload type。
- Memory condition名稱、enabled狀態與正規化後的MemoryMax bytes。
- Training workload file names 與 SHA-256。
- Training run count。
- Aggregation metric。
- Tie-break 規則。
- Classification SHA-256。
- Profile CSV path 與 SHA-256。
- Eligible interior count。
- Eligible leaf count。

## 9. Prefetch 策略

第一版支援以下策略。

### 9.1 Baseline

不執行 prefetch，不產生 post-cold script，不產生 prefetch-result JSON。

### 9.2 `range_interior`

- 選取所有 eligible interior pages。
- 依 file offset 排序。
- 將相鄰 pages 合併為 contiguous extents。
- 每個 extent 執行一次或多次 backend I/O；`pread` 受 chunk 上限限制。

### 9.3 `offset_topk_interior`

- Eligible interior pages 依 file offset 排序。
- 選取前 N 個 pages。
- 每頁建立一個 extent。
- N 必須為正整數。
- N 不得超過 eligible interior count。
- N 等於 eligible interior count 時等同原 per-page 策略。
- 不得以 N=0 建立第二個 baseline。

N sweep 支援兩種格式：

```json
{"values": [1, 5, 10]}
```

```json
{
  "range": {
    "start": 1,
    "end_exclusive": 93,
    "step": 1
  }
}
```

`values` 與 `range` 不得同時存在。每個 N 為獨立 cell，每次執行前必須重新清空 cache。

Smoke 使用：

```text
N=5
```

### 9.4 `residency_topk`

- Interior 與 leaf 分別依 training profile 排名。
- 選取前 `interior_k` 個 interior pages。
- 選取前 `leaf_k` 個 leaf pages。
- 每頁建立一個 extent。
- `interior_k` 與 `leaf_k` 為相互獨立的非負整數。
- `interior_k` 不得超過 eligible interior count。
- `leaf_k` 不得超過 eligible leaf count。
- 兩者皆為 0 時等同 baseline，config 不得建立重複 baseline cell。

預設 smoke variants：

```text
interior_k=5, leaf_k=0
interior_k=5, leaf_k=5
```

歷史設定可直接表示為：

```text
interior_k=92, leaf_k=40
interior_k=92, leaf_k=92
```

不使用 ratio 或 rounding 規則。

## 10. Prefetch Backend

Experiment config 必須以有序陣列明確指定一個或多個backends：

```json
{
  "backends": ["madvise", "pread"]
}
```

Backend值只能為`madvise`或`pread`，同一陣列不得重複。Orchestrator必須依陣列順序執行backends。Backend必須納入cell identity。

Baseline不屬於任何backend，每個layout、workload、measurement file與repetition只得執行一次baseline。Baseline的cell identity與cell JSON中的`backend`必須固定為JSON `null`。所有非baseline strategy variants必須分別使用config列出的每個backend執行。

### 10.1 Madvise

使用：

```c
madvise(address, length, MADV_WILLNEED)
```

計時範圍：

```text
第一個madvise呼叫開始
→ 最後一個madvise返回
```

輸出：

```text
madvise_dispatch_us
prefetch_elapsed_us
```

每個 runner job 必須依下列順序建立與釋放 mapping：

1. 以 `O_RDONLY` 開啟 DB。
2. 以 `fstat()` 取得 file size。
3. 讀取 SQLite header 取得 SQLite page size。
4. 以 `sysconf(_SC_PAGESIZE)` 取得 OS page size。
5. 驗證所有 selected offset、length、OS page alignment 與 file boundary。
6. 以 `mmap(NULL, file_size, PROT_READ, MAP_SHARED, fd, 0)` 建立完整 DB mapping。
7. 同一 job 的所有 `madvise` 共用此 mapping。
8. Job 結束時執行 `munmap()` 與 `close()`。

每個 runner job 只得建立一次完整 DB mapping。Open、`fstat()`、header parsing、JSON/CSV parsing、page selection、`mmap()` 與 `munmap()` 不得納入 prefetch elapsed time。

### 10.2 Pread

使用 buffered `pread()`，不得使用 `O_DIRECT`。

計時範圍：

```text
第一個pread呼叫開始
→ 最後一個pread返回
```

輸出：

```text
pread_elapsed_us
prefetch_elapsed_us
```

Pread 必須支援 per-page 與 contiguous-range extents。

Pread backend 不得建立 DB mapping。Runner 必須以 `O_RDONLY` 開啟 DB，在 prefetch 計時前配置 chunk buffer，並於 job 結束時釋放 buffer 與關閉 file descriptor。Open、header parsing、JSON/CSV parsing、page selection、buffer allocation 與buffer釋放不得納入prefetch elapsed time。

### 10.3 Pread Chunk

預設 chunk 上限：

```text
1 MiB = 1,048,576 bytes
```

Config 必須明確記錄 `pread_chunk_bytes`。Smoke config 固定使用 1,048,576 bytes。其他 experiment 允許指定不同值。

規則：

- 必須大於 0。
- 必須為 SQLite page size 的整數倍。
- 必須納入 cell identity。
- Buffer 必須在 prefetch 計時開始前配置。
- 大於 chunk 上限的 extent 必須依 chunk 上限分段 `pread`。

SQLite page size 預期為 4096 bytes；runner 仍必須從 DB header 讀取實際 page size。

## 11. Selected Pages CSV

Runner 必須為每個非-baseline cell 產生 selected-pages CSV：

```csv
page_number,page_type,file_offset,length,residency_count,selection_rank,io_operation_index,prefetch_succeeded
```

每個 selected page 一列。每個selected page必須對應一個`io_operation_index`。`prefetch_succeeded`只能為`0`或`1`。Range與chunk資訊記錄於prefetch-result JSON。

### 11.1 Classification CSV

`classify.csv` 欄位固定為：

```csv
page_number,page_type,file_offset
```

規則：

- 每個SQLite page一列。
- `page_number`從1開始，連續至SQLite page count。
- `file_offset = (page_number - 1) × SQLite page size`。
- `page_type`只能為下列值：

```text
interior_index
interior_table
leaf_index
leaf_table
freelist_trunk
freelist_leaf
overflow
lock_page
unknown
```

### 11.2 Residency Snapshot CSV

Residency snapshot CSV欄位固定為：

```csv
page_number,is_resident
```

規則：

- 每個SQLite page一列。
- `page_number`從1開始，連續至SQLite page count。
- `is_resident`只能為`0`或`1`。
- SQLite page涵蓋的所有OS pages均為resident時，`is_resident`才得記為`1`。

### 11.3 Prefetch Job JSON

每個非-baseline cell必須產生一份prefetch job JSON。Schema固定為：

```json
{
  "schema_version": 1,
  "cell_id": "abc123",
  "backend": "pread",
  "strategy": "residency_topk",
  "variant": "interior5_leaf5",
  "memory_condition": {
    "name": "512m",
    "enabled": true,
    "memory_max_bytes": 536870912
  },
  "database": {
    "path": "/absolute/path/database.db",
    "sha256": "..."
  },
  "classification": {
    "path": "/absolute/path/classify.csv",
    "sha256": "..."
  },
  "training_profile": {
    "path": "/absolute/path/residency_counts.csv",
    "sha256": "..."
  },
  "parameters": {
    "n": null,
    "interior_k": 5,
    "leaf_k": 5,
    "pread_chunk_bytes": 1048576
  },
  "output": {
    "result_json": "/absolute/path/prefetch_result.json",
    "selected_pages_csv": "/absolute/path/selected_pages.csv"
  }
}
```

規則：

- 所有path必須為絕對路徑。
- `range_interior`的`n`、`interior_k`與`leaf_k`必須為`null`。
- `offset_topk_interior`只有`n`得為非`null`值。
- `residency_topk`只有`interior_k`與`leaf_k`得為非`null`值。
- Structure-based strategy的`training_profile`必須為`null`。
- Baseline不得建立prefetch job JSON。
- `pread_chunk_bytes`必須在兩種backend的job JSON中保留。
- `memory_condition`必須與cell identity中的正規化memory condition完全一致。

## 12. Prefetch Result JSON

`prefetch_result.json` 至少包含：

- Schema version。
- Cell ID。
- Status。
- Backend。
- Strategy。
- Variant label。
- Memory condition名稱、enabled狀態與正規化後的MemoryMax bytes。
- N、`interior_k`、`leaf_k`。
- Eligible interior/leaf count。
- Selected interior/leaf count。
- Selected unique page count。
- Extent count。
- Syscall attempted/succeeded/failed count。
- Bytes requested/completed。
- Short-read count。
- `prefetch_elapsed_us`。
- `madvise_dispatch_us` 或 `pread_elapsed_us`。
- Pread chunk bytes。
- Selected-pages CSV path。
- 每個 syscall error 的 offset、length、errno 與 message。
- 每個I/O operation的operation index、offset、length、first page、last page、backend call count、bytes completed、success與errno。

每個I/O operation的JSON物件固定包含：

```json
{
  "operation_index": 3,
  "offset": 40960,
  "length": 1048576,
  "first_page": 11,
  "last_page": 266,
  "backend_calls": 1,
  "bytes_completed": 1048576,
  "success": true,
  "errno": null
}
```

Successful-page mapping規則：

- Madvise per-page operation必須依該次`madvise`的return code標記該page。
- Madvise range operation成功時，range內所有pages必須標記為successful。
- Madvise range operation失敗時，range內所有pages必須標記為failed。
- Pread range必須先依`pread_chunk_bytes`切割，chunk boundary必須保持SQLite page aligned。
- 每個selected page只能屬於一個pread chunk。
- Pread遇到`EINTR`時必須重試。
- Pread發生short read時必須繼續讀取剩餘bytes，直到完成、EOF或error。
- 完整讀取一個page的全部bytes後，該page必須標記為successful。
- EOF或error前未完整讀取的page必須標記為failed。
- EOF或error前已完整讀取的pages必須維持successful。

## 12.1 Benchmark Harness 整合契約

Orchestrator 必須使用下列 CLI 形狀呼叫既有 `benchmark_harness`：

```bash
benchmark_harness \
  --db <layout-db> \
  --workload <workload-file> \
  --output <operations.csv> \
  --record-dir <record-directory> \
  --mmap-size <database-file-size> \
  --cold-advice none \
  --sqlite-open-timing before-cold \
  --schema-init-timing before-cold \
  --drop-caches-script <drop-cache-helper> \
  [--drop-caches-use-sudo] \
  [--post-cold-script <post-cold-script>]
```

Baseline cell 不得傳入 `--post-cold-script`。非baseline cell 必須傳入 `--post-cold-script`。

Orchestrator 必須從 harness stderr 的下列行取得實際 run record path：

```text
benchmark record: <path>
```

Run record 必須包含並由orchestrator解析：

- `record_path`
- `db`
- `workload`
- `output`
- `cold_advice`
- `drop_caches_script`
- `drop_caches_use_sudo`
- `post_cold_script`
- `sqlite_open_timing`
- `schema_init_timing`
- `file_size`
- `sqlite_page_size`
- `sqlite_pages`
- `workload_ops`
- Helper result欄位。
- Before-cold、after-cold與after-run residency count、distribution與ranges。
- `ops`
- `avg_latency_us`
- `first_query_latency_us`
- `total_majflt`
- `total_minflt`

Harness operations CSV欄位固定為：

```csv
op_no,op_type,target_id,rows_returned,bytes_returned,elapsed_ns,majflt_delta,minflt_delta
```

## 12.2 Post-cold Script 契約

Post-cold script 必須：

- 不接受命令列參數。
- 為可執行的一般檔案。
- 使用cell專屬prefetch job JSON的絕對路徑。
- 執行 `prefetch_runner --job <prefetch-job.json>`。
- 將stdout與stderr交由harness繼承。
- 在exit 0前完成`prefetch_result.json`與selected-pages CSV的寫入。

Post-cold script或runner以非0結束時，harness必須以非0結束，cell必須記錄為failed。

## 12.3 Drop-cache Helper 契約

Drop-cache helper 必須：

- 不接受命令列參數。
- 為可執行的一般檔案。
- 自行執行`sync`。
- 將`3`寫入`/proc/sys/vm/drop_caches`。
- 成功時exit 0。
- 失敗時以非0結束。

Harness以root執行時必須直接執行helper。Harness以非root執行且config要求sudo時，必須以`sudo -n`執行helper。Helper stdout與stderr由harness繼承。Helper非0結束時，harness必須以非0結束，cell必須記錄為failed。

## 13. Cold-start Protocol

模擬情境：

```text
應用程式仍存活
SQLite connection與prepared statements仍存在
DB file-backed pages已被回收
```

正式 harness 設定：

```text
sqlite_open_timing = before-cold
schema_init_timing = before-cold
cold_advice = none
```

正式 measurement 順序：

```text
benchmark_harness mmap DB
→ SQLite open
→ prepare statements
→ harness記錄cold前residency
→ harness呼叫drop-cache helper
→ harness呼叫optional post-cold prefetch runner
→ harness記錄after-cold residency
→ 第一筆query
→ 剩餘workload
→ harness記錄after-run residency
```

Cold boundary 由 `benchmark_harness` 控制。Drop-cache helper 與 prefetch runner 為外部 executable，由 harness 在指定位置呼叫。

Baseline 不執行 post-cold script。

## 14. Residency Measurement

Prefetch runner 不得執行 residency check。

Measurement residency 使用 harness run record：

- Before-cold residency。
- After-cold/prefetch residency。
- After-run residency。
- 完整 resident page ranges。

Orchestrator 必須將 selected page list 與 after-cold resident ranges 做交集，計算：

```text
requested_selected_resident_ratio
successful_selected_resident_ratio
```

Requested ratio 的分母為所有 requested pages。Successful ratio 的分母為prefetch成功的pages。

Selected pages為0或ratio分母為0時：

- Cell JSON中的對應ratio必須為`null`。
- Raw results CSV中的對應ratio欄位必須留空。
- Summary不得將該sample納入對應resident-ratio統計。
- Ratio不得記為`0`。

Baseline不產生selected pages或selected resident ratio。`offset_topk_interior`的N必須大於0。`residency_topk`不得同時使用`interior_k=0`與`leaf_k=0`。`range_interior`在eligible interior count為0時必須於preflight失敗。

## 15. Memory Conditions

Config必須以有序非空陣列明確提供一個或多個memory conditions：

```json
"memory_conditions": [
  {"name": "unlimited", "enabled": false},
  {"name": "512m", "enabled": true, "memory_max": "512MiB"}
]
```

規則：

- `name`必須唯一，且必須符合regex `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`。名稱長度必須為1至64個ASCII字元。
- `enabled`必須為boolean。
- `enabled=false`時，`memory_max`必須省略或為`null`。
- `enabled=false`時，正規化後的`memory_max_bytes`必須為JSON `null`。
- `enabled=true`時，`memory_max`為必填字串，且必須符合regex `^[1-9][0-9]*(B|KiB|MiB|GiB)$`。單位區分大小寫，不得使用小數、空白、無單位數值或其他單位。
- 單位換算固定為`B=1`、`KiB=1024`、`MiB=1024^2`、`GiB=1024^3` bytes。
- Orchestrator必須以精確整數運算將`memory_max`正規化為`memory_max_bytes`。超出實作整數範圍時，preflight必須失敗。
- Memory condition identity必須包含`name`、`enabled`與`memory_max_bytes`，不得只使用`name`。
- Orchestrator必須依陣列順序執行memory conditions。
- 陣列第一個memory condition必須作為`memory_comparison.csv`的reference condition。

相同training workload清單與measurement workload清單必須套用至所有memory conditions。每個layout、workload type與memory condition必須建立獨立training profile。

Memory condition的`enabled=true`時，下列完整measurement segment必須位於同一個`systemd-run --user --scope`：

```text
benchmark_harness
→ drop cache
→ optional prefetch
→ after-cold residency
→ query workload
→ after-run residency
```

Memory condition的`enabled=true`時，training `benchmark_harness`位於memory-limited scope。外部`residency_checker`在scope結束後立即執行，不受memory limit。

每個measurement與training cell必須產生cell專屬wrapper script。當前memory condition的`enabled=false`時，orchestrator必須直接執行wrapper。當前memory condition的`enabled=true`時，orchestrator必須使用下列命令形狀執行wrapper：

```bash
systemd-run --user --scope --collect \
  --unit=<cell-unit-name> \
  -p MemoryMax=<memory-max-bytes> \
  <cell-wrapper-script>
```

Cell unit name必須由cell ID產生並保持唯一。Wrapper必須將stdout、stderr與artifacts直接寫入experiment cell與log目錄。Orchestrator必須使用前景執行的`systemd-run --scope` process exit code作為cell process exit code，不得加入`--wait`。

Memory-limited cell timeout時，orchestrator必須執行：

```bash
systemctl --user kill --kill-whom=all <cell-unit-name>.scope
```

Scope內所有process終止後，cell必須記錄為timeout，orchestrator必須繼續下一個cell。

下列工作不受 memory limit：

- Layout provisioning。
- Page classification。
- Training profile aggregation。
- Result summary。
- Plot generation。

Smoke config必須只列出一個`enabled=false`且名稱為`unlimited`的memory condition。

## 16. Experiment Config

Config 必須包含以下區段：

```text
schema_version
experiment
paths
layouts
workloads
prefetch
cold_protocol
memory_conditions
execution
statistics
```

不得依賴未記錄於 config 的隱式實驗參數。

### 16.1 Experiment

必要欄位：

```text
id
output_root
resume
```

### 16.2 Paths

必要欄位：

```text
workloads_directory
benchmark_harness
prefetch_runner
classify_pages
residency_checker
drop_caches_script
```

Layout provisioning path 不屬於 experiment config。

### 16.3 Workloads

必要欄位：

```text
types
sampling_seed
training.pool_start
training.pool_end_inclusive
training.count
measurement.pool_start
measurement.pool_end_inclusive
measurement.count
measurement.repetitions
```

### 16.4 Prefetch

必要欄位：

```text
backends
pread_chunk_bytes
strategies
```

每個 strategy 的所有 N/K/variant 必須明確列出。

### 16.5 Memory Conditions

必要欄位：

```text
memory_conditions[].name
memory_conditions[].enabled
```

`enabled=true`時另需：

```text
memory_conditions[].memory_max
```

### 16.6 Execution

必要欄位：

```text
layout_order
strategy_order
cell_timeout_seconds
```

Smoke cell timeout 固定為：

```text
180 seconds
```

### 16.7 Statistics

必要欄位：

```text
percentiles
percentile_method
```

Smoke 使用：

```text
percentiles = [25, 50, 75, 99]
percentile_method = nearest_rank
```

## 17. Preflight Validation

Orchestrator 必須在建立 experiment 目錄、執行 training 或執行 measurement 前完成 preflight。

必須檢查：

- Config 所有必要欄位存在且型別正確。
- Experiment ID 合法。
- 所有必要工具存在。
- Drop-cache helper 存在、為一般檔案且可執行。
- 不測試 drop-cache helper 是否真的能清除 cache。
- Enabled layout DB、classification與metadata存在。
- Workload directory與所有選用files存在。
- Training/measurement pool、count與repetition合法。
- Backends陣列至少包含一個值、值合法且不重複。
- Strategy、variant與sweep合法。
- N為正整數且不超過eligible interior count。
- K為非負整數且不超過對應eligible page count。
- 不存在重複baseline cell。
- Pread chunk合法。
- Memory conditions陣列非空、名稱唯一、順序固定，且每個condition的enabled與memory_max組合合法。
- Cell timeout為正整數。

Preflight失敗時：

- 將全部validation errors輸出至stderr。
- Process exit code非0。
- 不建立experiment目錄。
- 不建立validation report JSON。
- 不執行training或measurement。

## 18. Cell 與執行順序

Cell identity 至少包含：

- Database SHA-256。
- Layout。
- Training workload SHA-256清單。
- Measurement workload SHA-256。
- Strategy與variant參數。
- Backend。
- Pread chunk bytes。
- Cold protocol。
- Memory condition名稱、enabled狀態與正規化後的MemoryMax bytes。
- Repetition index。
- Tool SHA-256清單。

Baseline cell identity中的`backend`必須為JSON `null`。非baseline cell identity中的`backend`必須為`madvise`或`pread`。

執行大順序：

```text
original layout-only
vacuum layout-only
rewrite layout-only

original + prefetch cells
vacuum + prefetch cells
rewrite + prefetch cells
```

各layout與memory condition的baseline cells同時定義為layout-only cells與same-layout、same-memory-condition prefetch comparison baseline。Orchestrator不得另行建立或執行重複的layout-only cells。Orchestrator必須先完成所有layout與memory condition的baseline cells，再依layout與memory condition順序執行prefetch cells。

Workload type依config順序。Memory condition依`memory_conditions`陣列順序。Strategy與variant依config順序。Backend依`prefetch.backends`陣列順序。Measurement files依抽樣順序。

每個layout、workload type、memory condition、measurement file與repetition的baseline必須只執行一次。每個非baseline strategy variant必須在每個memory condition下依序執行config列出的全部backends。

每個measurement file、strategy與repetition執行前必須重新清空cache。

## 19. Timeout 與失敗

### 19.1 Memory Condition 未啟用限制

Orchestrator必須以獨立process group啟動cell wrapper，並使用等同於`start_new_session=true`的process建立方式。

Cell超過`cell_timeout_seconds`時：

1. 對process group送出`SIGTERM`。
2. 等待grace period。
3. 尚未結束時送出`SIGKILL`。
4. 等待並回收child processes。
5. Cell記錄`status=timeout`。
6. 保存stdout與stderr。
7. 繼續下一個cell。

### 19.2 Memory Condition 啟用限制

Systemd scope必須作為主要終止單位。Cell超過`cell_timeout_seconds`時：

1. 執行`systemctl --user kill --kill-whom=all <cell-unit-name>.scope`。
2. 終止仍存在的本地`systemd-run` process。
3. 等待並回收scope與child processes。
4. Cell記錄`status=timeout`。
5. 保存stdout與stderr。
6. 繼續下一個cell。

當前memory condition的`enabled=true`時，不得只終止本地`systemd-run` process而保留scope內process。

Runtime cell失敗、timeout或invalid不得終止其餘cells。所有狀態均必須記錄。

## 20. Resume

Experiment ID規則：

- 目錄不存在：建立新experiment。
- 目錄存在且config SHA-256相同：resume。
- 目錄存在但config SHA-256不同：拒絕執行。

工具SHA-256改變時，原cell不得視為相同cell，必須重新執行。

已完成cell跳過。中斷中的cell從該measurement workload與repetition開頭重跑，不從query或operation level恢復。

Cell只有在以下artifact同時存在且可解析時才算完成：

```text
cell.json且status=completed
benchmark_harness run record
operations CSV
prefetch_result.json（非baseline）
```

`cell.json` 必須使用temporary file寫入後atomic rename。

## 21. Result JSON

每個cell的`cell.json`至少記錄：

- Experiment ID與cell ID。
- Status。
- Layout、DB SHA-256。
- Workload type。
- Training profile SHA-256。
- Measurement file與SHA-256。
- Repetition。
- Strategy、variant與backend。
- Memory condition名稱、enabled狀態與正規化後的MemoryMax bytes。
- N、`interior_k`、`leaf_k`。
- Prefetch result摘要。
- Harness query metrics。
- Residency metrics。
- Artifact paths。
- Error與timeout資訊。

Baseline `cell.json`中的`backend`必須為JSON `null`。非baseline `cell.json`中的`backend`必須記錄實際執行的backend。

完整provenance、hash、paths與errors以JSON、run record與logs為準。

## 22. 分類式結果目錄

Experiment結果必須依下列固定層級分類：

```text
workload type
→ database layout
→ memory condition
→ baseline，或backend
→ strategy variant
```

Baseline結果目錄固定為：

```text
experiments/<experiment-id>/results/<workload-type>/<layout>/memory_conditions/<memory-condition>/baseline/
```

非baseline strategy variant結果目錄固定為：

```text
experiments/<experiment-id>/results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backends/<backend>/<strategy-key>/
```

`strategy-key` 必須唯一識別strategy與參數，格式固定為：

```text
baseline
range_interior
offset_topk_interior_n<N>
residency_topk_<variant>_i<interior-k>_l<leaf-k>
```

`variant`必須轉換為只包含小寫英文字母、數字、連字號與底線的filesystem-safe字串。同一backend目錄內的`strategy-key`不得重複。

每個strategy variant目錄必須包含：

```text
raw.csv
summary.csv
```

Experiment根目錄不得產生`raw_results.csv`或單一大型`summary.csv`。跨分類的完整raw索引只得產生於`results/all_raw.csv`。

## 22.1 Raw Results CSV

每個`raw.csv`只包含相同workload type、layout、memory condition與strategy variant的cells。每列代表一個cell。欄位固定為：

```csv
cell_id,status,measurement_file,repetition,selected_interior,selected_leaf,syscall_count,prefetch_elapsed_us,first_query_latency_us,average_latency_us,major_page_faults,minor_page_faults,resident_after_cold_pages,requested_selected_resident_ratio,successful_selected_resident_ratio
```

Experiment ID、workload type、layout、memory condition、strategy、variant、backend與N/K參數不得重複寫入`raw.csv`；這些值由experiment目錄、memory condition與baseline/backend結果目錄、manifest與cell JSON唯一決定。

失敗或timeout cell的不可用數值欄位留空。詳細錯誤只記錄於cell JSON與logs。

不產生`prefetch_elapsed_us + first_query_latency_us`合成指標。

### 22.2 Consolidated Raw Results CSV

每次彙整必須產生：

```text
experiments/<experiment-id>/results/all_raw.csv
```

`all_raw.csv`必須包含實驗內全部cells，每個cell恰好一列，並依實際執行順序排列。此檔案用於跨分類查閱與複製，不得取代各strategy variant目錄中的`raw.csv`。

欄位固定為：

```csv
experiment_id,cell_id,status,workload_type,layout,memory_condition,memory_limit_enabled,memory_max_bytes,strategy_key,backend,n,interior_k,leaf_k,measurement_file,repetition,selected_interior,selected_leaf,syscall_count,prefetch_elapsed_us,first_query_latency_us,average_latency_us,major_page_faults,minor_page_faults,resident_after_cold_pages,requested_selected_resident_ratio,successful_selected_resident_ratio
```

Baseline列的`backend`、`n`、`interior_k`與`leaf_k`必須留空。非適用或不可用欄位必須留空，不得填入推測值。`all_raw.csv`中的數值必須與對應cell JSON及分類後`raw.csv`一致。

## 23. Strategy Summary CSV

每個strategy variant目錄的`summary.csv`採long-form，一列代表一個metric的聚合結果。欄位固定為：

```csv
scope,measurement_file,metric,sample_count,mean,median,p25,p75,p99,min,max,comparison_basis,improvement_percent
```

Experiment ID、workload type、layout、memory condition、strategy、variant、backend與N/K參數不得重複寫入strategy-level `summary.csv`；這些值由目錄、manifest與cell JSON唯一決定。

`scope`：

```text
measurement: 同一measurement file的所有repetitions
strategy: 所有抽中的measurement files與repetitions
```

`metric` 至少包含：

```text
prefetch_elapsed_us
first_query_latency_us
average_latency_us
major_page_faults
minor_page_faults
resident_after_cold_pages
requested_selected_resident_ratio
successful_selected_resident_ratio
```

Query latency improvement 同時計算：

- First-query latency。
- Average-query latency。

公式：

```text
improvement_percent = (baseline - strategy) / baseline × 100
```

正數表示改善，負數表示退化。Prefetch cost不計算improvement百分比。

Prefetch strategy必須與相同layout、workload type、memory condition、measurement file與repetition的baseline配對後計算sample improvement，再進行聚合。

Layout comparison使用相同memory condition下的original layout baseline。Prefetch comparison使用same-layout、same-memory-condition baseline。

所有raw samples必須保留，不定義、不標記、不刪除outlier。

統計量：

```text
sample_count
mean
median
p25
p75
p99
min
max
```

Percentile方法固定由config指定；smoke使用`nearest_rank`。

## 23.1 Strategy Comparison CSV

每個layout、memory condition與backend目錄必須產生：

```text
experiments/<experiment-id>/results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backends/<backend>/strategy_comparison.csv
```

此檔案必須包含相同memory condition下唯一的baseline與該workload type、layout、memory condition、backend下的全部非baseline strategy variants，並使用該baseline計算prefetch query latency improvement。Baseline row的`strategy_key`必須為`baseline`。

`strategy_comparison.csv`欄位固定為：

```csv
strategy_key,metric,sample_count,mean,median,p25,p75,p99,min,max,baseline_mean,improvement_percent
```

`strategy_key`必須等於對應strategy result目錄名稱。Experiment ID、workload type、layout、memory condition與backend不得重複寫入`strategy_comparison.csv`；memory condition與backend由父目錄名稱唯一決定。

## 23.2 Backend Comparison CSV

每個layout與memory condition目錄必須產生：

```text
experiments/<experiment-id>/results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backend_comparison.csv
```

此檔案必須包含相同memory condition下唯一的baseline，並彙整config列出的全部backends與非baseline strategy variants。此檔案必須使用該baseline計算query latency improvement。Baseline row的`backend`欄位必須留空，`strategy_key`必須為`baseline`。

`backend_comparison.csv`欄位固定為：

```csv
backend,strategy_key,metric,sample_count,mean,median,p25,p75,p99,min,max,baseline_mean,improvement_percent
```

## 23.3 Memory Comparison CSV

每個layout目錄必須產生：

```text
experiments/<experiment-id>/results/<workload-type>/<layout>/memory_comparison.csv
```

此檔案必須比較相同layout、strategy variant與backend在所有memory conditions下的結果。Config中第一個memory condition為reference condition。

相同strategy variant與backend的memory comparison必須使用相同measurement file與repetition進行配對後再聚合。Baseline的backend必須為`null`，並在所有memory conditions間獨立比較。

每個metric只得納入reference condition與目標condition皆有有效數值的paired samples。`sample_count`必須記錄實際納入的paired sample數；`mean`、`median`、`p25`、`p75`、`p99`、`min`與`max`必須以目標condition的paired sample數值計算，`reference_mean`必須以同一批paired reference數值計算。

`memory_comparison.csv`欄位固定為：

```csv
memory_condition,backend,strategy_key,metric,sample_count,mean,median,p25,p75,p99,min,max,reference_mean,change_percent
```

Baseline row的`backend`欄位必須留空。每個有效paired sample的變化率公式固定為：

```text
paired_change_percent = (condition - reference) / reference × 100
```

CSV中的`change_percent`必須為全部有效`paired_change_percent`的算術平均。Reference值為0的pair不得納入百分比計算；沒有任何有效百分比pair時，`change_percent`必須留空。

`change_percent`只表示指標變化方向，不表示改善：正值表示目標condition的指標增加，負值表示目標condition的指標降低。Latency與prefetch cost降低時為改善；resident ratio與resident page count增加時為改善。

## 23.4 Layout Comparison CSV

每個workload type目錄必須產生：

```text
experiments/<experiment-id>/results/<workload-type>/layout_comparisons/<memory-condition>.csv
```

每個memory condition必須各自產生一份layout comparison。此檔案只得使用相同memory condition下enabled layouts的baseline cells。Original layout為layout comparison baseline。Original、vacuum與rewrite全部啟用時，此檔案必須比較三種database layouts。

每個layout comparison CSV的欄位固定為：

```csv
layout,metric,sample_count,mean,median,p25,p75,p99,min,max,original_baseline_mean,improvement_percent
```

Experiment ID、workload type與memory condition不得重複寫入layout comparison CSV。`layout`必須保留，作為跨layout比較識別欄位。Layout comparison只使用backend-neutral baseline cells，因此不得包含backend欄位。

## 24. Trade-off Plot

Experiment完成後，orchestrator必須依序呼叫：

```text
summarize_results.py
plot_tradeoff.py
generate_report.py
```

`summarize_results.py`必須產生`results/all_raw.csv`、全部strategy-level `summary.csv`、per-memory-condition與per-backend `strategy_comparison.csv`、`backend_comparison.csv`、`memory_comparison.csv`與per-memory-condition layout comparison CSV。`plot_tradeoff.py`必須從各layout與memory condition的`backend_comparison.csv`讀取繪圖資料。

Trade-off圖定義：

```text
X軸: prefetch_elapsed_us
Y軸: first-query improvement percent
```

每張圖只得包含檔名所指定backend的資料。每個點代表：

```text
layout × workload type × memory condition × strategy variant
```

X軸與Y軸均顯示p25–p75 error bars。Y軸使用same-layout paired baseline improvement。同一backend圖中必須以顏色區分memory conditions，並以不同點形區分layout與strategy variant；圖例必須完整標示對應關係。

輸出：

```text
experiments/<experiment-id>/plots/tradeoff_<backend>.png
experiments/<experiment-id>/plots/tradeoff_points.csv
```

`plot_tradeoff.py`必須為config列出的每個backend各產生一張PNG。`<backend>`必須使用config中的backend名稱。

`tradeoff_points.csv`跨越所有workload types、layouts、memory conditions與strategy variants，因此必須保留完整分類欄位。欄位固定為：

```csv
workload_type,layout,memory_condition,strategy_key,backend,prefetch_median_us,prefetch_p25_us,prefetch_p75_us,first_query_improvement_median,first_query_improvement_p25,first_query_improvement_p75
```

## 25. Markdown Report

Experiment完成後必須產生：

```text
experiments/<experiment-id>/report.md
```

`report.md`必須使用繁體中文，並包含下列章節：

```text
實驗摘要
執行環境
各workload type結果
Layout比較
各layout的strategy比較
Prefetch cost與first-query improvement trade-off
Cell狀態
Training與measurement workload清單
Artifacts連結
```

### 25.1 實驗摘要

實驗摘要表格必須至少列出：

- Experiment ID。
- Prefetch backends與執行順序。
- Enabled layouts。
- Workload types。
- Training file count。
- Measurement file count。
- Measurement repetitions。
- Memory conditions、執行順序、enabled狀態與MemoryMax。
- SQLite page size。
- Completed、failed、timeout與invalid cell count。

### 25.2 Layout 比較表

每個workload type與memory condition必須產生一個layout baseline比較表。表格至少包含：

```text
Layout
First-query latency median
First-query P25–P75
First-query P99
First-query improvement vs original
Average-query latency median
Average-query improvement vs original
```

### 25.3 Strategy 比較表

每個workload type、layout、memory condition與backend必須產生一個strategy比較表。表格至少包含：

```text
Strategy key
Prefetch cost median
First-query latency median
First-query improvement vs same-layout baseline
Average-query latency median
Average-query improvement vs same-layout baseline
Requested selected resident ratio
Successful selected resident ratio
Major page faults
Minor page faults
```

Strategy比較正文表格必須同時呈現first-query latency與average-query latency。Baseline的prefetch cost與selected resident ratio必須顯示為`—`。

每個strategy比較表之後必須提供distribution詳細表，至少包含下列metrics的P25、median、P75與P99：

```text
prefetch_elapsed_us
first_query_latency_us
average_latency_us
major_page_faults
minor_page_faults
requested_selected_resident_ratio
successful_selected_resident_ratio
```

### 25.4 Backend 比較表

Config列出多個backends時，每個workload type、layout與memory condition必須產生backend比較表。表格至少包含：

```text
Backend
Strategy key
Prefetch cost median
Prefetch cost P25–P75
First-query latency median
First-query improvement vs shared baseline
Average-query latency median
Average-query improvement vs shared baseline
Requested selected resident ratio
Successful selected resident ratio
```

報告必須標示`madvise`的prefetch cost為非同步request submission時間，`pread`的prefetch cost為同步read完成時間。

### 25.5 Memory Condition 比較表

Config列出多個memory conditions時，每個workload type與layout必須產生memory condition比較表。表格至少包含：

```text
Memory condition
Backend
Strategy key
Prefetch cost median
First-query latency median
First-query change vs reference condition
Average-query latency median
Average-query change vs reference condition
```

報告必須標示config中第一個memory condition為reference condition。

### 25.6 Trade-off

Markdown報告必須依config中的backend順序嵌入每張圖：

```markdown
![madvise prefetch cost 與 first-query improvement](plots/tradeoff_madvise.png)
![pread prefetch cost 與 first-query improvement](plots/tradeoff_pread.png)
```

圖後必須附上trade-off點位表，至少包含：

```text
Workload type
Layout
Memory condition
Strategy key
Backend
Prefetch median與P25–P75
First-query improvement median與P25–P75
```

### 25.7 Cell 與 Workload 狀態

報告必須列出各cell status的數量。Failed、timeout與invalid cells必須列出cell ID、workload、layout、strategy與對應log連結。

報告必須依manifest列出實際使用的training與measurement workloads、抽樣順序、index與repetition count。

### 25.8 Artifact 連結

報告必須使用相對路徑連結至：

- Experiment config。
- Experiment manifest。
- Consolidated raw results `results/all_raw.csv`。
- 各workload type與memory condition的layout comparison CSV。
- 各layout、memory condition與backend的strategy comparison CSV。
- 各layout與memory condition的backend comparison CSV。
- 各layout的memory comparison CSV。
- Trade-off data CSV。
- Failed、timeout與invalid cell logs。

報告不得推算或填補缺失數值。缺失數值必須顯示為`N/A`。Latency與prefetch cost在Markdown表格中統一使用microseconds。

## 26. Environment Metadata

Experiment manifest至少記錄：

- Linux kernel version。
- Hostname。
- CPU model與logical CPU count。
- Total RAM。
- Filesystem type。
- Storage device資訊。
- SQLite version。
- SQLite page size。
- Tool paths與SHA-256。
- Config SHA-256。
- Sampling seed。
- 固定後的training與measurement清單及其SHA-256。
- Memory conditions的名稱、順序、enabled狀態、原始MemoryMax字串與正規化MemoryMax bytes。

## 27. Smoke Experiments

必須提供一份smoke config：

```text
configs/smoke.json
```

Smoke設定：

```text
layout: original
workload type: read_zipf_full
training file count: 5
measurement file count: 5
measurement repetitions: 1
memory conditions: [{name: unlimited, enabled: false}]
cell timeout: 180 seconds
pread chunk: 1 MiB
backends: [madvise, pread]
```

Smoke策略：

```text
baseline
range_interior
offset_topk_interior N=5
residency_topk interior_k=5 leaf_k=0
residency_topk interior_k=5 leaf_k=5
```

Smoke experiment共：

```text
baseline: 5 measurement files × 1 = 5 cells
non-baseline: 5 measurement files × 4 strategy variants × 2 backends = 40 cells
total: 45 cells
```

### 27.1 Smoke成功條件

- Preflight成功。
- 固定5個training files與5個measurement files。
- 產生5份training residency snapshots。
- 產生可解析的training profile。
- Eligible interior與leaf均至少5頁。
- 展開45個unique cells。
- 所有cells在180秒內完成。
- 每個cell的必要artifacts完整且可解析。
- Baseline不產生prefetch result。
- 每個非baseline strategy variant分別以madvise與pread執行，並產生prefetch result與selected-pages CSV。
- Madvise cells記錄`madvise_dispatch_us`。
- Pread cells記錄`pread_elapsed_us`，且成功reads的bytes completed等於bytes requested。
- Harness記錄before-cold、after-cold與after-run residency。
- Baseline目錄產生`raw.csv`與`summary.csv`，`raw.csv`有5列。
- 兩個backend各有四個非baseline strategy variant目錄，每個目錄均產生`raw.csv`與`summary.csv`，每個`raw.csv`有5列。
- 全部`raw.csv`合計45列。
- `results/all_raw.csv`產生成功、恰有45列資料，且每個cell ID唯一。
- 每個strategy-level `summary.csv`同時包含measurement與strategy scope。
- Original layout的`unlimited` memory condition下，兩個backend目錄各產生`strategy_comparison.csv`。
- Original layout的`unlimited` memory condition目錄產生`backend_comparison.csv`。
- Original layout目錄產生`memory_comparison.csv`。
- `read_zipf_full/layout_comparisons/unlimited.csv`產生成功。
- First-query與average-query improvement可計算。
- Trade-off CSV與config中每個backend對應的PNG產生成功。
- `report.md`產生成功且包含實驗摘要、執行環境、layout比較、per-memory-condition與per-backend strategy比較、backend比較、memory condition比較、first-query與average-query latency、trade-off圖、cell狀態、workload清單與artifact連結。
- 相同config重跑時不得重新抽樣、重新training或重跑completed cells。
- Smoke不要求任何prefetch策略必須優於baseline。

## 28. 正式執行流程

```bash
cd static_experiment

make

python3 tools/src/build_layouts.py \
  --source <source.db> \
  --output-dir data/layouts

python3 tools/src/orchestrator.py \
  --config configs/smoke.json
```

正式N sweep、residency K矩陣、standard/full規模與正式MemoryMax值不屬於smoke規格。
