# Static SQLite Layout 與 Prefetch 實驗

本專案比較不同 SQLite database layout 與 prefetch 策略對 cold-start query latency 的影響。

實驗模擬下列情境：應用程式仍在執行，SQLite connection 與 prepared statements 已建立，但 database 的 file-backed pages 已從 Linux page cache 回收。每個 measurement cell 都會重新清除 page cache，在第一筆 query 前選擇性執行 `madvise(MADV_WILLNEED)` 或 buffered `pread()` prefetch。

完整規格與 artifact schema 請參閱 [DESIGN_REVISED.md](DESIGN_REVISED.md)。`DESIGN.md`保留為原始版本，不代表目前實作契約。

## 前置條件

實驗必須在 Linux 執行，並需要：

- GCC、GNU Make 與 Python 3。
- Python 3 的標準 `sqlite3` module。
- 一支由執行環境提供、可非互動呼叫且符合下述契約的drop-cache helper；它可以是本機安裝的root-owned副本，也可以是工作站管理者提供的另一支helper。
- 若啟用 memory limit，系統必須提供可用的 `systemd --user` session，以及 `systemd-run`、`systemctl`。
- Trade-off圖建議安裝Python `matplotlib`以取得完整座標、圖例與並排子圖；未安裝時會依序使用Pillow或內建最小PNG fallback。
- 專案已附帶 canonical SQLite database：`data/source/database.db`。

Drop-cache動作最終需要足夠權限，但執行實驗的帳號本身不必擁有root權限。Helper必須不接受參數、可非互動執行、成功時exit 0，並完成等效於`sync`後將`3`寫入`/proc/sys/vm/drop_caches`的動作。

若使用者可請管理者安裝本專案helper，請勿直接對使用者可修改的專案內script設定`NOPASSWD`；應安裝一份root-owned副本：

```bash
sudo install -o root -g root -m 0755 \
  tools/bin/drop_caches.sh \
  /usr/local/sbin/static-experiment-drop-caches
```

使用`whoami`確認帳號名稱，接著執行：

```bash
sudo visudo -f /etc/sudoers.d/static-experiment-drop-caches
```

加入下列規則，將`<user>`替換為實際帳號：

```sudoers
<user> ALL=(root) NOPASSWD: /usr/local/sbin/static-experiment-drop-caches
```

驗證sudoers與helper：

```bash
sudo chmod 0440 /etc/sudoers.d/static-experiment-drop-caches
sudo visudo -c
sudo -k
sudo -n /usr/local/sbin/static-experiment-drop-caches
echo $?
```

最後一行必須輸出`0`。Config中的`paths.drop_caches_script`也必須改為：

```json
"drop_caches_script": "/usr/local/sbin/static-experiment-drop-caches"
```

### 無root權限工作站

若工作站已提供另一支drop-cache helper，不需要安裝或複製`tools/bin/drop_caches.sh`，也不需要取得root shell。請向工作站管理者確認helper的絕對路徑、呼叫方式與授權範圍，然後：

1. 將`paths.drop_caches_script`設為工作站helper的絕對路徑。
2. 若規定透過passwordless `sudo -n`呼叫，將`cold_protocol.drop_caches_use_sudo`設為`true`；若目前帳號可直接執行，設為`false`。
3. 以管理者指定的方式先做一次非互動probe，確認helper exit 0。不要假設工作站helper與`/usr/local/sbin/static-experiment-drop-caches`同名或位於相同路徑。

例如工作站提供`/opt/workstation/bin/drop-page-cache`且允許直接執行：

```json
{
  "paths": {
    "drop_caches_script": "/opt/workstation/bin/drop-page-cache"
  },
  "cold_protocol": {
    "cold_advice": "none",
    "sqlite_open_timing": "before-cold",
    "schema_init_timing": "before-cold",
    "drop_caches_use_sudo": false
  }
}
```

Helper路徑及`drop_caches_use_sudo`都是config與cell identity的一部分。移到另一台工作站時應在第一次執行前調整config並使用對應的experiment ID；不要拿不同內容的config對既有experiment目錄做resume。

## 重複實驗

下列指令皆從 repository 根目錄開始。

### 1. Build 所有工具

```bash
cd static_experiment
make
```

確認以下 executable 已產生：

```text
tools/bin/benchmark_harness
tools/bin/classify_pages
tools/bin/residency_checker
tools/bin/layout_rewriter
tools/bin/prefetch_runner
tools/bin/drop_caches.sh
```

### 2. Provision 三種 database layout

使用專案附帶的 canonical SQLite database：

```bash
python3 tools/src/build_layouts.py \
  --source data/source/database.db \
  --output-dir data/layouts
```

此步驟會各自從同一份 source 建立：

```text
data/layouts/original/database.db
data/layouts/vacuum/database.db
data/layouts/rewrite/database.db
```

每個 layout 亦會產生 `classify.csv` 與 `metadata.json`；rewrite layout 另有 `fix.sql`。Provisioning 工具不會覆寫既有 layout 目錄。若要重新 provisioning，請先自行保存或移除舊的 `data/layouts/` artifacts。

### 3. 確認 smoke experiment 設定

Smoke experiment 使用：

- `original` layout。
- `read_zipf_full` workload type。
- 5 份 training workloads。
- 5 份 measurement workloads。
- 每份 measurement 執行一次。
- Backends 依序為 `madvise`、`pread`。
- Baseline 只執行一次；四種非baseline variants各以兩個backends執行，共45 cells。
- Memory conditions只有`unlimited`，不啟用限制。
- 每個 cell timeout 為 180 秒。

Config 位於：

```text
configs/smoke.json
```

若 repository 位置或 helper 路徑不同，請在執行前修正 config。Experiment 開始後不可用相同 experiment ID 搭配不同 config；需要變更 config 時，請使用新的 experiment ID。

#### Config各區段

- `experiment`：設定唯一experiment ID、輸出根目錄與是否允許resume。
- `paths`：workloads、五個工具與drop-cache helper路徑。相對路徑以config所在目錄為基準。
- `layouts`：列出本次啟用的layout及其database、classification與metadata。
- `workloads`：設定workload types、sampling seed、training/measurement pools、抽樣數量與repetitions。
- `prefetch.backends`：有序backend陣列；目前支援`madvise`與`pread`。Baseline不屬於任何backend，只執行一次。
- `prefetch.strategies`：明確列出baseline與所有N/K variants。
- `cold_protocol`：正式設定固定為`none`、`before-cold`、`before-cold`。
- `memory_conditions`：有序條件陣列；第一個條件是memory comparison reference。
- `execution`：layout、strategy順序及cell timeout。
- `statistics`：percentiles與`nearest_rank`方法。

#### Config欄位與可用值

| 欄位 | 型別／可用值 | 限制或說明 |
| --- | --- | --- |
| `schema_version` | `1` | 目前只支援schema version 1。 |
| `experiment.id` | 字串 | 1–128字元；首字必須是英數字，後續可使用英數字、`.`、`_`、`-`。 |
| `experiment.output_root` | 路徑字串 | 相對路徑以config目錄為基準；預設範例為`../experiments`。 |
| `experiment.resume` | `true`／`false` | `true`允許相同ID及相同config hash續跑；不允許不同config覆蓋既有experiment。 |
| `paths.*` | 路徑字串 | 可使用絕對或相對路徑；相對路徑以config目錄為基準。 |
| `layouts` | object | Key可為`original`、`vacuum`、`rewrite`；每個layout需提供`database`、`classification`、`metadata`。 |
| `workloads.types` | 字串陣列 | 必須從下方12種workload types選取，不得重複；陣列順序即執行順序。 |
| `workloads.sampling_seed` | 整數 | 任意固定整數；相同seed與config會得到相同抽樣。 |
| `pool_start`、`pool_end_inclusive` | 整數 | 定義可抽樣的檔案index範圍，起點不得大於終點。 |
| `training.count`、`measurement.count` | 正整數 | 不得超過各自pool大小。 |
| `measurement.repetitions` | 正整數 | 每份measurement workload的重複次數。 |
| `prefetch.backends` | `"madvise"`、`"pread"`的非空陣列 | 可指定其中一種或兩種，不得重複；陣列順序即backend執行順序。 |
| `prefetch.pread_chunk_bytes` | 正整數 | 必須是實際SQLite page size的整數倍；smoke使用`1048576`。即使只用madvise也必須記錄。 |
| `prefetch.strategies` | strategy object陣列 | 支援`baseline`、`range_interior`、`offset_topk_interior`、`residency_topk`。 |
| `memory_conditions[].name` | 1–64字元filesystem-safe字串 | 首字為英數字，後續可用英數字、`_`、`-`；不得重複。 |
| `memory_conditions[].enabled` | `true`／`false` | `false`代表unlimited；`true`時以systemd scope套用限制。 |
| `memory_conditions[].memory_max` | `B`、`KiB`、`MiB`或`GiB`整數字串 | `enabled=true`時必填，例如`512MiB`；停用時必須省略或為`null`。 |
| `execution.layout_order` | layout名稱陣列 | 必須恰好包含所有enabled layouts且不得重複。 |
| `execution.strategy_order` | strategy名稱陣列 | 必須與configured strategies一致且不得重複。 |
| `execution.cell_timeout_seconds` | 正整數 | Smoke固定為`180`。 |
| `statistics.percentiles` | 1–100的整數陣列 | 必須包含`25`、`50`、`75`、`99`。 |
| `statistics.percentile_method` | `"nearest_rank"` | 目前只支援nearest-rank。 |

#### 啟用多個 layouts 的範例

必須先使用`build_layouts.py`產生對應的database、classification與metadata。下例同時啟用`original`、`vacuum`與`rewrite`：

```json
{
  "layouts": {
    "original": {
      "database": "../data/layouts/original/database.db",
      "classification": "../data/layouts/original/classify.csv",
      "metadata": "../data/layouts/original/metadata.json"
    },
    "vacuum": {
      "database": "../data/layouts/vacuum/database.db",
      "classification": "../data/layouts/vacuum/classify.csv",
      "metadata": "../data/layouts/vacuum/metadata.json"
    },
    "rewrite": {
      "database": "../data/layouts/rewrite/database.db",
      "classification": "../data/layouts/rewrite/classify.csv",
      "metadata": "../data/layouts/rewrite/metadata.json"
    }
  },
  "execution": {
    "layout_order": ["original", "vacuum", "rewrite"],
    "strategy_order": ["baseline", "range_interior", "offset_topk_interior", "residency_topk"],
    "cell_timeout_seconds": 180
  }
}
```

`execution.layout_order`必須恰好包含`layouts`中的全部keys，且不得重複；陣列順序就是layout執行順序。若只要測試其中一種layout，應同時從`layouts`移除其他項目，並將`layout_order`改成只包含該名稱。修改既有config時應改用新的`experiment.id`，避免與舊experiment的config hash衝突。

支援的12種`workloads.types`：

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

正式cold protocol只有下列值：

```json
{
  "cold_protocol": {
    "cold_advice": "none",
    "sqlite_open_timing": "before-cold",
    "schema_init_timing": "before-cold",
    "drop_caches_use_sudo": true
  }
}
```

`drop_caches_use_sudo`可為`true`或`false`：helper需要以passwordless `sudo -n`呼叫時使用`true`；執行帳號可直接呼叫工作站helper時使用`false`。此設定描述helper的呼叫介面，不代表orchestrator本身必須以root執行。

Backends可用組合：

```json
{"backends": ["madvise"]}
{"backends": ["pread"]}
{"backends": ["madvise", "pread"]}
{"backends": ["pread", "madvise"]}
```

Strategy可用格式：

```json
{"name": "baseline"}
```

```json
{"name": "range_interior"}
```

`offset_topk_interior`可明列N值：

```json
{
  "name": "offset_topk_interior",
  "n": {"values": [1, 5, 10]}
}
```

或使用range；`end_exclusive`不包含在結果中：

```json
{
  "name": "offset_topk_interior",
  "n": {
    "range": {"start": 1, "end_exclusive": 93, "step": 1}
  }
}
```

`values`與`range`不得同時出現。每個N必須是正整數，且不得超過該layout的eligible interior page count。

`residency_topk`明列每個K variant：

```json
{
  "name": "residency_topk",
  "variants": [
    {"label": "interior5_leaf0", "interior_k": 5, "leaf_k": 0},
    {"label": "interior5_leaf5", "interior_k": 5, "leaf_k": 5}
  ]
}
```

`interior_k`與`leaf_k`必須是非負整數，不得超過對應eligible page count，且兩者不得同時為0。`label`會成為filesystem-safe strategy key的一部分，建議只使用小寫英文字母、數字、`-`與`_`。

若要建立自訂experiment，先複製config並修改ID：

```bash
cp configs/smoke.json configs/my_experiment.json
editor configs/my_experiment.json
```

至少確認以下內容：

```json
{
  "experiment": {
    "id": "my-experiment",
    "output_root": "../experiments",
    "resume": true
  },
  "paths": {
    "drop_caches_script": "/usr/local/sbin/static-experiment-drop-caches"
  },
  "prefetch": {
    "backends": ["madvise", "pread"],
    "pread_chunk_bytes": 1048576
  }
}
```

上例只顯示需要注意的欄位；實際檔案仍須保留`smoke.json`中的其他必要區段。可先檢查JSON語法：

```bash
python3 -m json.tool configs/my_experiment.json >/dev/null
```

### 4. 執行 multi-backend smoke experiment

```bash
python3 tools/src/orchestrator.py \
  --config configs/smoke.json
```

Orchestrator 會先完成完整 preflight。Config、工具、layout、metadata、workload、strategy或檔案可執行狀態不合法時，experiment不會開始，也不會建立experiment目錄。Preflight只確認drop-cache helper存在且可執行；`sudo -n`授權與實際清除cache能力必須用前述probe另行確認。

#### 測試 memory limit 執行路徑

Canonical `smoke.json`依規格固定停用memory limit。若要驗證systemd scope與`MemoryMax`能否正常運作，使用獨立的`memory-limit-smoke.json`，避免改動或污染一般smoke結果。

先確認user systemd session與memory controller可用：

```bash
systemctl --user is-system-running

systemd-run --user --scope --collect --expand-environment=no \
  --unit=static-memory-limit-probe \
  -p MemoryMax=536870912 \
  /bin/sh -c 'cg=$(cut -d: -f3 /proc/self/cgroup); printf "memory.max="; cat "/sys/fs/cgroup${cg}/memory.max"'
```

Probe應成功結束，且第二個指令應輸出`memory.max=536870912`。接著執行同時比較`unlimited`與`512m`的90-cell experiment：

```bash
python3 tools/src/orchestrator.py \
  --config configs/memory-limit-smoke.json
```

輸出位於`experiments/memory-limit-smoke/`。通過條件為`state.json`顯示90個completed、0個failed，且`report.md`列出`unlimited`與`512m`及memory comparison。兩個條件使用相同抽樣，但各自建立training profile。這項測試不會刻意製造OOM來測試超限終止。

正式執行順序為：

```text
固定抽樣 training 與 measurement workloads
→ 每份 training workload 前清除 page cache
→ 執行 training 並取得 residency snapshot
→ 聚合 residency training profile
→ 執行所有 baseline cells
→ 執行 prefetch cells；每個 cell 前再次清除 page cache
→ 產生 raw results、summary、trade-off plot 與繁體中文 Markdown report
```

Training profile屬於measurement前的必要準備；若training timeout、helper失敗或snapshot無法產生，orchestrator會中止該次實驗並保留log。進入measurement cell階段後，個別cell的failed或timeout則不會阻止其餘cells繼續執行。

## 結果與驗證

Smoke experiment 的輸出位於：

```text
experiments/smoke/
```

每個目錄至少應包含：

```text
config.json
manifest.json
state.json
cells/
logs/
plots/tradeoff_madvise.png
plots/tradeoff_pread.png
plots/tradeoff_points.csv
report.md
results/all_raw.csv
results/read_zipf_full/layout_comparisons/unlimited.csv
results/read_zipf_full/original/memory_comparison.csv
results/read_zipf_full/original/memory_conditions/unlimited/baseline/raw.csv
results/read_zipf_full/original/memory_conditions/unlimited/baseline/summary.csv
results/read_zipf_full/original/memory_conditions/unlimited/backend_comparison.csv
results/read_zipf_full/original/memory_conditions/unlimited/backends/<backend>/strategy_comparison.csv
results/read_zipf_full/original/memory_conditions/unlimited/backends/<backend>/<strategy-key>/raw.csv
results/read_zipf_full/original/memory_conditions/unlimited/backends/<backend>/<strategy-key>/summary.csv
```

成功的 smoke experiment 應符合：

- Baseline `raw.csv`有5列；兩個backend各有四份5-row非baseline `raw.csv`，合計45筆cells，且所有`status`均為`completed`。
- Baseline cells 沒有 `prefetch_result.json`。
- 非 baseline cells 均有 `prefetch_result.json` 與 `selected_pages.csv`。
- Madvise 結果包含 `madvise_dispatch_us`。
- Pread 結果包含 `pread_elapsed_us`，成功 reads 的 completed bytes 等於 requested bytes。
- 每個 strategy variant 的 `summary.csv` 同時包含 `measurement` 與 `strategy` scope。
- Original layout的兩個backend目錄各包含`strategy_comparison.csv`，layout目錄另包含`backend_comparison.csv`。
- `read_zipf_full/layout_comparisons`目錄包含`unlimited.csv`，original目錄包含`memory_comparison.csv`。
- `plots/tradeoff_madvise.png`、`plots/tradeoff_pread.png`與`plots/tradeoff_points.csv`均已產生。
- `report.md` 已產生，並包含實驗摘要、環境、比較表、trade-off、cell狀態、workload清單與artifact連結。

再次使用完全相同的 config 執行相同命令時，orchestrator 會 resume：沿用既有抽樣與 training profile，並跳過必要artifacts存在、格式正確且彼此一致的completed cells。

## Resume規則

Resume必須使用相同experiment ID及內容完全相同的config：

```bash
python3 tools/src/orchestrator.py \
  --config configs/smoke.json
```

Resume時：

- 不重新抽樣training或measurement workloads。
- 相同完整training profile會直接重用。
- `cell.json`為`completed`且必要artifacts完整可解析的cell會跳過。
- Failed、timeout、中斷或artifact不完整的cell會從該measurement workload與repetition開頭重跑。
- 不會從單一query或operation中間恢復。
- Tool SHA-256改變時，舊cell identity失效，相關cells會重新執行。
- Experiment目錄已存在但config SHA-256不同時會拒絕執行。

若要修改config，推薦改用新的experiment ID。若一定要沿用舊ID，必須由使用者自行保存或移除舊experiment目錄；orchestrator不會自動刪除。

## 查看與解讀結果

先閱讀自動產生的繁體中文報告：

```bash
less experiments/smoke/report.md
```

若桌面環境支援，可分別開啟各backend的trade-off圖：

```bash
xdg-open experiments/smoke/plots/tradeoff_madvise.png
xdg-open experiments/smoke/plots/tradeoff_pread.png
```

主要結果位置：

```text
results/<workload-type>/<layout>/memory_conditions/<memory-condition>/baseline/
results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backends/<backend>/<strategy-key>/
results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backends/<backend>/strategy_comparison.csv
results/<workload-type>/<layout>/memory_conditions/<memory-condition>/backend_comparison.csv
results/<workload-type>/<layout>/memory_comparison.csv
results/<workload-type>/layout_comparisons/<memory-condition>.csv
```

- `raw.csv`：每列是一個measurement cell，保留全部原始samples。
- `results/all_raw.csv`：合併整個experiment的所有raw rows，並補上workload、layout、backend與strategy key，適合直接複製到其他環境分析。
- `summary.csv`：該strategy的measurement與strategy scope統計。
- `strategy_comparison.csv`：同一layout、memory condition與backend內和shared baseline比較。
- `backend_comparison.csv`：同一layout與memory condition下跨madvise/pread比較。
- `memory_comparison.csv`：相同layout、strategy、backend在所有memory conditions間做paired比較。
- `layout_comparisons/<memory-condition>.csv`：只使用該memory condition的backend-neutral baseline比較layouts。
- `tradeoff_<backend>.png`：只包含該backend點位的trade-off圖。
- `tradeoff_points.csv`：所有backend的trade-off實際點位與P25–P75；`backend`欄位保留分類。
- Trade-off圖以memory condition分成並排子圖，X軸使用log scale，Y軸依P25–P75範圍自動縮放；點位為median，水平與垂直線分別為兩軸的P25–P75 error bars。
- `cells/<cell-id>/cell.json`：完整cell provenance、metrics與artifact路徑。
- `logs/<cell-id>.err`：cell失敗時的主要診斷資訊。

Summary、strategy comparison與backend comparison另包含衍生metric `effective_first_query_latency_us`，其定義為`prefetch_elapsed_us + first_query_latency_us`。`effective_first_query_improvement_percent`使用相同measurement file與repetition的baseline first-query latency配對計算：

```text
(baseline_first_query_latency_us - effective_first_query_latency_us)
/ baseline_first_query_latency_us × 100
```

Report會同時顯示原始first-query改善與包含prefetch cost後的effective first-query改善。Raw CSV維持原始measurement欄位，不重複儲存可派生數值。

若要替既有experiment重新產生包含新指標的summary與report，先確認該experiment已有完整classified raw results與plots，再執行：

```bash
python3 tools/src/summarize_results.py --experiment-dir experiments/<experiment-id>
python3 tools/src/generate_report.py --experiment-dir experiments/<experiment-id>
```

快速查看CSV：

```bash
column -s, -t < \
  experiments/smoke/results/read_zipf_full/original/memory_conditions/unlimited/backend_comparison.csv \
  | less -S

column -s, -t < \
  experiments/smoke/plots/tradeoff_points.csv \
  | less -S
```

確認45個current cells的狀態：

```bash
python3 - <<'PY'
import csv
from collections import Counter
from pathlib import Path

path = Path("experiments/smoke/results/all_raw.csv")
with path.open(newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
print("cells:", len(rows))
print("status:", Counter(row["status"] for row in rows))
PY
```

Smoke成功時應顯示：

```text
cells: 45
status: Counter({'completed': 45})
```

## 完整端到端範例

以下範例假設剛把`static_experiment/`複製到一台可由管理者安裝root-owned helper的Linux。若使用無root權限工作站，省略步驟2與3，改用「無root權限工作站」小節提供的helper路徑與呼叫設定。

```bash
cd static_experiment

# 1. Build
make

# 2. 安裝root-owned drop-cache helper
sudo install -o root -g root -m 0755 \
  tools/bin/drop_caches.sh \
  /usr/local/sbin/static-experiment-drop-caches

# 3. 建立sudoers fragment；在編輯器加入：
# <user> ALL=(root) NOPASSWD: /usr/local/sbin/static-experiment-drop-caches
sudo visudo -f /etc/sudoers.d/static-experiment-drop-caches
sudo chmod 0440 /etc/sudoers.d/static-experiment-drop-caches
sudo visudo -c
sudo -k
sudo -n /usr/local/sbin/static-experiment-drop-caches

# 4. Provision original、vacuum、rewrite layouts
python3 tools/src/build_layouts.py \
  --source data/source/database.db \
  --output-dir data/layouts

# 5. 編輯config：將drop_caches_script與drop_caches_use_sudo設為本機helper的實際介面
editor configs/smoke.json
python3 -m json.tool configs/smoke.json >/dev/null

# 6. 執行45-cell multi-backend smoke
python3 tools/src/orchestrator.py \
  --config configs/smoke.json

# 7. 查看報告與狀態
less experiments/smoke/report.md
cat experiments/smoke/state.json

# 8. 使用完全相同的config重跑，確認resume會跳過completed cells
python3 tools/src/orchestrator.py \
  --config configs/smoke.json
```

## 常見失敗

- Drop-cache helper失敗：確認config使用該主機實際提供的helper絕對路徑，並核對`drop_caches_use_sudo`。若採`sudo -n`，請由管理者修正最小授權；不要改成互動式sudo。
- Preflight 回報 layout hash 不符：重新從 canonical source provisioning，勿手動修改 layout artifacts。
- Experiment ID 已存在但 config hash 不同：改用新的 experiment ID。
- Cell timeout 或 failed：查看該 experiment 的 `logs/` 與對應 `cells/<cell-id>/cell.json`；其他 cells 仍會繼續執行。
- Memory-limited timeout偶爾顯示`Unit ... not loaded`：scope可能已在kill命令送達前自行結束並被`--collect`回收；以`cell.json`的`status=timeout`、wrapper已回收且後續cells仍執行為判斷依據。這類訊息的數量不必與timeout cell數一一對應。

`state.json`的`status=completed`表示orchestrator已完成整個排程與後處理，不代表每個cell都成功；請同時檢查`completed`與`failed`計數。此處的`failed`計數包含所有非`completed`cell（例如failed或timeout）。
