# 問題背景

在 Android 或嵌入式系統上，作業系統會主動回收背景 App 的記憶體。  

Android 的 ActivityManagerService 可透過 `process_madvise(MADV_COLD)` 把背景 App 的 file-backed pages 標記為冷頁面，讓 kernel 在記憶體壓力下優先回收。  

使用者切回 App 時，process 可能仍然存在，但 SQLite database 對應的 page cache 已經不完整，第一批 query 因此會遇到 cold-cache latency。  

SQLite 是最廣泛部署的嵌入式資料庫引擎。  

當 SQLite 啟用 `PRAGMA mmap_size` 時，database file 會被映射進 address space，資料頁面的載入與淘汰主要交由 OS page cache 管理。  

問題在於 OS 的 page cache 以通用的 page replacement policy 管理 4 KB page frames，並不知道 SQLite B+tree 的結構。  

從資料結構角度來看，B+tree 的 interior pages 通常位於查詢路徑上。  

冷啟動後，第一筆查詢可能需要依序 fault root page、中間層 interior page，最後才到 leaf page。  

這些 fault 具有相依性：前一層讀取完成後，SQLite 才知道下一層要走哪個 page。  

因此，即使單次 I/O 很快，多個 serial page faults 仍可能放大 first-query latency。  

本實驗不直接在 Android 裝置上量測，而是在 Linux workstation 上重現其中一個關鍵現象：SQLite process 與 connection 已存在，但 database file-backed pages 被清出 Linux page cache。  

本實驗以此作為受控情境，觀察不同 SQLite file layout 與 prefetch strategy 的組合，在 cold-cache 查詢中是否能降低延遲。  
實驗結果因此應解讀為 Linux cold-cache benchmark 的結果，而不是 Android 實機行為的直接量測。  

# 方法與實驗設計

本實驗評估不同 SQLite database layout 與 page prefetch strategy 所形成的完整 configurations，是否能改善 cold-cache 條件下的查詢延遲。  

實驗模擬的情境是：應用程式已經建立 SQLite connection 與 prepared statements，但 database file 對應的 Linux page cache 已被清除。  

在第一筆 query 執行前，系統可以選擇性地根據 database layout 或先前 training workloads 觀察到的 page residency pattern，預先載入部分 SQLite pages。  

此設定對應到真實系統中常見的冷啟動或記憶體壓力情境：process 本身仍然存在，但 file-backed pages 可能因系統記憶體壓力而被回收。  

## 研究問題

本實驗的研究問題聚焦於完整 configuration 在 cold-cache 情境下的表現，而不是單獨估計每個 component 的獨立因果貢獻。  
具體而言，本實驗回答下列問題：  

- RQ1：哪些完整 layout–prefetch configurations 能在納入 prefetch cost 後，相對「原始 layout 且不做 prefetch 的 baseline」降低 cold-cache latency？  
- RQ2：最佳完整 configuration 是否會隨 workload type、memory condition 與 metric 改變？  
- RQ3：這些最佳 configurations 呈現哪些共同模式，且在 exploratory benchmark 的限制下應如何解讀？  

後續分析會以完整 configuration 相對這個 baseline 的改善作為主要比較基準。  

## 實驗因素

本實驗比較五個主要因素：  

| Factor            | Values                                                                                                                                                     |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Database layout   | `original`, `vacuum`, `rewrite`                                                                                                                            |
| Workload type     | `read_zipf_full`, `read_zipf_tail`, `read_uniform_full`, `read_uniform_tail`, `scan_zipf_full`, `scan_zipf_tail`, `scan_uniform_full`, `scan_uniform_tail` |
| Memory condition  | `unlimited`, `20m`                                                                                                                                         |
| Prefetch backend  | `madvise`, `pread`                                                                                                                                         |
| Prefetch strategy | `baseline`, `range_interior`, `offset_topk_interior`, `residency_topk`                                                                                     |

`original` 是原始 SQLite database layout；`vacuum` 是透過 SQLite `VACUUM` 產生的 layout；`rewrite` 是由本專案 layout rewriter 產生的 layout。  
baseline 則表示不執行 prefetch 的比較組；主要結果中的 baseline 是 `original` layout 搭配 no-prefetch。  

三種 layout 使用相同資料，但 database file 中 pages 的排列方式不同。  

workload type 同時涵蓋 point-read 與 range-scan 類型，並比較 Zipfian 與 uniform key distribution。  

`full` 與 `tail` 代表不同查詢區域，使實驗能觀察資料分布與存取區域對 prefetch 效果的影響。  

memory condition 包含無限制執行與 `MemoryMax=20MiB` 的受限執行。  

後者用來模擬較強記憶體壓力下 page cache 與 process memory 競爭的情境。  

## 實驗資料庫

實驗使用同一份邏輯資料產生三個 SQLite database files，分別對應 `original`、`vacuum` 與 `rewrite` layout。  

三者的資料內容與查詢語意相同，差異只在 SQLite pages 於 database file 中的排列方式。  

這樣可以避免資料內容差異干擾 layout–prefetch configuration 的比較。  
不過，本實驗不單獨估計 layout、backend 與 strategy 各自的獨立因果貢獻。  

資料庫由專案中的 `data/source/source_db_builder.py` 產生。  

核心 schema 如下：  

```sql
CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  k1 TEXT NOT NULL,
  k2 TEXT NOT NULL,
  payload BLOB NOT NULL
);
CREATE INDEX idx_items_k1 ON items(k1);
CREATE INDEX idx_items_k2 ON items(k2);
```

資料庫包含 600,000 筆資料，每筆資料帶有兩個文字欄位與一個 100-byte payload。  

正式實驗中觀察到的 database file 約為 103 MiB，SQLite page size 為 4096 bytes，約 26k 個 SQLite pages。  

benchmark harness 會將 `PRAGMA mmap_size` 設為 database file size，使 SQLite 可透過 mmap 存取整個 database file。  

三種 layout 的意義如下：  

| Layout | Meaning |
| --- | --- |
| `original` | source database builder 直接產生的 layout |
| `vacuum` | 對相同資料執行 SQLite `VACUUM` 後得到的 layout |
| `rewrite` | 由本專案 layout rewriter 依 page classification 重新排列 pages，並修正 B-tree page pointers 後得到的實驗性 layout |

`rewrite` layout 由本專案 GitHub repository 中的 `tools/src/layout_rewriter.c` 產生。  

正文僅概述其設計方法；完整演算法與實作細節可在提交的 source code 中檢查。  

layout rewriter 會先讀取 page classification，建立新的 database image，將結構上較重要的 pages 放到較前面的 file offset，並在重寫後修正 B-tree page references。  

rewrite provisioning 會對 rewritten database 執行 SQLite `PRAGMA integrity_check`。  
正式 benchmark 也會在三種 layout 上執行相同 queries，以確認本實驗 workload 的查詢語意一致。  

在實驗前，每個 database file 都會先經過 page classification，將 SQLite pages 標記為 interior、leaf 或其他類型。  

這些分類結果用於 prefetch strategy 的 page selection。  

例如 `range_interior` 與 `offset_topk_interior` 只選取 interior pages；`residency_topk` 則可依 pages 過去 resident 的紀錄分別選取 interior 與 leaf pages。  

## 工作負載抽樣

workload 由專案中的 workload generator 產生，並以純文字檔交給 `benchmark_harness` 執行。  
每個 workload file 包含 1000 個 operations；正式實驗只使用 `read` 與 `scan`，不包含任何寫入操作。  
`read` operation 是 point lookup；`scan` operation 的 scan length 固定為 50。  
實際 benchmark query 以 `items.id` 為 lookup key。  
`read <id>` 對應：`SELECT payload FROM items WHERE id = ?1;`。  
`scan <id> 50` 對應：`SELECT payload FROM items WHERE id >= ?1 ORDER BY id LIMIT ?2;`。  
因此，本實驗主要量測 SQLite table B-tree / rowid access path 的 cold-cache 行為，而不是 `idx_items_k1` 或 `idx_items_k2` 兩個 secondary indexes。  

資料庫的合法 ID 範圍為 1 到 600,000。  

`full` workload 從完整 ID 範圍取樣；`tail` workload 只使用資料尾端 60,000 筆附近的區域。  

distribution 則分為 uniform 與 Zipfian，Zipf 參數為 `theta = 0.99`。  

因此正式實驗使用的八種 workload type 覆蓋了 operation type、key distribution 與 access range 三個維度：  

```text
read_zipf_full, read_zipf_tail,
read_uniform_full, read_uniform_tail,
scan_zipf_full, scan_zipf_tail,
scan_uniform_full, scan_uniform_tail
```

每個 workload type 都有獨立的 training workload pool 與 measurement workload pool。  

training workloads 用於建立 residency-based prefetch profile；measurement workloads 則用於正式量測。  
training profile 是由 training workloads 執行後觀察到的 page residency 統計，用來估計哪些 interior 或 leaf pages 較可能在後續查詢中被用到。  

兩者使用不同 index 範圍，避免用同一批 workload 同時訓練與評估。  

| Purpose     | Pool        | Sample count |
| ----------- | ----------- | -----------: |
| Training    | index 1–25  |            5 |
| Measurement | index 26–50 |            5 |

每個 measurement workload 重複執行 3 次。  

抽樣 seed 固定為 `20250620`，因此每次實驗都會使用相同的 training 與 measurement workload selection。  

這些 workload 不是從真實手機 App trace 擷取，而是用可控制的 synthetic workload 模擬常見 access pattern。  

這樣的好處是可以系統性比較 distribution、range 與 scan/read 型態；限制是結果代表受控 benchmark，而不是特定真實應用的完整行為。  

本實驗刻意不納入 write workloads。  

原因是本研究要隔離的核心問題是：當 SQLite database 的 file-backed pages 被回收後，read-only query 是否能透過 layout 與 prefetch 降低 refault latency。  

寫入負載會額外引入 WAL 或 rollback journal、dirty-page writeback、index update、page split，以及 layout 隨時間被破壞等因素。  

這些因素很重要，但會把研究問題從 cold-cache read latency 擴展到 layout maintenance 與 write amplification。  

因此，本報告先把 write workload 視為未來工作，而不是混入本次實驗矩陣。  

## 預取策略

baseline 不執行任何 prefetch，用作比較基準。  

其餘策略會在 page cache 被清除後、measurement workload 執行前，選出一組 SQLite pages 進行 prefetch。  

| Strategy | Description | Variants |
| --- | --- | ---: |
| `baseline` | no prefetch | 1 |
| `range_interior` | prefetch all interior pages | 1 |
| `offset_topk_interior` | prefetch the first N interior pages by file offset; `N ∈ {1, 5, 10, 20, 40, 60, 80}` | 7 |
| `residency_topk` | use training profiles to select top-K interior and leaf pages; `interior_k ∈ {0, 5, 20, 80}`, `leaf_k ∈ {0, 5, 20, 80}`, excluding 0/0 baseline | 15 |

`range_interior` 會選取全部 interior pages，依 file offset 排序，並將相鄰 SQLite pages 合併成 contiguous byte ranges 後再發出 I/O。  
`madvise` backend 會對合併後的 ranges 發出 `MADV_WILLNEED`；`pread` backend 則會以 buffered `pread()` 讀取相同 ranges，必要時再依 `pread_chunk_bytes` 分段。  

因此共有 24 個 strategy variants，其中 1 個是 baseline，23 個是非 baseline prefetch variants。  
這裡的 strategy variant 指的是同一種 prefetch strategy 在不同參數下形成的具體設定，例如 `offset_topk_interior` 的不同 `N`，或 `residency_topk` 的不同 `interior_k` 與 `leaf_k` 組合。  

每個非 baseline variant 會分別使用 `madvise` 與 `pread` backend 執行。  

`offset_topk_interior` 與 `residency_topk` 的參數採用 coarse-grained sweep，而不是窮舉所有可能的 `n`、`interior_k` 與 `leaf_k`。  

這是因為實驗已經包含 33,840 個 measurement cells；若再對所有 k 值做 dense sweep，實驗會變成參數調校工作，而不是比較 layout 與 prefetch family 的主要效果。  

目前選用的數值涵蓋小、中、大三種 prefetch budget：`1` 或 `5` 代表極小預取量，`20` 代表中等預取量，`80` 則接近涵蓋大部分 interior pages 的設定。  

因此，這組 sweep 的目的不是找到全域最佳 k，而是觀察不同 prefetch family 在合理 budget 下是否有穩定趨勢。  

`madvise` backend 對 selected pages 對應的 file ranges 送出 `MADV_WILLNEED` advice；`pread` backend 則用 buffered `pread()` 同步讀取 selected pages。  

兩者的成本語意不同：`madvise` 主要反映 request submission time，而 `pread` 反映同步讀取完成時間。  

因此實驗同時記錄 prefetch elapsed time，並在 derived metrics 中把 prefetch cost 納入。  

## 量測單位

實驗的最小量測單位稱為 cell。  

一個 cell 對應下列組合：  

```text
layout
× workload type
× memory condition
× measurement workload file
× repetition
× backend
× strategy
```

baseline cell 沒有 backend；非 baseline cell 則包含一個 backend 與一個具體 strategy variant。  

每個 `layout × workload type × memory condition × measurement workload × repetition` 會執行：  

```text
1 baseline + (23 non-baseline variants × 2 backends) = 47 cells
```

因此實驗的 measurement cell 數為：  

```text
3 layouts
× 8 workload types
× 2 memory conditions
× 5 measurement workloads
× 3 repetitions
× 47 cells
= 33,840 measurement cells
```

training profile run 數為：  

```text
3 layouts
× 8 workload types
× 2 memory conditions
× 5 training workloads
= 240 training runs
```

## 自動化執行流程

實驗由 `orchestrator.py` 自動展開與執行。  
orchestrator 的角色是把實驗設定轉換成 deterministic execution plan，確保每個 cell 的輸入、執行順序、輸出與後處理規則一致。  
實驗使用 deterministic execution order，而不是 randomized order。  
這讓中斷後續跑、artifact 對照與 cell provenance 較容易驗證；代價是長時間實驗仍可能受到背景負載、thermal state 或時間趨勢影響。  
因此，執行順序本身也列為本實驗的限制之一。  

每次實驗的流程如下：  

1. 驗證 database layout artifacts、workload files 與工具版本。  
2. 根據固定 seed 抽樣 training 與 measurement workloads。  
3. 為需要 profile 的 `residency_topk` strategy 建立 training profiles。  
4. 展開所有 measurement cells。  
5. 對每個 cell 清除 page cache。  
6. 對非 baseline cell 執行 prefetch。  
7. 執行 measurement workload 並記錄 latency、page faults 與 residency。  
8. 彙整 raw results，產生 summary、plots 與 report tables。  

所有 completed cells 都包含完整 provenance，因此結果可追溯到 layout、workload、memory condition、strategy、backend、repetition 與工具版本。  

## 冷快取流程

每個 measurement cell 都使用相同 cold-cache procedure：  

1. benchmark harness 開啟 SQLite database；  
2. 初始化 schema；  
3. 呼叫 drop-cache helper 清除 Linux page cache；  
4. 如果是非 baseline cell，執行 prefetch；  
5. 執行 measurement workload；  
6. 記錄 first-query latency、average latency、page faults、operation count 與 page residency。  

SQLite connection 與 schema initialization 都在 drop-cache 前完成。  

這是為了模擬應用程式已在執行，但 database file-backed pages 被 OS 回收的情境。  

cache 清除後不額外使用其他 cold advice，而是依賴系統提供的 drop-cache helper 建立 cold-cache condition。  

正式實驗使用的 drop-cache helper 對應 Linux 的 `sync && echo 3 > /proc/sys/vm/drop_caches`。  
benchmark harness 會在 drop-cache 前後使用 residency observation 記錄 SQLite pages 的 resident 狀態，因此可以檢查 page cache 是否確實被清除。  
這個方法能控制 Linux page cache 狀態，但不等同於完整模擬 Android LMK、Activity lifecycle 或實機 storage 行為。  

`MemoryMax=20MiB` 的 memory condition 透過 `systemd-run --user --scope` 建立 cgroup scope，並以 `MemoryMax` property 套用限制。  
experiment manifest 與 raw results 會記錄 `memory_limit_enabled` 與 `memory_max_bytes`，可確認該 cell 是在 memory-limited condition 下執行。  
不過，本次輸出資料沒有逐 cell 記錄 cgroup `memory.events` 或 `memory.current`，因此只能證明實驗是在 MemoryMax scope 下執行，不能直接證明每個 cell 都實際觸發 cgroup reclaim。  
memory condition 對 latency、page faults 與 residency 的影響可作為間接 evidence，但不應被寫成 direct reclaim proof。  

## 量測指標

benchmark harness 直接量測：  

- first-query latency；  
- average-query latency；  
- operation count；  
- major and minor page faults；  
- SQLite page residency before and after the cold-cache step；  
- prefetch elapsed time；  
- selected page residency ratio。  

由於 prefetch 本身也需要時間，實驗另外計算兩個 effective metrics：  

```text
effective_first_query_latency_us =
    prefetch_elapsed_us + first_query_latency_us
effective_average_query_latency_us =
    average_latency_us + prefetch_elapsed_us / ops
```

baseline 沒有 prefetch cost，因此 baseline 的 effective metrics 分別等於原始 first-query latency 與 average-query latency。  

改善幅度使用 paired baseline 計算：  

```text
improvement_percent =
    (baseline_latency - candidate_latency)
    / baseline_latency × 100
```

正值代表 candidate 比 baseline 快；負值代表 candidate 較慢。  

## 統計顯著性

實驗使用 paired design。  

每個 candidate cell 都與相同 measurement workload file、相同 repetition、相同 workload type 與 memory condition 下的 baseline 配對。  

這樣可以減少不同 workload file 本身難度差異造成的干擾。  

統計分析使用：  

- paired median difference；  
- bootstrap 95% confidence interval；  
- exact sign-test p-value；  
- Benjamini-Hochberg FDR correction。  

顯著效果的判定條件為：  

```text
FDR q-value <= 0.05
and bootstrap 95% CI does not cross 0
```

在報告中，主要關注每個：  

```text
workload type × memory condition × effective metric
```

下 median latency 最低的 layout/backend/strategy 組合，並標示該組合相對「原始 layout 且不做 prefetch 的 baseline」是否達到統計顯著。  

需要注意的是，這裡的顯著性主要用來判斷在本次 sampled workloads 中效果是否穩定，不應解讀為對所有可能 workloads 的保證。  
measurement set 包含 5 個 workload files，每個重複 3 次；repetition 有助於降低量測雜訊，但不等同於 15 個完全獨立的 workload populations。  
sign test 的 paired observations 是 `measurement workload file × repetition`，因此每個完整比較最多有 15 pairs。  
在 15 pairs 全部同方向時，two-sided exact sign test 的 p-value 約為 `2 / 2^15 = 0.000061`；經 Benjamini-Hochberg correction 與報告格式化後，q-value 可能顯示為 `0.0001`。  
因此，小 q-value 代表在這 15 個 paired observations 中方向非常一致，但仍需搭配前述 workload-level independence caveat 解讀。  
另外，最佳組合是從大量 candidate configurations 中挑出後再檢查顯著性，因此這些統計結果應視為本實驗資料中的穩定性證據，而不是嚴格的 post-selection confirmatory inference。  

## 結果彙整方式

實驗結果主要以 effective metrics 解讀。  

對每個：  

```text
workload type × memory condition × effective metric
```

報告選出 median latency 最低的 layout/backend/strategy 組合，並檢查該組合相對「原始 layout 且不做 prefetch 的 baseline」是否達到統計顯著。  
這個設計的目的在於回答「在納入 prefetch cost 後，哪個完整組合表現最好」。  
因此，最佳組合表應被解讀為相對 `original / baseline` 的 end-to-end comparison。  
表中的 `Exploratory significance` 欄位也是針對 `original / baseline`，不是 same-layout baseline。  
它能指出每個 workload 與 memory condition 下表現最好的完整 configuration，但不單獨分離 layout、backend 與 strategy 各自的因果貢獻。  
為了檢查 layout 與 prefetch 是否各自仍有訊號，本報告另外做 layout-only 與 same-layout prefetch 補充分析。  
這兩個分析不取代主要 end-to-end result，而是用來輔助解讀完整 configuration 的改善來源。  

## 可重現性

可重現性由三個層次保證。  

第一，實驗使用固定 config 與固定 workload sampling seed，因此 layout、workload selection、memory conditions、strategies 與 repetitions 都是 deterministic 的。  

第二，實驗執行期間，每個 cell 都會記錄 provenance，包括 layout、workload、memory condition、strategy、backend、repetition 與工具版本。  

這些 provenance 在本次分析中用於確認結果可追溯到具體實驗條件。  

第三，summary statistics、significance analysis 與 figures 都由專案中的分析程式自動產生，避免手動彙整造成不一致。  

實驗完成後，33,840 個 measurement cells 全部完成，沒有 failed、timeout 或 invalid cells。  

隨報告提供的 GitHub repository 包含 source code、實驗 config、實驗與製圖程式，以及本報告使用的摘要結果與圖表。  

完整 per-cell artifacts（例如每個 cell 的 operation log、run record 與其他中間檔）因資料量較大，未直接納入 GitHub repository。  

這些 per-cell artifacts 在實驗期間用於驗證與除錯，而報告中的結果則由 GitHub repository 中的程式、config 與摘要資料產生。  

# 實驗結果

## 實驗摘要

| 項目                      | 值                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Prefetch backends       | madvise → pread                                                                                                                            |
| Enabled layouts         | original, vacuum, rewrite                                                                                                                  |
| Workload types          | read_zipf_full, read_zipf_tail, read_uniform_full, read_uniform_tail, scan_zipf_full, scan_zipf_tail, scan_uniform_full, scan_uniform_tail |
| Training file count     | 5                                                                                                                                          |
| Measurement file count  | 5                                                                                                                                          |
| Measurement repetitions | 3                                                                                                                                          |
| Memory conditions       | unlimited (unlimited) → 20m (enabled, MemoryMax=20MiB)                                                                                     |
| SQLite page size        | original=4096, rewrite=4096, vacuum=4096                                                                                                   |

## 執行環境

| 項目                | 值                                                                                                                                                    |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Linux kernel      | 6.17.0-19-generic                                                                                                                                    |
| Hostname          | meow1                                                                                                                                                |
| CPU model         | AMD Ryzen 9 9950X 16-Core Processor                                                                                                                  |
| Logical CPU count | 32                                                                                                                                                   |
| Total RAM         | 59.21 GiB                                                                                                                                            |
| Filesystem type   | xfs                                                                                                                                                  |
| Storage devices   | sda (3.6T, WUS721204BLE6L4 ), nvme2n1 (1.9T, KINGSTON SKC3000D2048G), nvme0n1 (1.9T, KINGSTON SKC3000D2048G), nvme1n1 (1.9T, KINGSTON SKC3000D2048G) |
| SQLite version    | 3.46.1                                                                                                                                               |

## 採用的預取策略

| Strategy | 展開數量 | 設定 |
| --- | --- | --- |
| baseline | 1 | 無prefetch；作為比較基準 |
| range_interior | 1 | 所有interior pages |
| offset_topk_interior | 7 | n=1, 5, 10, 20, 40, 60, 80 |
| residency_topk | 15 | interior_k={0, 5, 20, 80}; leaf_k={0, 5, 20, 80}; 排除0/0 baseline |

## 最佳配置組合（含顯著性）

每列對應一個 `workload type × memory condition × metric`，列出median latency最低的組合。  

表格依workload type、memory condition、metric排序。  

若最佳組合是`original / baseline`，顯著性以`—`表示。  

| Workload type     | Memory condition | Metric                             | Best layout | Best backend | Best strategy                | Best median | Best P25–P75        | Improvement | Exploratory significance |
| ----------------- | ---------------- | ---------------------------------- | ----------- | ------------------------ | ---------------------------- | ----------- | ------------------- | ----------- | ------------------------ |
| read_zipf_full    | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i80_l80 | 34.23 µs    | 33.47 µs–35.23 µs   | 14.83%      | yes          |
| read_zipf_full    | unlimited        | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i5_l5   | 29.63 µs    | 27.48 µs–56.35 µs   | 69.20%      | yes          |
| read_zipf_full    | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i80_l80 | 38.68 µs    | 37.48 µs–39.94 µs   | 23.59%      | yes          |
| read_zipf_full    | 20m              | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i5_l0   | 29.53 µs    | 23.10 µs–38.63 µs   | 71.16%      | yes          |
| read_zipf_tail    | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i20_l80 | 12.18 µs    | 11.95 µs–12.71 µs   | 2.91%       | yes          |
| read_zipf_tail    | unlimited        | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i20_l0  | 218.74 µs   | 212.39 µs–227.24 µs | 43.15%      | yes          |
| read_zipf_tail    | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i20_l80 | 12.09 µs    | 11.91 µs–12.94 µs   | 1.99%       | yes          |
| read_zipf_tail    | 20m              | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i20_l0  | 224.03 µs   | 219.83 µs–227.69 µs | 42.15%      | yes          |
| read_uniform_full | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | range_interior               | 89.02 µs    | 87.02 µs–90.45 µs   | 2.10%       | yes          |
| read_uniform_full | unlimited        | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i80_l0  | 257.34 µs   | 254.97 µs–274.09 µs | 28.72%      | yes          |
| read_uniform_full | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i80_l80 | 145.22 µs   | 144.38 µs–148.19 µs | 13.61%      | yes          |
| read_uniform_full | 20m              | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i80_l5  | 271.16 µs   | 263.82 µs–281.22 µs | 25.21%      | yes          |
| read_uniform_tail | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i0_l80  | 14.49 µs    | 14.37 µs–14.93 µs   | 10.74%      | yes          |
| read_uniform_tail | unlimited        | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i5_l5   | 204.63 µs   | 197.85 µs–361.15 µs | 34.85%      | no           |
| read_uniform_tail | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i20_l80 | 14.44 µs    | 14.20 µs–14.79 µs   | 2.94%       | yes          |
| read_uniform_tail | 20m              | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i5_l5   | 216.30 µs   | 209.83 µs–368.64 µs | 26.83%      | no           |
| scan_zipf_full    | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | range_interior               | 38.19 µs    | 36.91 µs–41.39 µs   | 12.65%      | yes          |
| scan_zipf_full    | unlimited        | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i5_l0   | 207.25 µs   | 197.56 µs–253.29 µs | 9.74%       | yes          |
| scan_zipf_full    | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i80_l0  | 44.44 µs    | 40.90 µs–46.98 µs   | 17.51%      | yes          |
| scan_zipf_full    | 20m              | effective_first_query_latency_us   | rewrite     | madvise      | offset_topk_interior_n5      | 213.43 µs   | 203.90 µs–243.63 µs | 14.63%      | no           |
| scan_zipf_tail    | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i0_l80  | 15.09 µs    | 14.79 µs–15.33 µs   | 2.17%       | yes          |
| scan_zipf_tail    | unlimited        | effective_first_query_latency_us   | original    | madvise      | residency_topk_sweep_i20_l5  | 215.68 µs   | 211.09 µs–231.94 µs | 43.12%      | yes          |
| scan_zipf_tail    | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i5_l80  | 15.15 µs    | 14.69 µs–15.39 µs   | 3.47%       | yes          |
| scan_zipf_tail    | 20m              | effective_first_query_latency_us   | vacuum      | madvise      | residency_topk_sweep_i20_l0  | 233.83 µs   | 228.39 µs–244.32 µs | 41.61%      | yes          |
| scan_uniform_full | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | range_interior               | 93.32 µs    | 92.33 µs–94.24 µs   | 2.37%       | yes          |
| scan_uniform_full | unlimited        | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i80_l0  | 272.27 µs   | 263.79 µs–300.94 µs | 31.58%      | no           |
| scan_uniform_full | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i80_l5  | 153.62 µs   | 151.53 µs–155.38 µs | 10.29%      | yes          |
| scan_uniform_full | 20m              | effective_first_query_latency_us   | rewrite     | madvise      | residency_topk_sweep_i80_l5  | 280.79 µs   | 271.89 µs–310.14 µs | 31.53%      | no           |
| scan_uniform_tail | unlimited        | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i0_l80  | 17.07 µs    | 16.73 µs–17.70 µs   | 1.85%       | yes          |
| scan_uniform_tail | unlimited        | effective_first_query_latency_us   | original    | madvise      | residency_topk_sweep_i20_l0  | 227.77 µs   | 220.99 µs–244.27 µs | 41.97%      | yes          |
| scan_uniform_tail | 20m              | effective_average_query_latency_us | vacuum      | madvise      | residency_topk_sweep_i5_l80  | 17.00 µs    | 16.63 µs–17.19 µs   | 3.31%       | yes          |
| scan_uniform_tail | 20m              | effective_first_query_latency_us   | original    | madvise      | residency_topk_sweep_i20_l0  | 240.17 µs   | 228.35 µs–252.66 µs | 42.71%      | yes          |

## 統計顯著性摘要

前一節的最佳組合表已經在每個 `workload type × memory condition × metric` 組合中標示該組合是否相對 `original / baseline` 達到統計顯著。  

因此，這裡不再重複列出完整的顯著性明細表。  

整體來看，多數最佳組合達到統計顯著，表示這些改善在本次 sampled workloads 中相對穩定。  
未達顯著的案例主要集中在 effective first-query latency；這些組合雖然 median latency 最低，但 sampled workloads 之間的變異較大，因此在最佳組合表中仍保留 `Exploratory significance = no`，避免把探索性結果寫成穩定結論。  

統計顯著性在本報告中主要作為穩定性指標。  

由於最佳組合是從多個 candidate configurations 中挑選出來，這些結果應解讀為本實驗矩陣中的探索性穩定性證據，而不是嚴格的 post-selection confirmatory inference。  

## 資料排列與預取補充分析

主要結果是以完整 configuration 相對原始 layout 且不做 prefetch 的 baseline 進行 end-to-end 比較。  
為了確認改善是否只來自 layout，或 prefetch 在固定 layout 後仍有幫助，我另外從同一份 `all_raw.csv` 產生兩種配對比較。  

第一種是只看資料排列的比較。  
這個比較只使用 no-prefetch baseline cells，比較 `vacuum` 或 `rewrite` 相對 `original` 的差異。  
第二種是固定同一種 layout 後的預取比較。  
這個比較把每個 prefetch candidate 與相同 layout 下的 no-prefetch baseline 配對。  
兩者都使用相同的 effective metrics、bootstrap 95% CI、sign test 與 FDR correction。  

| 比較類型 | 比較列數 | 顯著列數 | 顯著且改善的列數 |
| --- | ---: | ---: | ---: |
| 只看資料排列 | 64 | 24 | 22 |
| 固定 layout 後比較預取 | 4416 | 2467 | 996 |

只看資料排列的比較顯示，單純改變 database layout 在 average-query latency 上確實有穩定訊號。  
在顯著改善的 rows 中，`vacuum` 有 14 rows，`rewrite` 有 8 rows，且全部出現在 `effective_average_query_latency_us`。  
這表示 layout 本身主要影響的是 1000 個 operations 攤開後的平均表現，而不是第一筆 query 的延遲。  

固定同一種 layout 後的預取比較顯示，即使固定 layout，prefetch 仍然經常帶來改善。  
在顯著改善的 rows 中，`madvise` 搭配 `residency_topk` 最常出現，effective first-query latency 有 299 rows，effective average-query latency 有 254 rows。  
`offset_topk_interior` 也有穩定改善，但數量較少；`range_interior` 的改善數量更少，表示全抓 interior pages 不一定比有選擇的策略更好。  
`pread` 也有顯著改善案例，但數量明顯少於 `madvise`。  

這個補充分析讓主要結果的解讀更清楚。  
`vacuum` 經常成為 average-query latency 的最佳 layout，並不只是 prefetch 造成的假象，因為 no-prefetch 的 layout-only comparison 也看到穩定改善。  
另一方面，first-query latency 的大幅改善主要仍來自 prefetch；固定 layout 後的比較中，最佳改善多集中在 `madvise` 與 small-budget 或 residency-based prefetch。  
因此，本實驗的結果比較適合解讀為 layout 與 prefetch 共同形成的 end-to-end 改善，而不是單一因素獨自造成全部效果。  

## 核心有效指標圖

下列圖直接使用已納入 prefetch cost 的 effective 指標。  

每個格子代表一個 workload type 與 memory condition ，文字標出該格 median latency 最低的 layout/backend/strategy 組合；顏色代表相對 paired baseline 的 improvement 。  

![Best effective average-query latency](plots/best_effective_average_heatmap.png)

![Best effective first-query latency](plots/best_effective_first_heatmap.png)

## 機制合理性檢查

benchmark harness 也會記錄每個 cell 的 major 與 minor page faults。  
per-operation 的 fault delta 會寫入該 cell 的 `operations.csv`，欄位為 `majflt_delta` 與 `minflt_delta`。  
cell log 中也會輸出整個 workload 的 `total_majflt` 與 `total_minflt`。  
orchestrator 進一步把它們彙整成 `results/all_raw.csv` 中的 `major_page_faults`、`minor_page_faults` 與 `resident_after_cold_pages` 欄位。  
在這裡，baseline cells 的 `resident_after_cold_pages` 是 drop-cache 後、workload 前的 residency；prefetch cells 的同一欄位則是 drop-cache 且 prefetch 後、執行 workload 前的 residency。  

下表是從 `results/all_raw.csv` 產生的 compact sanity check。  
這張表的 `all completed` rows 混合了不同 layouts、workloads、strategies 與 backends，因此只用來確認 cold-cache refault activity，不應解讀為 baseline 與 prefetch strategy 之間的 controlled causal comparison。  
`20m` condition 確認是以 `MemoryMax=20971520` bytes 執行。  
兩個 memory conditions 在 cold-cache 後的 baseline resident pages 中位數皆為 16 pages，表示 drop-cache 後 database pages 大多已不 resident。  
同時，completed cells 的 major faults 中位數約為 148–152，表示 benchmark 確實量到 page refault activity，而不是只量到 CPU-side overhead。  

| Group | Memory | Cells | Median major faults | Median minor faults | Median resident after cold | Median effective first query | Median effective average |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| all completed | unlimited | 16920 | 147.50 | 108 | 42 | 424.39 µs | 32.10 µs |
| all completed | 20m | 16920 | 152 | 111 | 42 | 429.20 µs | 35.71 µs |
| baseline only | unlimited | 360 | 160.50 | 102 | 16 | 385.59 µs | 29.47 µs |
| baseline only | 20m | 360 | 169 | 108 | 16 | 389.47 µs | 31.21 µs |

這些數字支持本實驗確實在 cold-cache refault 情境下執行。  
不過，本報告沒有進一步把 page faults attribution 拆到 interior、leaf 或 overflow pages，因此機制解釋仍保守地視為 sanity check，而不是完整因果證明。  
此外，`20m` condition 可確認是在 MemoryMax scope 下執行，但本次輸出資料沒有逐 cell 記錄 cgroup `memory.events`，所以這些數字不能直接證明每個 cell 都觸發 cgroup reclaim。  

## 結果解讀

對 RQ1，本實驗結果顯示，多數 workload type 與 memory condition 下，都存在至少一個完整 layout–prefetch configuration 能在納入 prefetch cost 後優於原始 layout 且不做 prefetch 的 baseline。  
最佳組合整理於前一節表格。  

對 RQ2，最佳 configuration 會隨 metric 與 workload type 改變。  
first-query latency 的最佳組合較常包含 aggressive 或 residency-based prefetch；average-query latency 的改善較小，且 `vacuum` layout 較常成為最佳。  
`MemoryMax=20MiB` 改變 latency 水準，但沒有使最佳 strategy family 完全翻轉。  

對 RQ3，最佳組合呈現三個主要模式：`madvise` backend 最常出現在最佳組合中；`residency_topk` 與 `range_interior` 是主要有效的 prefetch family；`vacuum` 與 `rewrite` layout 分別較常對 average-query 與 first-query latency 有利。  
這些結果應解讀為本實驗矩陣中的 end-to-end winner pattern，而不是單一 component 的因果估計。  

整體而言，結果支持「在本 Linux cold-cache benchmark 中，若干完整 layout–prefetch configurations 相對 `original / baseline` 呈現較低 latency」，但改善型態依 metric 不同而有差異。  

第一，first-query latency 的改善通常比 average-query latency 更明顯。  

這與實驗設計的預期一致：完整 configuration 若能在第一筆 query 前先恢復一部分重要 pages，就可能減少第一筆 query 遇到的 serial page faults。  

相對地，average-query latency 把 1000 個 operations 全部納入後，單次 prefetch 對整體平均的影響會被攤薄，因此 improvement 通常較小。  

第二，`madvise` 在最佳組合中幾乎全面勝出。  

這不代表 `madvise` 在所有系統上都必然優於 `pread`，而是表示在本環境中，`MADV_WILLNEED` 的 submission cost 較低，且 kernel 可在 query 前後自行安排讀取；相較之下，`pread` 是同步讀取，prefetch elapsed time 更容易直接反映到 effective latency。  

也因此，本報告把 prefetch cost 納入 effective metrics，而不是只比較 query latency。  

第三，`vacuum` 經常是 effective average-query latency 的最佳 layout，而 `rewrite` 在多個 full-range workload 的 effective first-query latency 上較常出現。  

這表示出現在最佳組合中的 layout 可能對 file locality 或 early B-tree page placement 有幫助；但此處仍是從 end-to-end winner pattern 做出的解讀，不是 layout-only causal estimate。  

不過，目前報告沒有進一步拆解 fault page type，因此這個機制解釋應視為合理推論，而不是已被直接證明的因果結論。  

第四，`tail` workloads 的 average-query improvement 通常較小，但 first-query improvement 仍可能很大。  

這可能是因為 tail workload 的 key range 較集中，baseline 在 1000 個 operations 內較快累積 locality；但第一筆 query 仍然要從 cold-cache 狀態開始，因此 prefetch 對 first query 仍有明顯空間。  

第五，`MemoryMax=20MiB` 沒有完全改變最佳策略的型態。  

多數最佳組合仍使用 `madvise` 與 residency-based strategy。  

這表示在本 benchmark 中，memory condition 會改變 latency 水準，但沒有讓最佳完整 configurations 的型態完全翻轉。  

不過，20MiB 是單一壓力設定，不能代表所有 Android 或 embedded memory pressure 條件。  

## 效度威脅

本實驗有幾個限制。  

首先，實驗在 Linux workstation、XFS 與高速 NVMe 上執行，並不直接等同於 Android 手機上的 flash storage、LMK policy 與 app lifecycle。  

其次，workloads 是 synthetic workloads，能控制 access pattern，但不保證完全代表真實 App trace。  

第三，本實驗聚焦 read-only cold-cache behavior，沒有納入任何 write workload；因此結果不應外推到頻繁更新資料庫、WAL writeback 或 layout churn 明顯的情境。  

第四，prefetch 參數使用 coarse-grained sweep，能比較策略家族的趨勢，但不能保證找到每個 workload 的最佳 `n` 或 `k`。  

第五，顯著性分析使用 sampled workloads 與 repetitions；repetition 能降低量測雜訊，但不應被視為完全獨立 workload population。  

第六，最佳組合由大量 candidate configurations 中選出，可能存在 winner's curse，因此結果應以「本實驗矩陣中的最佳觀察組合」解讀。  
第七，實驗使用 deterministic execution order，而不是 randomized order；這有助於 provenance 檢查與中斷後續跑，但可能保留長時間實驗中的時間趨勢影響。  

最後，雖然 benchmark 有記錄 page fault 與 residency，本報告主要仍以 latency 指標解讀；若要更強地證明 B+tree-aware prefetch 的機制，未來應加入 root-only、random-page、oracle prefetch 或 fault-page-type analysis 等控制組。  

# GitHub 儲存庫連結
