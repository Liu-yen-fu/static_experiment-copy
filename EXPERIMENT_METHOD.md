# Method and Experimental Design

本實驗評估靜態 SQLite database layout 與 page prefetch 策略，是否能改善 cold-cache 條件下的查詢延遲。實驗模擬的情境是：應用程式已經建立 SQLite connection 與 prepared statements，但 database file 對應的 Linux page cache 已被清除；在第一筆 query 執行前，系統可以選擇性地根據 database layout 或 training profile 預先載入部分 SQLite pages。

此設定對應到真實系統中常見的冷啟動或記憶體壓力情境：process 本身仍然存在，但 file-backed pages 可能因系統記憶體壓力而被回收。實驗的核心問題是：在這種情境下，靜態 layout 調整與 prefetch 是否能有效降低第一筆查詢與整體 workload 的延遲；同時，prefetch 自身的成本是否會抵消其收益。

## Experimental Factors

本實驗比較五個主要因素：

| Factor | Values |
| --- | --- |
| Database layout | `original`, `vacuum`, `rewrite` |
| Workload type | `read_zipf_full`, `read_zipf_tail`, `read_uniform_full`, `read_uniform_tail`, `scan_zipf_full`, `scan_zipf_tail`, `scan_uniform_full`, `scan_uniform_tail` |
| Memory condition | `unlimited`, `20m` |
| Prefetch backend | `madvise`, `pread` |
| Prefetch strategy | `baseline`, `range_interior`, `offset_topk_interior`, `residency_topk` |

`original` 是原始 SQLite database layout；`vacuum` 是透過 SQLite `VACUUM` 產生的 layout；`rewrite` 是由本專案 layout rewriter 產生的 layout。三種 layout 使用相同邏輯資料，但 database file 中 pages 的排列方式不同。

workload type 同時涵蓋 point-read 與 range-scan 類型，並比較 Zipfian 與 uniform key distribution。`full` 與 `tail` 代表不同查詢區域，使實驗能觀察資料分布與存取區域對 prefetch 效果的影響。

memory condition 包含無限制執行與 `MemoryMax=20MiB` 的受限執行。後者用來模擬較強記憶體壓力下 page cache 與 process memory 競爭的情境。

## Database Under Test

實驗使用同一份邏輯資料產生三個 SQLite database files，分別對應 `original`、`vacuum` 與 `rewrite` layout。三者的資料內容與查詢語意相同，差異只在 SQLite pages 於 database file 中的排列方式。這讓實驗可以把觀察到的差異主要歸因於 file layout 與 prefetch policy，而不是資料內容不同。

所有 layout 使用相同 SQLite page size：

```text
4096 bytes
```

在實驗前，每個 database file 都會先經過 page classification，將 SQLite pages 標記為 interior、leaf 或其他類型。這些分類結果用於 prefetch strategy 的 page selection。例如 `range_interior` 與 `offset_topk_interior` 只選取 interior pages；`residency_topk` 則可依 training profile 分別選取 interior 與 leaf pages。

`vacuum` layout 代表 SQLite 內建重整後的 file organization；`rewrite` layout 則代表實驗性 page reordering。兩者都與 `original` layout 保持相同查詢結果，因此 benchmark 可以在相同 workloads 下比較不同 layout 對 cold-cache latency 的影響。

## Workload Sampling

每個 workload type 都有獨立的 training workload pool 與 measurement workload pool。training workloads 用於建立 residency-based prefetch profile；measurement workloads 則用於正式量測。兩者使用不同 index 範圍，避免用同一批 workload 同時訓練與評估。

| Purpose | Pool | Sample count |
| --- | --- | ---: |
| Training | index 1–25 | 5 |
| Measurement | index 26–50 | 5 |

每個 measurement workload 重複執行 3 次。抽樣 seed 固定為 `20250620`，因此每次 formal experiment 都會使用相同的 training 與 measurement workload selection。

## Prefetch Strategies

baseline 不執行任何 prefetch，用作比較基準。其餘策略會在 page cache 被清除後、measurement workload 執行前，選出一組 SQLite pages 進行 prefetch。

| Strategy | Description | Variants |
| --- | --- | ---: |
| `baseline` | no prefetch | 1 |
| `range_interior` | prefetch all interior pages | 1 |
| `offset_topk_interior` | prefetch the first N interior pages by file offset; `N ∈ {1, 5, 10, 20, 40, 60, 80}` | 7 |
| `residency_topk` | use training profiles to select top-K interior and leaf pages; `interior_k ∈ {0, 5, 20, 80}`, `leaf_k ∈ {0, 5, 20, 80}`, excluding 0/0 baseline | 15 |

因此共有 24 個 strategy variants，其中 1 個是 baseline，23 個是非 baseline prefetch variants。每個非 baseline variant 會分別使用 `madvise` 與 `pread` backend 執行。

`madvise` backend 對 selected pages 對應的 file ranges 送出 `MADV_WILLNEED` advice；`pread` backend 則用 buffered `pread()` 同步讀取 selected pages。兩者的成本語意不同：`madvise` 主要反映 request submission time，而 `pread` 反映同步讀取完成時間。因此實驗同時記錄 prefetch elapsed time，並在 derived metrics 中把 prefetch cost 納入。

## Measurement Unit

實驗的最小量測單位稱為 cell。一個 cell 對應下列組合：

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

因此 formal experiment 的 measurement cell 數為：

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

## Automated Execution

實驗由 `orchestrator.py` 自動展開與執行。orchestrator 的角色是把 formal experiment 設定轉換成 deterministic execution plan，確保每個 cell 的輸入、執行順序、輸出與後處理規則一致。

每次 formal experiment 的流程如下：

1. 驗證 database layout artifacts、workload files 與工具版本。
2. 根據固定 seed 抽樣 training 與 measurement workloads。
3. 為需要 profile 的 `residency_topk` strategy 建立 training profiles。
4. 展開所有 measurement cells。
5. 對每個 cell 清除 page cache。
6. 對非 baseline cell 執行 prefetch。
7. 執行 measurement workload 並記錄 latency、page faults 與 residency。
8. 彙整 raw results，產生 summary、plots 與 report tables。

所有 completed cells 都包含完整 provenance，因此結果可追溯到 layout、workload、memory condition、strategy、backend、repetition 與工具版本。

## Cold-cache Procedure

每個 measurement cell 都使用相同 cold-cache procedure：

1. benchmark harness 開啟 SQLite database；
2. 初始化 schema；
3. 呼叫 drop-cache helper 清除 Linux page cache；
4. 如果是非 baseline cell，執行 prefetch；
5. 執行 measurement workload；
6. 記錄 first-query latency、average latency、page faults、operation count 與 page residency。

SQLite connection 與 schema initialization 都在 drop-cache 前完成。這是為了模擬應用程式已在執行，但 database file-backed pages 被 OS 回收的情境。cache 清除後不額外使用其他 cold advice，而是依賴系統提供的 drop-cache helper 建立 cold-cache condition。

## Metrics

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

## Statistical Significance

實驗使用 paired design。每個 candidate cell 都與相同 measurement workload file、相同 repetition、相同 workload type 與 memory condition 下的 baseline 配對。這樣可以減少不同 workload file 本身難度差異造成的干擾。

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

下 median latency 最低的 layout/backend/strategy 組合，並標示該組合相對 original baseline 是否達到統計顯著。

## Result Summarization

實驗結果主要以 effective metrics 解讀。對每個：

```text
workload type × memory condition × effective metric
```

報告選出 median latency 最低的 layout/backend/strategy 組合，並檢查該組合相對 original baseline 是否達到統計顯著。這個設計直接回答「在納入 prefetch cost 後，哪個組合最值得採用」。

正式報告的主要視覺化是 effective average-query 與 effective first-query heatmaps。每個格子對應一個 workload type 與 memory condition，格內標示該條件下的最佳組合，顏色表示相對 baseline 的 improvement。這比單純比較 prefetch cost 與 first-query improvement 更接近本實驗的主要決策問題。

其他診斷性圖表，例如 prefetch elapsed time 與 first-query improvement 的 trade-off scatter plots，保留於 repository 中作為補充檢查工具，不作為正式報告主文的主要結論依據。

## Reproducibility

可重現性由三個層次保證。

第一，formal experiment 使用固定 config 與固定 workload sampling seed，因此 layout、workload selection、memory conditions、strategies 與 repetitions 都是 deterministic 的。

第二，實驗執行期間，每個 cell 都會記錄 provenance，包括 layout、workload、memory condition、strategy、backend、repetition 與工具版本。這些 provenance 在本次分析中用於確認結果可追溯到具體實驗條件。

第三，summary statistics、significance analysis 與 figures 都由 scripts 自動產生，避免手動彙整造成不一致。實驗完成後，33,840 個 measurement cells 全部完成，沒有 failed、timeout 或 invalid cells。

隨報告提供的 GitHub repository 包含 source code、formal experiment config、實驗與製圖 scripts，以及本報告使用的摘要結果與圖表。完整 per-cell artifacts（例如每個 cell 的 operation log、run record 與其他中間檔）因資料量較大，未直接納入 repository；這些 artifacts 在實驗期間用於驗證與除錯，而報告中的結果則由 repository 中的 scripts、config 與摘要資料產生。
