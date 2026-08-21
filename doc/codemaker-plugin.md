# codemaker 插件 Skill 挂载指南

> 配表系统后端（`http://127.0.0.1:8000`）可通过三个 HTTP 端点向 codemaker 插件侧暴露 skill 包，使插件也能挂载同一套 skill 操作 Excel 配表。本文档约定挂载流程与 `CodeMakerCLI` 抽象接口。

## 1. 背景

配表系统的 skill 知识库分四层（位于 `server/agent/excel/skills/`）：

| 层级 | 目录 | 可变性 | 内容 |
|---|---|---|---|
| L0 | 根目录 | 纯人工，极少改 | 方法论：`parser_config.yaml` / `sheet_aliases.yaml` / `column_short_form.yaml` / `index_builder_hints.yaml` / `formula_agent.md` |
| L1 | `L1_derived/` | 表变更自动重算 | 表结构派生：`column_aliases` / `row_aliases` / `table_context` / `value_constraints` / `merge_strategies` / `cascade_rules` / `enum_mappings` |
| L2 | `L2_runtime/` | 运行结果驱动 | 使用经验：`table_relations.runtime.json` / `column_aliases.runtime.yaml` |
| L3 | `L3_anti_patterns/` | 雷区/反模式 | `anti_patterns.yaml` |
| —  | `_pending/` | 候选池 | 验证通过才 promote（不导出） |

插件侧拉取后，应将 L0/L1/L2/L3 合并加载（合并规则：L1 manual 优先，L2 runtime 叠加，L3 强制约束），与后端 `skill_loader.py` 行为一致。

## 2. 三个 HTTP 端点

### 2.1 `GET /api/skills/manifest`

列出 skills/ 下所有文件的层级/用途/更新时间/大小。插件侧首次挂载或定期同步前调用，判断是否需要拉取增量。

**响应示例：**
```json
{
  "skills_dir": "/path/to/server/agent/excel/skills",
  "total_files": 18,
  "files": [
    {
      "path": "parser_config.yaml",
      "layer": "L0",
      "purpose": "解析器前导动词 + 非业务 sheet 标记",
      "size": 412,
      "mtime": "2026-07-22T03:21:00+00:00"
    },
    {
      "path": "L1_derived/column_aliases.yaml",
      "layer": "L1",
      "purpose": "列名别名映射（含中英对照）",
      "size": 1823,
      "mtime": "2026-07-23T06:01:00+00:00"
    }
  ]
}
```

**字段说明：**
- `path`：相对 skills/ 的 posix 路径（zip 内同名）
- `layer`：`L0` / `L1` / `L2` / `L3` / `pending`
- `purpose`：用途摘要（未知文件标"未分类"）
- `size`：字节数
- `mtime`：UTC ISO 8601 时间戳

### 2.2 `GET /api/skills/export`

打包整个 skills/ 为 zip 流式下载。跳过候选池 jsonl / 隔离区 / 快照（插件侧无需）。

**响应：** `Content-Type: application/zip`，`Content-Disposition: attachment; filename="skills_bundle_YYYYMMDD_HHMMSS.zip"`。

zip 内文件路径与 manifest 的 `path` 字段一致，解压后可直接覆盖插件本地 skills 目录。

### 2.3 `GET /api/skills/diff?since=YYYY-MM-DD`

返回 `since` 之后修改的文件清单（增量 delta）。插件侧定期轮询此端点，仅当 `total > 0` 时才重新拉取 export。

**参数：** `since` — 起始日期 `YYYY-MM-DD` 或 ISO 8601 时间戳。

**响应示例：**
```json
{
  "since": "2026-07-22T00:00:00+00:00",
  "changed_files": [
    {
      "path": "L1_derived/column_aliases.yaml",
      "layer": "L1",
      "purpose": "列名别名映射（含中英对照）",
      "size": 1823,
      "mtime": "2026-07-23T06:01:00+00:00"
    }
  ],
  "total": 1
}
```

## 3. 推荐挂载流程

```
1. 插件启动 → GET /api/skills/manifest → 记录各文件 mtime
2. GET /api/skills/export → 解压到插件本地 skills 目录 → 加载四层
3. 定期（如每 5 分钟）GET /api/skills/diff?since=<上次最新 mtime>
   - total = 0 → 跳过
   - total > 0 → 重新 GET /api/skills/export → 热重载
```

## 4. `CodeMakerCLI` 抽象接口约定

插件侧应实现一个 `CodeMakerCLI` 抽象，封装对 Excel 的底层操作。后端 agent 通过 `server/agent/excel/cli_interface.py` 调用本地 `xlsx_tool`；插件侧实现同一接口后即可复用 skill 知识库操作各自环境下的表格。

### 4.1 必需方法

| 方法签名 | 作用 | 后端对应 |
|---|---|---|
| `read_sheet(path, sheet) -> SheetData` | 读整 sheet（表头 + 数据行 + 公式） | `cli_interface.read_sheet` |
| `write_cell(path, sheet, row, col, value) -> None` | 写单个单元格（保留样式） | `cli_interface.write_cell` |
| `append_row(path, sheet, row) -> None` | 追加行（继承相邻行样式） | `cli_interface.append_row` |
| `insert_row(path, sheet, row_idx, row) -> None` | 插入行（继承样式 + 公式 shift） | `cli_interface.insert_row` |
| `delete_row(path, sheet, row_idx) -> None` | 删除行 | `cli_interface.delete_row` |
| `scan_workbook(path) -> WorkbookSchema` | 扫整表返回列约束/类型/唯一性 | `cli_interface.scan_workbook` |

### 4.2 约定

- `row`/`col` 均为 1-based Excel 行列号（与 `write_cell` 一致）
- `value` 为 Python 原生类型（str/int/float/datetime），CLI 内部负责 Excel 类型映射
- 写操作必须保留原单元格样式（字体/填充/边框/数字格式），参考后端 `style_utils.copy_cell_style`
- 公式列识别：`read_sheet` 返回的 `SheetData` 需区分纯值单元格与公式单元格（`formula` 字段），避免公式被覆写（M14）

### 4.3 skill 知识库使用约定

插件侧加载四层 skill 后，调用顺序应与后端 agent 一致：

1. **解析阶段**：`parser_config.lead_verbs` 剥离前导动词 → `sheet_aliases` 解析表名 → `column_short_form` + `column_aliases` 解析列名 → `row_aliases` 定位行
2. **校验阶段**：`value_constraints` 校验类型/唯一/外键 → `cascade_rules` 校验级联 → `id_scope`（跨表编号段）校验
3. **反模式检查**：`anti_patterns`（L3）命中 `force_exact` → 强制精确匹配；命中 `block_dry_run` → 阻断 dry-run 预览
4. **运行时经验**：`table_relations.runtime`（L2）提供跨表热路径权重，辅助多表场景表名消歧

## 5. 版本兼容

- 后端 `version` 2.0.0 起提供三端点
- skill 包格式向后兼容：新增 yaml 字段不破坏旧插件解析
- 插件侧应忽略未知 `layer` 值（如未来扩展 L4），仅处理 L0/L1/L2/L3

## 6. 故障排查

| 现象 | 排查 |
|---|---|
| `GET /manifest` 返回 `total_files: 0` | 确认后端 `SKILLS_DIR` 路径存在（`server/config.py`） |
| `GET /export` 404 | skills 目录缺失或权限不足 |
| `GET /diff` 400 | `since` 格式不符 YYYY-MM-DD 或 ISO 8601 |
| 插件侧列名匹配失败 | 检查 `L1_derived/column_aliases.yaml` 是否含该表别名；中英文混排见 R14 |
