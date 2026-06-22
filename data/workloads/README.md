# new_workloads workload 說明

這個資料夾包含一批給 `benchmark_harness` 使用的純文字 workload 檔案。

每個 `.txt` 檔案代表一個 workload。每行是一個 operation，總共 1000 行。這批檔案只包含 `read` 與 `scan`，不包含 `update`、`insert`、`readmodifywrite`。

## 檔案總覽

本資料夾包含：

```text
600 個 workload .txt 檔案
1 個 SUMMARY.csv
1 個 README.md
```

600 個 workload 由 12 種 workload type 組成，每種 type 50 個檔案。

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

## 檔名格式

所有 workload 檔案使用以下格式：

```text
<operation>_<distribution>_<range_type>_<index>.txt
```

欄位意義：

```text
operation     read 或 scan
distribution  uniform 或 zipf
range_type    full、window 或 tail
index         三位數，001 到 050
```

範例：

```text
read_uniform_full_001.txt
read_zipf_tail_050.txt
scan_uniform_window_001.txt
scan_zipf_tail_050.txt
```

## 檔案內容格式

### read workload

`read_*` 檔案每行格式如下：

```text
read <id>
```

範例：

```text
read 525219
```

`read` 的合法 ID 範圍是：

```text
1..600000
```

### scan workload

`scan_*` 檔案每行格式如下：

```text
scan <id> 50
```

範例：

```text
scan 348120 50
```

`scan` 的第三個欄位固定為 `50`，代表 scan 長度。`scan` 的起始 ID 必須保證可以完整回傳 50 筆資料，因此合法起始 ID 範圍是：

```text
1..599951
```

## 固定參數

這批 workload 使用以下固定參數：

```text
record_count = 600000
ops_per_file = 1000
files_per_type = 50
window_size = 60000
tail_size = 60000
zipf_theta = 0.99
scan_len = 50
base_seed = 20260617
```

## Range 類型

每個 workload 使用一種 range 類型。operation ID 只會從該檔案的實際 range 內產生。

### full

`full` 使用完整合法 ID 範圍。

```text
read full range = 1..600000
scan full range = 1..599951
```

### window

`window` 是一段非 tail 的連續局部範圍。每個 window workload 檔案都會各自抽樣一個 `window_start`，實際 range 長度固定為 60000。

```text
read window_start 抽樣範圍 = 1..480001
scan window_start 抽樣範圍 = 1..479952
window_end = window_start + 59999
```

每個 window 檔案實際使用的 `range_start` 與 `range_end` 可在 `SUMMARY.csv` 中查到。

### tail

`tail` 使用資料尾端的固定範圍。

```text
read tail range = 540001..600000
scan tail range = 539952..599951
```

## 分布類型

每個 workload 使用一種 ID 抽樣分布。

### uniform

`uniform` 在該檔案的實際 range 內均勻抽樣 ID。

### zipf

`zipf` 在該檔案的實際 range size 上產生 Zipf rank，再把 rank 對應到實際 ID。

Zipf 參數：

```text
theta = 0.99
minimum = 1
maximum = range_end - range_start + 1
```

Zipf 抽樣使用 `generated_workloads/generate_ycsb_workloads.py` 中既有的 `ZipfianGenerator`。

rank 到 ID 的對應方向如下：

```text
full   -> forward
window -> forward
tail   -> reverse
```

`forward`：

```text
rank 1 -> range_start
rank 2 -> range_start + 1
rank 3 -> range_start + 2
```

`reverse`：

```text
rank 1 -> range_end
rank 2 -> range_end - 1
rank 3 -> range_end - 2
```

這表示 `zipf_tail` 會偏向較大的 ID，也就是 tail range 的尾端。

## Seed 與重現規則

每個 workload type 有固定的 `type_index`：

```text
0  read_uniform_full
1  read_uniform_window
2  read_uniform_tail
3  read_zipf_full
4  read_zipf_window
5  read_zipf_tail
6  scan_uniform_full
7  scan_uniform_window
8  scan_uniform_tail
9  scan_zipf_full
10 scan_zipf_window
11 scan_zipf_tail
```

每個檔案的 seed 計算方式：

```text
seed = base_seed + type_index * 1000 + file_index
```

其中：

```text
base_seed = 20260617
file_index = 1..50
```

範例：

```text
read_uniform_full_001.txt
type_index = 0
file_index = 1
seed = 20260617 + 0 * 1000 + 1 = 20260618

scan_zipf_tail_050.txt
type_index = 11
file_index = 50
seed = 20260617 + 11 * 1000 + 50 = 20271667
```

每個檔案使用一個 Python RNG：

```text
random.Random(seed)
```

`full` 與 `tail` workload 的產生順序：

```text
1. 使用 seed 初始化 RNG
2. 設定 range_start 與 range_end
3. 使用同一個 RNG 產生 1000 筆 operation ID
```

`window` workload 的產生順序：

```text
1. 使用 seed 初始化 RNG
2. 使用同一個 RNG 先抽 window_start
3. 設定 range_start = window_start
4. 設定 range_end = window_start + 59999
5. 使用同一個 RNG 繼續產生 1000 筆 operation ID
```

## SUMMARY.csv

`SUMMARY.csv` 記錄每個 workload 檔案的生成參數。每一列對應一個 `.txt` workload 檔案，共 600 列資料，不含 header。

格式：

```text
encoding = UTF-8 without BOM
delimiter = comma
newline = LF
header = required
```

欄位：

```text
filename,type,operation,distribution,range_type,file_index,seed,record_count,op_count,valid_id_start,valid_id_end,range_start,range_end,window_size,tail_size,zipf_theta,zipf_direction,scan_len
```

欄位意義：

```text
filename       workload 檔名
type           workload type
operation      read 或 scan
distribution   uniform 或 zipf
range_type     full、window 或 tail
file_index     1..50
seed           該檔案使用的 random seed
record_count   600000
op_count       1000
valid_id_start operation 合法 ID 起點
valid_id_end   operation 合法 ID 終點
range_start    該檔案實際使用的抽樣範圍起點
range_end      該檔案實際使用的抽樣範圍終點
window_size    60000
tail_size      60000
zipf_theta     0.99
zipf_direction forward、reverse 或空欄位
scan_len       50；read workload 為空欄位
```

空值使用空欄位，不使用 `NULL`、`NA` 或引號空字串。

範例：

```text
zipf_direction 在 uniform workload 中為空欄位
scan_len 在 read workload 中為空欄位
```

列排序規則：

```text
先依 type_index 由小到大排序
同一 type_index 內依 file_index 由小到大排序
```

## 如何理解任一 txt 檔案

以 `scan_zipf_tail_050.txt` 為例：

```text
operation = scan
distribution = zipf
range_type = tail
file_index = 50
```

因此這個檔案：

```text
每行格式為 scan <id> 50
共有 1000 行
scan 起始 ID 必定落在 539952..599951
使用 Zipf 分布，theta = 0.99
zipf_direction = reverse
seed = 20271667
```

若要知道它實際記錄在摘要中的參數，可查 `SUMMARY.csv` 中 `filename` 等於 `scan_zipf_tail_050.txt` 的那一列。

## 產生器

這批檔案由根目錄的以下腳本產生：

```text
generate_new_workloads.py
```

產生器會輸出到：

```text
new_workloads/
```

並引用以下既有 Zipf 實作：

```text
generated_workloads/generate_ycsb_workloads.py
```
