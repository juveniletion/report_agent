# 个人上传成本汇总进入月度报告（MVP）设计

- 日期：2026-11-16
- 状态：待用户审阅
- 实现分支：`feat/personal-csv-report`
- 范围：**只接成本汇总表**；其余 8 类报表数据仍用共享基线

## 1. 目标

1. 用户上传的成本汇总 CSV（现有表头）能进入其个人月度报告。
2. 个人同产品/月份行覆盖共享行；个人缺失时回退共享。
3. 报告明确列出来源与回退/缺失项，不静默混用。
4. 个人报告只写个人工作区，其他用户不可见、不可下载。
5. 未启用个人数据时，现有共享报告行为完全不变。

## 2. 非目标

- 不接原材料/人工/制造费用/预算/行情/二厂表的个人覆盖（下一阶段）。
- 不支持任意 CSV 列映射。
- 不支持季度/专题主题的个人数据（MVP 只做月度）。
- 不新增产品；只支持现有 3 个产品。
- 不把个人数据写入共享 `wiki/`、`state/`、`graph/`、`reports/`、SQLite。

## 3. 数据契约

### 3.1 文件位置与识别

- 个人文件位置：`data/users/<id>/ws/raw/csv/cost_data/**/*.csv`。
- 按**表头**识别，不依赖文件名。
- 必需列：
  - `工厂`
  - `产品名称`
  - `产品规格`
  - `月份`
  - `产量(盒)`
  - `直接材料(元/盒)`
  - `直接人工(元/盒)`
  - `制造费用(元/盒)`
  - `单位成本(元/盒)`
  - `总成本(元)`
- 文件名含“成本汇总”但表头不匹配 → 400，并列出缺失列。
- 其他 CSV 不参与本功能，不报错。

### 3.2 取值与合并

- 只接受 `工厂 = 中药一厂` 的行；`中药二厂` 行在 MVP 中忽略并返回警告。
- 按 `(产品名称, 月份)` 合并：
  - 共享 `中药一厂_成本汇总_2026年1-6月.csv` 提供 2026 基线。
  - 共享 `中药一厂_成本汇总_2025年1-6月.csv` 提供 2025 基线。
  - 个人 2026 行覆盖 2026 基线；个人 2025 行覆盖 2025 基线。
  - 个人没有的月份/字段回退共享。
- 个人文件之间同一 `(产品名称, 月份)` 重复 → 400，列出冲突文件，不静默取最后一个。
- `产品名称` 不在现有 3 个产品内 → 400。
- 允许新增月份（如 2026-07）；其他共享表没有该月数据时，对应段落记缺失并在报告中标注。

### 3.3 来源坐标

- 合并后的每一行附带 `_source_name`：
  - 共享行：`中药一厂_成本汇总_2026年1-6月.csv` / `...2025...`
  - 个人行：实际个人文件名
- 成本汇总相关 `Fact` 的 `coord` 与 `source_file` 使用该行的 `_source_name`；
  不再固定写共享文件名。
- 其他表的事实坐标保持现状（仍指共享文件）。
- `FactSet` 增加来源清单，供报告 stats/API 返回。

## 4. 架构

### 4.1 ReportDataContext

在 `report_facts.py` 引入 `contextvars.ContextVar`：

```python
@dataclass
class ReportDataContext:
    shared_root: Path
    personal_root: Optional[Path]
    use_personal: bool
```

- `report_context(...)` 上下文管理器负责设置/恢复；默认无上下文 = 现有共享行为。
- `_read_cost_summary(name, year)` 使用上下文合并共享 + 个人行。
- 其余 `_read` 保持不变，避免影响布局与共享路径。
- 使用 contextvar 而非全局变量，避免并发请求互相污染。

### 4.2 report_build

- 现有 `build` 改名为 `_build_impl`；新增同名包装 `build`，签名增加：
  - `use_personal: bool = False`
  - `personal_root: Optional[Path] = None`
  - `out_dir: Optional[Path] = None`
- 包装函数在 `report_context` 内调用 `_build_impl`，确保 `RF.build`、图表、对标都使用同一数据上下文。
- `_build_impl` 的 markdown/docx 默认输出改用 `out_dir`；模板仍从共享代码仓库读取。
- `stats` 增加：
  - `scope`: `shared` / `personal`
  - `sources`: 个人覆盖表/月份/文件、共享回退项
  - `warnings`: 忽略的二厂行、缺失项等

### 4.3 图表与 Word 模板

- `build_report_charts` 已调用 `RF.build`；在上下文中自动使用个人成本汇总。
- Word 模板输出仍走 `template_docx`，图表写入个人输出目录的 `_charts/`。

## 5. 输出与隔离

### 5.1 输出位置

- 个人模式输出根：`data/users/<id>/ws/reports/`
- 共享模式：`reports/`（现状不变）
- 个人图表：`ws/reports/_charts/`
- 个人报告绝不写共享 `reports/`、`exports/`、`state/`、DB。

### 5.2 API

- `POST /api/control/generate` 的 `GenerateIn` 增加 `use_personal: bool = False`。
- `use_personal=True` 时：
  - 只允许 `theme == "monthly"`，否则 400。
  - 取当前用户 `ws` 作为个人根，输出到 `ws/reports/`。
  - 返回 `scope: "personal"`、`sources`、`warnings`、`file`。
- 新增用户级接口（均要求登录）：
  - `GET /api/control/my-reports`：列出 `ws/reports/*.md|*.docx`。
  - `GET /api/control/my-download?name=...`：只能下载当前用户 `ws/reports/` 下的文件。
- 共享 `/api/reports`、`/api/control/exports`、`/api/control/download` 不变，不混入个人报告。
- 文件名校验：只取 `basename`，后缀白名单 `.md/.docx/.pdf`，路径必须落在当前用户 `ws/reports/`。

### 5.3 前端

- 报告生成区增加复选框 **「使用我的上传数据」**。
- 请求体带 `use_personal`。
- 生成后：
  - 个人模式下载链接指向 `/api/control/my-download`。
  - 展示实际使用的个人文件/月份、回退共享项、警告。
- 未勾选时 UI 行为不变。

## 6. 错误处理

| 情况 | 行为 |
|---|---|
| 个人文件表头错误 | 400，列出缺失列 |
| 个人文件重复键 | 400，列出冲突文件 |
| 产品不在现有 3 个 | 400 |
| 非月度主题 + `use_personal` | 400 |
| 没有个人成本汇总 | 回退共享 + warning，不报错 |
| 个人文件含二厂行 | 忽略 + warning |
| 个人报告不存在/属于他人 | 404 |
| 共享数据缺失且个人也没有 | 沿用现有 `DataMissing` / 缺失标记 |

## 7. 测试与验收

1. **同月覆盖**：个人 2026-05 单位成本改值 → 报告使用个人值，坐标指向个人文件。
2. **新增月份**：个人 2026-07 → 趋势/当月事实使用个人值；其他表回退共享并标注。
3. **回退**：个人只覆盖部分月份 → 未覆盖月份仍为共享值。
4. **表头错误/重复键/未知产品** → 400，不生成报告。
5. **隔离**：用户 A 生成的个人报告，用户 B 列表不可见、下载 404。
6. **Word 模板 + 图表**：个人报告仍为 3 张图 + 0 残留占位符。
7. **共享回归**：不启用个人数据时，共享报告结果与现状一致；现有 smoke 通过。
8. **计算校验**：个人报告跑 `check_math.py` 通过；`check_evidence` 的坐标指向个人文件。

## 8. 风险与取舍

- 仅成本汇总表来源可变；其他表的数字仍来自共享，报告必须明确标注“混合来源”。
- 成本汇总事实的坐标改为真实个人文件；其他表维持共享坐标。
- 个人报告不进入共享报告列表；评委演示时需在“我的报告”或生成结果中下载。
- 新增月份可能使趋势图超过 6 个月；MVP 保持现有绘图逻辑，不额外截断。
- `check_evidence` 需要以个人 ws 为数据根才有意义；MVP 先保证坐标真实，后续补个人校验命令。

## 9. 变更文件（预计）

- `scripts/report_facts.py`：context、成本汇总合并、来源坐标、来源清单。
- `scripts/report_build.py`：包装 build、`out_dir`、stats。
- `server/app.py`：`use_personal`、个人输出、my-reports/my-download。
- `server/static/app.js`：复选框、来源/警告展示、下载链接。
- `README.md` / `官方运行指南.md` / `CLAUDE.md`：数据来源与隔离说明。
- 测试脚本或 smoke 检查。

## 10. 阶段

- **Phase 1（本 spec）**：成本汇总 MVP + 个人报告隔离。
- **Phase 2**：其余 8 类表个人覆盖、每表来源坐标、季度/专题。
- **Phase 3**：个人证据校验命令、更多 schema 测试。
