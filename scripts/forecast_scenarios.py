# -*- coding: utf-8 -*-
# 来源：GitHub PR #3 "Add historical scenario cost forecast model"
# 作者：lym2006227-netizen；按其提交引入，保留算法实现。
#
# 注意：这是**外推**，预测值没有 raw 坐标；区间是历史情景的最小/最大范围，
# 不是统计置信区间。只用于首页预测卡片，不写入 6 章报告正文。
"""Estimate next month's unit cost from joint historical monthly scenarios."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

from console_io import ensure_utf8_stdout

BASELINE_MONTHS = 3
MAX_SCENARIOS = 5
MIN_HISTORY_MONTHS = 4
RAW_CSV = Path(__file__).resolve().parent.parent / "raw" / "csv"


def _read_rows(directory: Path, pattern: str):
    paths = sorted(directory.glob(pattern))
    if not paths:
        raise ValueError(f"找不到数据文件：{directory / pattern}")
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                yield path, row


def _month(value: str) -> date:
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError(f"月份必须采用 YYYY-MM 格式：{value}") from exc


def _month_label(month: date) -> str:
    return month.isoformat()[:7]


def _number(row: dict, column: str, *, positive: bool = False) -> float:
    try:
        value = float(row.get(column, "").replace(",", ""))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"缺少有效数值：{column}") from exc
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"数值不符合预测要求：{column} = {value}")
    return value


def _product_rows(directory: Path, pattern: str, product: str):
    for _, row in _read_rows(directory, pattern):
        if (row.get("产品名称") or "").strip() == product:
            yield _month((row.get("月份") or "").strip()), row


def _unique_rows(rows, label: str) -> dict:
    result = {}
    for key, row in rows:
        if key in result:
            raise ValueError(f"{label}存在重复记录：{key}")
        result[key] = row
    return result


def _recent_months(cost: dict, as_of: str | None) -> list[date]:
    if not cost:
        raise ValueError("该产品没有月度成本数据。")
    cutoff = _month(as_of) if as_of is not None else max(cost)
    months = []
    while cutoff in cost and len(months) <= MAX_SCENARIOS:
        months.append(cutoff)
        cutoff = (cutoff - timedelta(days=1)).replace(day=1)
    if len(months) < MIN_HISTORY_MONTHS:
        raise ValueError(f"预测需要至少 {MIN_HISTORY_MONTHS} 个连续实绩月，当前只有 {len(months)} 个。")
    return list(reversed(months))


def _market_prices(directory: Path, months: set[date], materials: set[str]) -> dict:
    prices = defaultdict(dict)
    for path, row in _read_rows(directory, "药材市场价格行情_*.csv"):
        name = (row.get("药材名称") or "").strip()
        if name not in materials:
            continue
        year = re.search(r"([0-9]{4})年", path.stem)
        if year is None:
            raise ValueError(f"药材行情文件名缺少年份：{path.name}")
        for month in months:
            column = f"{month.month}月价格"
            if month.year == int(year[1]) and (row.get(column) or "").strip():
                price = _number(row, column, positive=True)
                previous = prices[name].get(month)
                if previous is not None and previous != price:
                    raise ValueError(f"药材行情存在重复且不同的报价：{name} {_month_label(month)}")
                prices[name][month] = price
    return prices


def _centered_changes(values: list[float]) -> list[float]:
    changes = [current / previous for previous, current in zip(values, values[1:])]
    average_change = mean(changes)
    return [value / average_change for value in changes]


def forecast(
    product: str,
    *,
    cost_dir: Path = RAW_CSV / "cost_data",
    market_dir: Path = RAW_CSV / "market_data",
    as_of: str | None = None,
) -> dict:
    """Return a one-month forecast from equal-weight, joint historical scenarios.

    The optional as_of month is the last available actual month in a backtest.
    The range is the minimum and maximum of the scenario totals.
    """
    product = product.strip()
    cost = _unique_rows(
        _product_rows(cost_dir, "中药一厂_成本汇总_*.csv", product), "成本汇总"
    )
    months = _recent_months(cost, as_of)
    baseline_months = months[-BASELINE_MONTHS:]
    labor = _unique_rows(
        ((m, r) for m, r in _product_rows(cost_dir, "中药一厂_人工工时明细_*.csv", product)
         if m in months),
        "人工工时",
    )
    if set(months) - labor.keys():
        raise ValueError("预测窗口内缺少人工工时记录。")
    depreciation = _unique_rows(
        ((m, r) for m, r in _product_rows(cost_dir, "中药一厂_制造费用明细_*.csv", product)
         if m in baseline_months and (r.get("费用类别") or "").strip() == "折旧费"),
        "折旧费用",
    )
    if set(baseline_months) - depreciation.keys():
        raise ValueError("基准月份缺少折旧费用记录。")
    material_rows = _unique_rows(
        (((m, (r.get("原材料名称") or "").strip()), r)
         for m, r in _product_rows(cost_dir, "中药一厂_原材料消耗明细_*.csv", product)
         if m in baseline_months),
        "原材料成本",
    )
    material_by_month = defaultdict(dict)
    for (month, name), row in material_rows.items():
        if not name:
            raise ValueError(f"原材料名称为空：{_month_label(month)}")
        material_by_month[month][name] = _number(row, "单位消耗成本(元/盒)")
    for month in baseline_months:
        expected = _number(cost[month], "直接材料(元/盒)")
        actual = sum(material_by_month.get(month, {}).values())
        if month not in material_by_month or not math.isclose(
            actual, expected, rel_tol=0.0, abs_tol=0.005
        ):
            raise ValueError(f"材料明细缺失或与成本汇总不一致：{_month_label(month)}")

    names = sorted({name for values in material_by_month.values() for name in values})
    material_baseline = {
        name: mean(material_by_month[month].get(name, 0.0) for month in baseline_months)
        for name in names
    }
    prices = _market_prices(market_dir, set(months), set(names))
    price_factors = {
        name: _centered_changes([quotes[month] for month in months])
        for name, quotes in prices.items()
        if all(month in quotes for month in months)
    }
    volumes = [_number(cost[m], "产量(盒)", positive=True) for m in months]
    hours = [_number(labor[m], "总工时(小时)", positive=True) for m in months]
    labor_totals = [_number(labor[m], "直接人工总额(元)", positive=True) for m in months]
    volume_factors = _centered_changes(volumes)
    hours_factors = _centered_changes([h / q for h, q in zip(hours, volumes)])
    rate_factors = _centered_changes([pay / h for pay, h in zip(labor_totals, hours)])
    labor_baseline = mean(_number(cost[m], "直接人工(元/盒)") for m in baseline_months)
    fixed_baseline = mean(_number(depreciation[m], "单位费用(元/盒)") for m in baseline_months)
    overhead_baseline = mean(_number(cost[m], "制造费用(元/盒)") for m in baseline_months)
    if fixed_baseline > overhead_baseline:
        raise ValueError("基准折旧高于制造费用合计。")

    weight = 1 / len(volume_factors)
    scenarios = []
    for i, (previous, current) in enumerate(zip(months, months[1:])):
        material = sum(
            baseline * (price_factors[name][i] if name in price_factors else 1.0)
            for name, baseline in material_baseline.items()
        )
        labor_cost = labor_baseline * hours_factors[i] * rate_factors[i]
        overhead = overhead_baseline - fixed_baseline + fixed_baseline / volume_factors[i]
        scenarios.append({
            "from_month": _month_label(previous),
            "to_month": _month_label(current),
            "probability": weight,
            "material": material,
            "labor": labor_cost,
            "overhead": overhead,
            "unit_cost": material + labor_cost + overhead,
        })

    totals = [scenario["unit_cost"] for scenario in scenarios]
    cutoff = months[-1]
    next_month = (cutoff.replace(day=28) + timedelta(days=4)).replace(day=1)
    warnings = [f"历史情景范围来自 {len(scenarios)} 个等权情景，仅供趋势参考。"]
    unpriced = sorted(set(names) - price_factors.keys())
    if unpriced:
        warnings.append("以下材料缺少完整行情，沿用基准成本：" + "、".join(unpriced))
    return {
        "method": "历史情景概率模型",
        "product": product,
        "as_of": _month_label(cutoff),
        "forecast_month": _month_label(next_month),
        "expected_unit_cost": mean(totals),
        "component_expectations": {
            part: mean(scenario[part] for scenario in scenarios)
            for part in ("material", "labor", "overhead")
        },
        "scenario_range": {"lower": min(totals), "upper": max(totals)},
        "scenarios": scenarios,
        "warnings": warnings,
    }


def _main() -> None:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="历史情景法预测下月单位成本")
    parser.add_argument("--product", required=True, help="产品名称")
    parser.add_argument("--cost-dir", type=Path, default=RAW_CSV / "cost_data", help="成本明细目录")
    parser.add_argument("--market-dir", type=Path, default=RAW_CSV / "market_data", help="行业行情目录")
    parser.add_argument("--as-of", help="用于回测的实绩截止月（YYYY-MM）")
    args = parser.parse_args()
    try:
        result = forecast(args.product, cost_dir=args.cost_dir, market_dir=args.market_dir, as_of=args.as_of)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    _main()
