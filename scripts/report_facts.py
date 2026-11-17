# -*- coding: utf-8 -*-
"""
report_facts.py — **报告事实引擎**（确定性取数，LLM 不参与）

设计原则
--------
> **能用脚本代码做的事情，就不用 LLM 做。**

月度成本报告里 **90% 的内容是数字**——而数字全部能从 raw CSV 直算。
把算术交给 LLM 是双输：既慢、又可能算错（本库实测就被 evidence checker
抓出过 3 处算错：产量比、截断精度、链接层级）。

所以本模块把**全部确定性内容**一次算清，并给每个值附上**溯源坐标**：

    fact = {
      "value": 11.21, "display": "11.21", "unit": "元/盒",
      "coord": "[中药一厂_成本汇总_2026年1-6月.csv:单位成本(元/盒):银黄口服液&2026-05]",
      "source_file": "raw/csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv",
      "formula": None,          # 原始值；派生值才有公式
    }

LLM 只拿到**算术结论**去写叙事段落，不参与任何计算。

数据缺失的处置
--------------
**缺失不是 0。** 找不到就记进 `missing` 并让 `value=None`，
由调用方决定显示为 `—` 还是显式说明。绝不静默填零——
那是本库最忌讳的"看起来很专业"的编造。

用法
----
    python scripts/report_facts.py --list-products
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05 --json
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05 \
        --monthly 2026-05 --out facts.json
"""
from __future__ import annotations

import argparse
import contextvars
import csv
import json
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
from errors import DataMissing, ConfigError  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
RAW = WIKI_ROOT / "raw"

UNIT_PER_BOX = {"银黄口服液": "支", "板蓝根颗粒": "袋", "六味地黄胶囊": "粒"}
BOX_PACK = {"银黄口服液": 10, "板蓝根颗粒": 20, "六味地黄胶囊": 60}
PRODUCT_SPEC = {
    "银黄口服液": "10ml×10支/盒",
    "板蓝根颗粒": "10g×20袋/盒",
    "六味地黄胶囊": "0.3g×60粒/盒",
}

# 个人成本汇总（MVP）——必须列齐全；表头按现有 CSV 契约校验。
PERSONAL_COST_COLUMNS = [
    "工厂", "产品名称", "产品规格", "月份", "产量(盒)",
    "直接材料(元/盒)", "直接人工(元/盒)", "制造费用(元/盒)",
    "单位成本(元/盒)", "总成本(元)",
]
PERSONAL_NUM_COLUMNS = [
    "产量(盒)", "直接材料(元/盒)", "直接人工(元/盒)",
    "制造费用(元/盒)", "单位成本(元/盒)", "总成本(元)",
]

# 制造费用五类 ↔ 模板措辞。CSV 列名是「人工(间接)」，模板叫「间接人工」。
#   [报告模板契约.md:3.3] 明确提示过这处名称映射。
MFG_TEMPLATE_NAMES = {
    "折旧费": "折旧费",
    "动力费(水电气)": "动力费(水电气)",
    "人工(间接)": "间接人工",
    "检验费": "检验费",
    "其他制造费用": "其他制造费用",
}


def _coord(fname: str, col: str, rows: str) -> str:
    return f"[{fname}:{col}:{rows}]"


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").rstrip("%")
    if s in ("", "—", "-", "–", "N/A", "无"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(v: Optional[float], nd: int = 2) -> str:
    """原样呈现，不截断（SKILL.md 铁律 2.1）。补零允许。"""
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def _pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    """环比/同比 %。分母为 0 或缺失时返回 None（缺失≠0）。"""
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / prev * 100.0


def _finalize_sources(fs: FactSet) -> FactSet:
    """把当前数据上下文的来源/警告写进 FactSet，供报告 stats/API 使用。"""
    ctx = _REPORT_CTX.get()
    if ctx is None:
        fs.sources = {"scope": "shared"}
        return fs
    warnings = list(dict.fromkeys(ctx.warnings))
    prefix = f"{fs.product}&"
    keys = {k: v for k, v in ctx.personal_rows.items() if k.startswith(prefix)}
    fs.warnings = warnings
    fs.sources = {
        "scope": "personal" if ctx.use_personal else "shared",
        "personal_files": sorted(set(keys.values())),
        "personal_keys": sorted(keys),
        "overridden_keys": sorted(k for k in keys if k in ctx.overridden),
        "ignored_rows": ctx.ignored_rows,
        "warnings": warnings,
    }
    return fs


# 行覆盖：让**季度/专题**分析能把"聚合后的合成行"喂进来，
# 从而**复用本模块的全部推导逻辑**（环比 / 占比 / 贡献度 / 预算偏差…）。
#
# ⚠️ 为什么用覆盖而不是另写一套聚合推导：
#    另写一套 = 两处口径，迟早漂移；而"环比""贡献度"这类算法
#    一旦两处实现不同，报告之间就会出现**互相矛盾但各自合法**的数——
#    本库最贵的一类错。覆盖机制让下游逻辑**只有一份**。
_OVERRIDE: Optional[Dict[str, List[Dict[str, str]]]] = None


def use_overrides(mapping: Optional[Dict[str, List[Dict[str, str]]]]) -> None:
    """
    临时替换若干 CSV 的读取结果。传 `None` 恢复读真实文件。

    用法（季度分析）：
        use_overrides({"csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv": 合成行})
        fs = build(product, month)
        use_overrides(None)          # 用完必须恢复，否则污染后续调用
    """
    global _OVERRIDE
    _OVERRIDE = mapping


def _read(name: str) -> List[Dict[str, str]]:
    if _OVERRIDE is not None and name in _OVERRIDE:
        return _OVERRIDE[name]
    p = RAW / name
    if not p.exists():
        # ⚠️ 这里曾是 `raise SystemExit`。SystemExit 继承 BaseException，
        #    app.py 的 `except Exception` 拦不住 ⇒ 6 个看板接口
        #    （overview/trend/heatmap/products/benchmark/decide）会**直接断连**，
        #    前端显示"网络错误"，把"缺个 CSV"误报成"服务挂了"。
        #    改用可捕获的 DataMissing，HTTP 层才能回一句人话。
        raise DataMissing(
            f"raw 文件缺失: {name}（找的是 {p}）",
            hint="确认数据已放在 raw/csv/ 下；自造的样例工作区可跑 "
                 "python scripts/make_sample_workspace.py 生成")
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_cost_summary(year: int) -> List[Dict[str, str]]:
    """
    读共享成本汇总，并在个人模式下合并个人成本汇总行。

    - 个人行按 `(产品名称, 月份)` 覆盖共享行；个人缺失的月份回退共享。
    - 个人行必须列齐全、数值可解析、两条恒等式成立，否则抛 ValueError。
    - 每行附加 `_source_name`，供事实坐标指向真实文件。
    """
    shared_name = f"csv/cost_data/中药一厂_成本汇总_{year}年1-6月.csv"
    shared = _read(shared_name)
    for r in shared:
        r["_source_name"] = Path(shared_name).name

    ctx = _REPORT_CTX.get()
    if not ctx or not ctx.use_personal or not ctx.personal_root:
        return shared

    personal_dir = ctx.personal_root / "raw" / "csv" / "cost_data"
    if not personal_dir.exists():
        ctx.warnings.append("未找到个人 raw/csv/cost_data；已回退共享成本汇总")
        return shared

    personal: Dict[Tuple[str, str], Tuple[str, Dict[str, str]]] = {}
    conflicts: List[str] = []
    ignored_local = 0

    for p in sorted(personal_dir.rglob("*.csv")):
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                cols = [c for c in (reader.fieldnames or []) if c]
                rows = list(reader)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"读取个人 CSV 失败：{p.name}：{type(e).__name__}: {e}")
        missing_cols = [c for c in PERSONAL_COST_COLUMNS if c not in cols]
        if missing_cols:
            if "成本汇总" in p.name:
                raise ValueError(
                    f"个人文件 {p.name} 表头不匹配，缺少列：{'、'.join(missing_cols)}")
            continue

        for idx, row in enumerate(rows, start=2):
            factory = (row.get("工厂") or "").strip()
            if not factory:
                raise ValueError(f"{p.name} 第 {idx} 行「工厂」为空")
            if factory not in ("中药一厂", "中药二厂"):
                raise ValueError(f"{p.name} 第 {idx} 行「工厂」非法：{factory}")
            if factory == "中药二厂":
                ignored_local += 1
                continue

            product = (row.get("产品名称") or "").strip()
            if product not in UNIT_PER_BOX:
                raise ValueError(
                    f"{p.name} 第 {idx} 行产品不在现有 3 个产品内：{product or '(空)'}")
            month = (row.get("月份") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise ValueError(f"{p.name} 第 {idx} 行月份格式非法：{month or '(空)'}")
            if not month.startswith(f"{year}-"):
                continue

            nums: Dict[str, float] = {}
            for col in PERSONAL_NUM_COLUMNS:
                v = _num(row.get(col))
                if v is None:
                    raise ValueError(
                        f"{p.name} 第 {idx} 行 {product}/{month} 列「{col}」为空或非数字")
                nums[col] = v

            mat = nums["直接材料(元/盒)"]
            lab = nums["直接人工(元/盒)"]
            mfg = nums["制造费用(元/盒)"]
            unit = nums["单位成本(元/盒)"]
            output = nums["产量(盒)"]
            total = nums["总成本(元)"]
            if abs(mat + lab + mfg - unit) > 0.02:
                raise ValueError(
                    f"{p.name} 第 {idx} 行恒等式不成立："
                    f"材料 {mat} + 人工 {lab} + 制造 {mfg} ≠ 单位成本 {unit}")
            if abs(output * unit - total) > 0.02:
                raise ValueError(
                    f"{p.name} 第 {idx} 行恒等式不成立："
                    f"产量 {output} × 单位成本 {unit} ≠ 总成本 {total}")

            spec = (row.get("产品规格") or "").strip()
            if spec and PRODUCT_SPEC.get(product) and spec != PRODUCT_SPEC[product]:
                ctx.warnings.append(
                    f"{p.name} {product} 规格「{spec}」与现有「{PRODUCT_SPEC[product]}」不一致")

            key = (product, month)
            if key in personal:
                prev_file, _ = personal[key]
                conflicts.append(f"{product}/{month}: {prev_file} 与 {p.name}")
            else:
                personal[key] = (p.name, dict(row))
                ctx.personal_files.add(p.name)
                ctx.personal_rows[f"{product}&{month}"] = p.name

    if conflicts:
        raise ValueError("个人成本汇总存在重复的产品/月份：" + "；".join(conflicts[:10]))
    ctx.ignored_rows = max(ctx.ignored_rows, ignored_local)
    if ctx.ignored_rows and not any("中药二厂" in w for w in ctx.warnings):
        ctx.warnings.append(
            f"忽略 {ctx.ignored_rows} 行中药二厂数据（MVP 只支持一厂）")
    if not personal:
        ctx.warnings.append(f"个人成本汇总未提供 {year} 年数据；已回退共享")
        return shared

    shared_keys = {
        ((r.get("产品名称") or "").strip(), (r.get("月份") or "").strip()): r
        for r in shared
    }
    by_key: Dict[Tuple[str, str], Dict[str, str]] = dict(shared_keys)
    for key, (fname, row) in personal.items():
        if key in shared_keys:
            ctx.overridden.add(f"{key[0]}&{key[1]}")
        r = dict(row)
        r["_source_name"] = fname
        by_key[key] = r

    out: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for r in shared:
        k = ((r.get("产品名称") or "").strip(), (r.get("月份") or "").strip())
        if k in by_key and k not in seen:
            out.append(by_key[k])
            seen.add(k)
    for k in sorted(by_key):
        if k not in seen:
            out.append(by_key[k])
            seen.add(k)
    return out


def _pick(rows: List[Dict[str, str]], **kw) -> Optional[Dict[str, str]]:
    for r in rows:
        if all((r.get(k) or "").strip() == str(v) for k, v in kw.items()):
            return r
    return None


@dataclass
class Fact:
    """一个可溯源的事实。`value=None` 表示数据缺失（不是 0）。"""
    value: Optional[float]
    display: str
    unit: str = ""
    coord: str = ""
    source_file: str = ""
    formula: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReportDataContext:
    """一次报告生成的数据根与个人覆盖状态（contextvar，线程/协程安全）。"""
    shared_root: Path
    personal_root: Optional[Path] = None
    use_personal: bool = False
    personal_files: Set[str] = field(default_factory=set)
    personal_rows: Dict[str, str] = field(default_factory=dict)  # "产品&月份" -> 文件
    overridden: Set[str] = field(default_factory=set)
    ignored_rows: int = 0
    warnings: List[str] = field(default_factory=list)


_REPORT_CTX: contextvars.ContextVar[Optional[ReportDataContext]] = \
    contextvars.ContextVar("report_data_context", default=None)


@contextmanager
def report_context(shared_root: Optional[Path] = None,
                   personal_root: Optional[Path] = None,
                   use_personal: bool = False):
    """设置一次报告取数的数据上下文；退出时恢复。默认 = 共享基线。"""
    ctx = ReportDataContext(
        shared_root=Path(shared_root) if shared_root else WIKI_ROOT,
        personal_root=Path(personal_root) if personal_root else None,
        use_personal=bool(use_personal and personal_root),
    )
    if use_personal and not personal_root:
        ctx.warnings.append("use_personal=True 但未提供 personal_root；已回退共享")
    token = _REPORT_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _REPORT_CTX.reset(token)


@dataclass
class FactSet:
    product: str
    month: str
    prev_month: str
    year_ago_month: str
    facts: Dict[str, Fact] = field(default_factory=dict)
    monthly: List[Dict[str, Any]] = field(default_factory=list)   # 近 6 个月趋势
    materials: List[Dict[str, Any]] = field(default_factory=list)  # 原材料明细
    mfg: List[Dict[str, Any]] = field(default_factory=list)        # 制造费用五类
    peer: List[Dict[str, Any]] = field(default_factory=list)       # 对标
    missing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)             # 已知不可自洽点
    sources: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["facts"] = {k: v.to_dict() for k, v in self.facts.items()}
        return d


# ---------------------------------------------------------------------------

def list_products() -> List[str]:
    rows = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    seen: List[str] = []
    for r in rows:
        p = (r.get("产品名称") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def _shift_month(month: str, delta: int) -> Optional[str]:
    """`2026-05` ± delta 个月。**不跨出数据窗口**时返回，否则 None。"""
    y, m = int(month[:4]), int(month[5:7])
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def build(product: str, month: str) -> FactSet:
    """算出某产品某月的全部报告事实。"""
    m26 = _read_cost_summary(2026)
    m25 = _read_cost_summary(2025)
    bud = _read("csv/cost_data/中药一厂_预算数据_2026年.csv")
    lab = _read("csv/cost_data/中药一厂_人工工时明细_2026年1-6月.csv")
    mfg = _read("csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv")
    mat = _read("csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv")
    p2 = _read("csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv")
    wx = _read("csv/market_data/药材市场价格行情_2026年上半年.csv")

    F26, F25, FB = ("中药一厂_成本汇总_2026年1-6月.csv",
                    "中药一厂_成本汇总_2025年1-6月.csv",
                    "中药一厂_预算数据_2026年.csv")
    FL, FM, FT = ("中药一厂_人工工时明细_2026年1-6月.csv",
                  "中药一厂_制造费用明细_2026年1-6月.csv",
                  "中药一厂_原材料消耗明细_2026年1-6月.csv")

    fs = FactSet(product=product, month=month,
                 prev_month=_shift_month(month, -1) or month,
                 year_ago_month="2025-" + month[5:])
    add = fs.facts.__setitem__
    if product not in UNIT_PER_BOX:
        fs.missing.append(f"未知产品: {product}")
        return _finalize_sources(fs)

    cur = _pick(m26, 产品名称=product, 月份=month)
    prev = _pick(m26, 产品名称=product, 月份=fs.prev_month)
    yago = _pick(m25, 产品名称=product, 月份=fs.year_ago_month)
    budr = _pick(bud, 产品名称=product, 月份=month)

    # 成本汇总行的真实来源（个人覆盖时指向个人文件）
    S_CUR = (cur or {}).get("_source_name", F26)
    S_PREV = (prev or {}).get("_source_name", F26)
    S_YA = (yago or {}).get("_source_name", F25)

    # 实际有 9/10 月简报（一厂），但该简报只含单位成本 → 单独处理，见 brief
    if cur is None:
        fs.missing.append(f"2026 年 {month} 成本汇总缺失（该窗口 raw 无数据）")

    def raw(table, row, col, fname, tag) -> Optional[float]:
        if row is None:
            return None
        return _num(row.get(col))

    # ---- 2.1 核心指标一览 ----
    def three(col: str, label: str, table_cur, row_cur, table_prev, row_prev):
        c = raw(table_cur, row_cur, col, F26, f"{product}&{month}")
        p = raw(table_cur, row_prev, col, F26, f"{product}&{fs.prev_month}")
        key = col
        rng = f"{product}&{month}"
        if c is not None:
            add(key, Fact(c, _fmt(c, 0), "", _coord(S_CUR, col, rng), S_CUR))
        else:
            fs.missing.append(f"{label}（本月）")
        if p is not None:
            add("上月" + key, Fact(p, _fmt(p, 0), "", _coord(S_PREV, col, f"{product}&{fs.prev_month}"), S_PREV))
        d = _pct(c, p)
        if d is not None:
            add(key + "环比", Fact(d, f"{d:+.2f}%", "%", _coord(S_CUR, col, rng), S_CUR,
                                   formula=f"({_fmt(c,0)} − {_fmt(p,0)}) ÷ {_fmt(p,0)}"))

    # 产量
    three("产量(盒)", "产量", m26, cur, m26, prev)
    # 单位成本
    c = raw(m26, cur, "单位成本(元/盒)", F26, "")
    p = raw(m26, prev, "单位成本(元/盒)", F26, "")
    ya = raw(m25, yago, "单位成本(元/盒)", F25, "")
    bv = raw(bud, budr, "预算单位成本(元/盒)", FB, "")
    if c is not None:
        # ⚠️ 覆写模式下（季度聚合）没有可指的 raw 列坐标——
        #    这时 `coord` 留空、改给公式，如实说明"这是期间聚合值"。
        #    硬编一个坐标等于**伪造溯源**，比留空更坏。
        add("本月单位成本", Fact(
            c, _fmt(c), "元/盒",
            "" if _OVERRIDE is not None else _coord(S_CUR, "单位成本(元/盒)", f"{product}&{month}"),
            S_CUR,
            formula="总成本 ÷ 总产量（期间聚合）" if _OVERRIDE is not None else None))
    if p is not None:
        add("上月单位成本", Fact(p, _fmt(p), "元/盒",
            _coord(S_PREV, "单位成本(元/盒)", f"{product}&{fs.prev_month}"), S_PREV))
    if ya is not None:
        add("去年单位成本", Fact(ya, _fmt(ya), "元/盒",
            _coord(S_YA, "单位成本(元/盒)", f"{product}&{fs.year_ago_month}"), S_YA))
    if bv is not None:
        add("预算单位成本", Fact(bv, _fmt(bv), "元/盒",
            _coord(FB, "预算单位成本(元/盒)", f"{product}&{month}"), FB))
    for key, num, den in (("单位成本环比", c, p), ("单位成本同比", c, ya),
                          ("单位成本预算偏差", c, bv)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%",
                          _coord(S_CUR, "单位成本(元/盒)", f"{product}&{month}"),
                          S_CUR, formula=f"({_fmt(num)} − {_fmt(den)}) ÷ {_fmt(den)}"))

    # 总成本
    tc = raw(m26, cur, "总成本(元)", F26, "")
    tp = raw(m26, prev, "总成本(元)", F26, "")
    ty = raw(m25, yago, "总成本(元)", F25, "")
    tb = raw(bud, budr, "预算总成本(元)", FB, "")
    for key, val, unit, tbl, tag in (
            ("本月总成本", tc, "元", S_CUR, f"{product}&{month}"),
            ("上月总成本", tp, "元", S_PREV, f"{product}&{fs.prev_month}"),
            ("去年总成本", ty, "元", S_YA, f"{product}&{fs.year_ago_month}"),
            ("预算总成本", tb, "元", FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), unit,
                          _coord(tbl, "总成本(元)" if "预算" not in key else "预算总成本(元)", tag), tbl))
    for key, num, den in (("总成本环比", tc, tp), ("总成本同比", tc, ty),
                          ("总成本预算偏差", tc, tb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(S_CUR, "总成本(元)", f"{product}&{month}"),
                          S_CUR, formula=f"({_fmt(num,0)} − {_fmt(den,0)}) ÷ {_fmt(den,0)}"))

    # 产量同比/预算偏差（产量环比已由 three() 产出）
    pv = raw(m26, cur, "产量(盒)", F26, "")
    pp = raw(m26, prev, "产量(盒)", F26, "")
    py = raw(m25, yago, "产量(盒)", F25, "")
    pb = raw(bud, budr, "预算产量(盒)", FB, "")
    for key, val, tbl, tag in (("去年同月产量", py, S_YA, f"{product}&{fs.year_ago_month}"),
                               ("预算产量", pb, FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), "盒", _coord(tbl, "产量(盒)" if tbl == S_YA else "预算产量(盒)", tag), tbl))
    for key, num, den in (("产量同比", pv, py), ("产量预算偏差", pv, pb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(S_CUR, "产量(盒)", f"{product}&{month}"),
                          S_CUR, formula=f"({_fmt(num,0)} − {_fmt(den,0)}) ÷ {_fmt(den,0)}"))

    # 三要素（本月/上月/环比/预算/偏差）
    for col, tag, name in (("直接材料(元/盒)", "材料", "材料"), ("直接人工(元/盒)", "人工", "人工"),
                           ("制造费用(元/盒)", "制造", "制造费用")):
        cc = raw(m26, cur, col, F26, "")
        cp = raw(m26, prev, col, F26, "")
        cb = raw(bud, budr, "预算" + col, FB, "")
        if cc is not None:
            add(f"本月{tag}成本", Fact(cc, _fmt(cc), "元/盒",
                _coord(S_CUR, col, f"{product}&{month}"), S_CUR))
        if cp is not None:
            add(f"上月{tag}成本", Fact(cp, _fmt(cp), "元/盒",
                _coord(S_PREV, col, f"{product}&{fs.prev_month}"), S_PREV))
        if cb is not None:
            add(f"预算{tag}成本", Fact(cb, _fmt(cb), "元/盒",
                _coord(FB, "预算" + col, f"{product}&{month}"), FB))
        v = _pct(cc, cp)
        if v is not None:
            add(f"{name}成本环比" if name != "制造费用" else "制造费用环比",
                Fact(v, f"{v:+.2f}%", "%", _coord(S_CUR, col, f"{product}&{month}"), S_CUR,
                     formula=f"({_fmt(cc)} − {_fmt(cp)}) ÷ {_fmt(cp)}"))
        v = _pct(cc, cb)
        if v is not None:
            add(f"{name}预算偏差", Fact(v, f"{v:+.2f}%", "%",
                _coord(S_CUR, col, f"{product}&{month}"), S_CUR,
                formula=f"({_fmt(cc)} − {_fmt(cb)}) ÷ {_fmt(cb)}"))

    # ---- 2.2 成本结构（占比 + 贡献度）----
    # 贡献度 = 该要素的「单位成本变动额」÷ 「单位成本变动总额」
    #   [成本异动登记册] 已核验：三项贡献度之和必须 = 100%
    dc = raw(m26, cur, "直接材料(元/盒)", F26, "")
    dp = raw(m26, prev, "直接材料(元/盒)", F26, "")
    lc = raw(m26, cur, "直接人工(元/盒)", F26, "")
    lp = raw(m26, prev, "直接人工(元/盒)", F26, "")
    mc = raw(m26, cur, "制造费用(元/盒)", F26, "")
    mp = raw(m26, prev, "制造费用(元/盒)", F26, "")
    if None not in (dc, lc, mc):
        tot = dc + lc + mc
        for tag, val in (("材料", dc), ("人工", lc), ("制造费用", mc)):
            add(f"{tag}金额", Fact(val, _fmt(val), "元/盒",
                _coord(S_CUR, f"直接{'材料' if tag=='材料' else '人工' if tag=='人工' else ''}(元/盒)"
                       if tag != "制造费用" else "制造费用(元/盒)", f"{product}&{month}"), S_CUR))
            if tot:
                add(f"{tag}占比", Fact(val / tot * 100, f"{val/tot*100:.2f}%", "%",
                    _coord(S_CUR, "单位成本(元/盒)", f"{product}&{month}"), S_CUR,
                    formula=f"{_fmt(val)} ÷ {_fmt(tot)}"))
        delta_tot = (dc + lc + mc) - ((dp or 0) + (lp or 0) + (mp or 0)) if None not in (dp, lp, mp) else None
        if delta_tot:
            for tag, c0, p0 in (("材料", dc, dp), ("人工", lc, lp), ("制造费用", mc, mp)):
                if c0 is None or p0 is None:
                    continue
                d = c0 - p0
                add(f"{tag}贡献度", Fact(d / delta_tot * 100, f"{d/delta_tot*100:.2f}%", "%",
                    _coord(S_CUR, "单位成本(元/盒)", f"{product}&{month}"), S_CUR,
                    formula=f"({_fmt(c0)} − {_fmt(p0)}) ÷ {_fmt(delta_tot)}"))

    # ---- 3.2 直接人工四指标 ----
    lc_row = _pick(lab, 产品名称=product, 月份=month)
    lp_row = _pick(lab, 产品名称=product, 月份=fs.prev_month)
    if lc_row and lp_row and pv:
        def lab_calc(row, tag, rng):
            tot_l = _num(row.get("直接人工总额(元)"))
            hours = _num(row.get("总工时(小时)"))
            people = _num(row.get("生产人数(人)"))
            days = _num(row.get("工作天数(天)"))
            prod = _num(row.get("产量(盒)"))
            if None in (tot_l, hours, prod):
                return
            add(f"人工单位成本{tag}", Fact(tot_l / prod, _fmt(tot_l / prod), "元/盒",
                _coord(FL, "直接人工总额(元),产量(盒)", f"{product}&{rng}"), FL,
                formula=f"{_fmt(tot_l,0)} ÷ {_fmt(prod,0)}"))
            if hours:
                v = hours / prod * 10000
                add(f"本月工时" if tag == "" else f"上月工时",
                    Fact(v, _fmt(v, 2), "h/万盒",
                         _coord(FL, "总工时(小时),产量(盒)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(hours,0)} ÷ {_fmt(prod,0)} × 10000"))
                add(f"本月时薪" if tag == "" else f"上月时薪",
                    Fact(tot_l / hours, _fmt(tot_l / hours), "元/h",
                         _coord(FL, "直接人工总额(元),总工时(小时)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(tot_l,0)} ÷ {_fmt(hours,0)}"))
            if people and days:
                v = prod / (people * days)
                add(f"本月效率" if tag == "" else f"上月效率",
                    Fact(v, _fmt(v, 2), "盒/人·日",
                         _coord(FL, "产量(盒),生产人数(人),工作天数(天)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(prod,0)} ÷ ({_fmt(people,0)} × {_fmt(days,0)})"))
        lab_calc(lc_row, "", month)
        lab_calc(lp_row, "上月", fs.prev_month)
        for k in ("工时", "时薪", "效率"):
            a, b = fs.facts.get(f"本月{k}"), fs.facts.get(f"上月{k}")
            if a and b:
                v = _pct(a.value, b.value)
                if v is not None:
                    add(f"{k}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FL,
                        formula=f"({a.display} − {b.display}) ÷ {b.display}"))

    # ---- 3.3 制造费用五类 ----
    for cat, tname in MFG_TEMPLATE_NAMES.items():
        r_c = _pick(mfg, 产品名称=product, 月份=month, 费用类别=cat)
        r_p = _pick(mfg, 产品名称=product, 月份=fs.prev_month, 费用类别=cat)
        if r_c:
            v = _num(r_c.get("单位费用(元/盒)"))
            if v is not None:
                add(f"本月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{month}"), FM))
        if r_p:
            v = _num(r_p.get("单位费用(元/盒)"))
            if v is not None:
                add(f"上月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{fs.prev_month}"), FM))
        a, b = fs.facts.get(f"本月{tname}"), fs.facts.get(f"上月{tname}")
        if a and b:
            v = _pct(a.value, b.value)
            if v is not None:
                add(f"{tname}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FM,
                    formula=f"({a.display} − {b.display}) ÷ {b.display}"))
        fs.mfg.append({"类别": tname, "csv类别": cat,
                       "本月": a.display if a else "—",
                       "上月": b.display if b else "—",
                       "环比": fs.facts.get(f"{tname}环比").display if fs.facts.get(f"{tname}环比") else "—"})
    # 合计校验：五类之和应与成本汇总的「制造费用(元/盒)」一致
    if mc is not None:
        s = sum(fs.facts[f"本月{n}"].value for n in MFG_TEMPLATE_NAMES.values()
                if fs.facts.get(f"本月{n}") and fs.facts[f"本月{n}"].value is not None)
        if abs(s - mc) > 0.02:
            fs.conflicts.append(
                f"制造费用五类之和 {_fmt(s)} ≠ 成本汇总的制造费用 {_fmt(mc)}"
                f"（差 {_fmt(s-mc)}）——需人工判读是否为舍入或口径差异")

    # ---- 3.1 原材料明细 ----
    mats_c = [r for r in mat if (r.get("产品名称") or "").strip() == product
              and (r.get("月份") or "").strip() == month]
    for r in mats_c:
        name = (r.get("原材料名称") or "").strip()
        cu = _num(r.get("单位消耗成本(元/盒)"))
        rp = _pick(mat, 产品名称=product, 月份=fs.prev_month, 原材料名称=name)
        pu = _num(rp.get("单位消耗成本(元/盒)")) if rp else None
        d = _pct(cu, pu)
        fs.materials.append({
            "原材料名称": name,
            "本月": _fmt(cu), "上月": _fmt(pu),
            "环比": f"{d:+.2f}%" if d is not None else "—",
            "占比": (r.get("占总材料成本比例") or "—").strip(),
            "单价来源": "中药材市场",
        })

    # ---- 4.1 近 6 个月趋势 ----
    for r in m26:
        if (r.get("产品名称") or "").strip() != product:
            continue
        fs.monthly.append({
            "月份": r["月份"],
            "产量": (r.get("产量(盒)") or "").strip(),
            "单位材料": _fmt(_num(r.get("直接材料(元/盒)"))),
            "单位人工": _fmt(_num(r.get("直接人工(元/盒)"))),
            "单位制造费用": _fmt(_num(r.get("制造费用(元/盒)"))),
            "单位成本": _fmt(_num(r.get("单位成本(元/盒)"))),
        })
    fs.monthly.sort(key=lambda x: x["月份"])
    for i, row in enumerate(fs.monthly):
        if i == 0:
            row["环比"] = "—"
            continue
        prev_v = _num(fs.monthly[i - 1]["单位成本"])
        cur_v = _num(row["单位成本"])
        d = _pct(cur_v, prev_v)
        row["环比"] = f"{d:+.2f}%" if d is not None else "—"

    # ---- 4.3 原材料价格跟踪 ----
    for r in wx:
        nm = (r.get("药材名称") or "").strip()
        jan, jun = _num(r.get("1月价格")), _num(r.get("6月价格"))
        d = _pct(jun, jan)
        r["_涨幅"] = f"{d:+.2f}%" if d is not None else "—"
    fs.market = []  # type: ignore[attr-defined]
    for r in wx:
        nm = (r.get("药材名称") or "").strip()
        used = next((m["原材料名称"] for m in fs.materials if m["原材料名称"] == nm), None)
        jan, jun = _num(r.get("1月价格")), _num(r.get("6月价格"))
        d = _pct(jun, jan)
        fs.market.append({  # type: ignore[attr-defined]
            "原材料": nm, "单位": (r.get("单位") or "").strip(),
            "年初价": _fmt(jan, 1), "6月价": _fmt(jun, 1),
            "涨幅": f"{d:+.2f}%" if d is not None else "—",
            "市场趋势": (r.get("趋势分析") or "").strip(),
            "用于本产品": "✓" if used else "",
        })

    # ---- 5 对标（一厂 vs 二厂）----
    p2c = _pick(p2, 产品名称=product, 月份=month)
    for dim, col in (("单位成本", "单位成本(元/盒)"), ("直接材料", "直接材料(元/盒)"),
                     ("直接人工", "直接人工(元/盒)"), ("制造费用", "制造费用(元/盒)")):
        a = raw(m26, cur, col, F26, "")
        b = raw(p2, p2c, col, "中药二厂_成本汇总_2026年1-6月.csv", "")
        if a is None or b is None:
            continue
        fs.peer.append({
            "维度": dim, "中药一厂": _fmt(a), "中药二厂": _fmt(b),
            "差异": _fmt(a - b), "差异率": f"{(a-b)/b*100:+.2f}%" if b else "—",
            "方向": "一厂高" if a > b else ("二厂高" if b > a else "持平"),
        })

    # ---- 已知不可自洽点：**从数据文件读**，不写死在代码里 ----
    # ⚠️ 这两句原本硬编码在此处。它们描述的是**具体某批数据**的口径争议
    #    （官方宣称的涨幅、工艺定额与实际值的差异），属于"关于数据的事实"，
    #    本该跟数据一起走。
    #    写死在这里的后果：换一套数据（如自造的样例工作区）后，
    #    报告仍会印出针对**另一批数据**的说法——读者会看到一句
    #    与眼前数字毫无关系的断言。而且它是"看起来对"的错，校验器拦不住。
    fs.conflicts.extend(_data_conflicts())

    return _finalize_sources(fs)


# 口径说明文件：缺失即为空，不报错（老工作区没有这个文件也要能跑）
_CONFLICTS_FILE = RAW / "meta/口径说明.json"


def _data_conflicts() -> List[str]:
    """
    读 `raw/meta/口径说明.json` 里的 `已知不可自洽点`。

    结构：
        {"已知不可自洽点": ["...", "..."]}
    文件不在 / 解析失败 / 键缺失 → 返回空列表（**静默降级**）。
    这里不该因为"没有口径说明"而让取数失败——它只是补充信息。
    """
    if not _CONFLICTS_FILE.exists():
        return []
    try:
        data = json.loads(_CONFLICTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("已知不可自洽点") or []
    return [str(x) for x in items]


# ---------------------------------------------------------------------------

def _print(fs: FactSet) -> None:
    print("=" * 74)
    print(f"报告事实  {fs.product}  {fs.month}  （对比 {fs.prev_month} / {fs.year_ago_month}）")
    print("=" * 74)
    for k, v in fs.facts.items():
        f = f"   = {v.formula}" if v.formula else ""
        print(f"  {k:<22} {v.display:>12}{(' ' + v.unit) if v.unit else ''}{f}")
        if v.coord:
            print(f"  {'':<22} ↳ {v.coord}")
    if fs.missing:
        print("\n⚠️  数据缺失（**不是 0**，不得填零）:")
        for m in fs.missing:
            print(f"   · {m}")
    if fs.conflicts:
        print("\n⚠️  已知不可自洽点（写报告时须回避或标注）:")
        for c in fs.conflicts:
            print(f"   · {c}")


def main() -> int:
    ap = argparse.ArgumentParser(description="报告事实引擎（确定性取数）")
    ap.add_argument("--product", help="产品名")
    ap.add_argument("--month", default="2026-05", help="分析月份 YYYY-MM")
    ap.add_argument("--list-products", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="写入 JSON 文件")
    a = ap.parse_args()

    if a.list_products:
        for p in list_products():
            print(p)
        return 0
    if not a.product:
        ap.print_help()
        return 1

    fs = build(a.product, a.month)
    d = fs.to_dict()
    if a.out:
        Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                               encoding="utf-8", newline="\n")
        print(f"已写出 {a.out}（{len(fs.facts)} 条事实）")
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif not a.out:
        _print(fs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
