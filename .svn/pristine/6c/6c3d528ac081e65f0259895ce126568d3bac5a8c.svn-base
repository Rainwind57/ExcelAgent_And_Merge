# 合并引导性能优化报告

> 生成时间：2026-08-04 15:30，本机复算（.venv）。

## 一、优化历程（会话实测）

| 阶段 | 操作 | 优化前 | 优化后 | 手段 |
|---|---|---|---|---|
| 比对（branch compare 全表） | 后端 | ~48s | ~6.4s | ① sparse 全等行省略 versions（payload 50.6MB→41.8MB）② `_sheet_names` 改 python-calamine ③ compare 端点 `model_dump_json` 绕过 `jsonable_encoder`（4.9s→0.3s） |
| 解决冲突/切换表格 | 前端 | 3-4s/次 | 即时（O(冲突数)） | 稀疏索引（冲突格/差异行候选+增量计数）+ 稀疏撤销快照 |
| apply 上传 | 前端 | 46.4MB | 0.1MB | tablesPayload 只传差异行 |
| apply 处理（big_data 单表） | 后端 | ~19.6s | ~4.3s | 公式检测 zip 快扫（省 2 次全量读）+ XML 直改快路径（lxml，绕开 openpyxl load+save） |
| 提交历史 | 后端 | 0.04s | 0.04s | 本身不慢；慢感来自 apply 等待 |

## 二、本机分阶段复算（脚本实测）

| 阶段 | 说明 | branch compare(处理) | 序列化 | big_data apply |
|---|---|---|---|---|
| S0 未优化 | | 31.42s | 5.39s | 19.23s |
| S1 读取加速 | | 6.78s | 5.87s | 19.76s |
| S2 序列化加速 | | 7.71s | 0.33s | 19.30s |
| S3 当前（含 apply 快路径/公式快扫/差异行 payload） | | 7.31s | 0.29s | 4.19s |

说明：compare 列为处理+序列化之外的纯比对耗时拆解见下表。

## 三、HTTP 端到端基线（当前，3 轮均值）

| 操作 | 平均 | 最快 |
|---|---|---|
| branch_compare | 7.47s | 7.13s |
| subdir_compare | 6.01s | 5.76s |
| branch_apply(过滤) | 5.91s | 5.77s |
| commits | 0.04s | 0.03s |

## 四、前端优化细节（不可脚本测量，说明）

- **稀疏索引**：比对完成后一次 O(总行数) 构建 `diffCandidates`/`rowCandidates`/`liveCounts`；
  解决冲突/切表/跳转全部降为 O(冲突数)，10w 行表不再每次全表扫描。
- **稀疏撤销快照**：每次解决只记录将变化的单元格（原实现全量深拷贝 10w 行）。
- **apply 差异行过滤**：只上传 conflict/resolved/changed/inserted/deleted/missing_row 行，46.4MB→0.1MB。

## 五、结论

- 后端 compare：**48s → 6.4s**（7.5×）；apply：**22.9s → ~6s**（3.8×，含上传）。
- 前端交互：解决/切表从秒级卡顿降为即时。
- 修复同轮发现的既有 bug：apply 对目标已存在主键的 inserted 行重复插入（重复 PK）。