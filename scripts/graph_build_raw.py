# -*- coding: utf-8 -*-
"""
graph_build_raw.py — **从 raw/ 确定性构图**（不依赖 LLM，也不依赖 wiki/）

为什么需要这份脚本
------------------
`graph_build.py` 从 `wiki/` 的表格抽节点。它有一个前提：知识库已经用
LLM 编译过。没有 API Key / 没有编译时，`graph/graph.json` 不存在，前端
只能显示「图谱尚未构建」——功能明明写好了，却看不到。

这份脚本用**原始数据**构建一张可解释的骨架图：

    工厂   --生产------> 产品
    产品   --消耗------> 原材料
    产品   --成本构成--> 直接材料 / 直接人工 / 制造费用
    原材料 --对应行情--> 行情药材
    工厂   --拥有设备--> 设备（按设备编号）

每个节点与边都带 raw 坐标，点开能回到具体文件。它**不是** wiki 图谱的
替代品：wiki 存在时仍优先用 `graph_build.py`；这里只保证「没编译也有图」。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
ROOT = Path(os.getenv("LLM_WIKI_WS") or HERE.parent)
RAW = ROOT / "raw"
OUT = ROOT / "graph" / "graph.json"

COST_FILE = "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"
COST_FILE_2 = "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv"
MATERIAL_FILE = "csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"
MARKET_FILE = "csv/market_data/药材市场价格行情_2026年上半年.csv"
EQUIPMENT_FILE = "pdf/pharma_docs/车间设备清单_中药一厂.txt"

EQUIP_NAMES = {
    "TQ": "提取/浓缩", "PZ": "配制", "GZ": "灌装", "MJ": "灭菌",
    "DJ": "灯检", "TB": "贴标", "ZH": "装盒", "FJ": "纯化水",
    "KL": "颗粒剂", "JN": "胶囊剂",
}


# ---------------------------------------------------------------------------
# 读 CSV：优先 UTF-8-BOM，失败再退回 GBK
# ---------------------------------------------------------------------------
def _read_csv(rel: str) -> List[Dict[str, str]]:
    p = RAW / rel
    if not p.exists():
        return []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with io.open(p, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    return []


def _node(nodes: Dict[str, Dict[str, Any]], nid: str, ntype: str, label: str,
          attrs: Optional[Dict[str, Any]] = None, coord: str = "") -> None:
    if nid not in nodes:
        nodes[nid] = {"id": nid, "type": ntype, "label": label,
                      "attrs": attrs or {}, "coord": coord}


def _parse_equipment(text: str) -> List[Tuple[str, str, str]]:
    """
    从设备清单派生文本里解析 (设备编号, 车间, 原始行)。

    ⚠️ PDF 表格被拆成多行：编号可能是
        EQ-   /  TQ-  /  001
    也可能是
        EQ-KL- / 001
    所以不能只用一个正则，必须看「当前行 + 后续两行」。
    设备中文名在 PDF 里被列宽切碎，这里不强行拼接——用编号前缀归类，
    比编造一个错误的全名更诚实。
    """
    lines = text.splitlines()
    out: List[Tuple[str, str, str]] = []
    seen = set()
    section = ""
    for i, line in enumerate(lines):
        msec = re.match(r"^[一二三四五六七八九十]+、(.+?)设备", line.strip())
        if msec:
            section = msec.group(1).strip()
        if "EQ-" not in line:
            continue

        prefix = None
        m = re.search(r"EQ-([A-Z]{2,3})-", line)
        if m:
            prefix = m.group(1)

        nxt = [x.strip() for x in lines[i + 1:i + 3] if x.strip()]
        code = None
        pending = None
        for x in nxt:
            m2 = re.match(r"([A-Z]{2,3})-(\d{3})\b", x)
            if m2:
                code = f"EQ-{m2.group(1)}-{m2.group(2)}"
                break
            m2b = re.match(r"([A-Z]{2,3})-\s", x)
            if m2b:
                pending = m2b.group(1)
                continue
            m3 = re.match(r"(\d{3})\b", x)
            if m3 and (pending or prefix):
                code = f"EQ-{pending or prefix}-{m3.group(1)}"
                break

        if code and code not in seen:
            seen.add(code)
            out.append((code, section, line.strip()))
    return out


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build(verbose: bool = True) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    seen_edges = set()
    warnings: List[str] = []

    def edge(src: str, dst: str, relation: str, coord: str = "") -> None:
        key = (src, dst, relation)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({"src": src, "dst": dst, "relation": relation,
                      "attrs": {}, "coord": coord})

    # ---- 产品 / 工厂 -------------------------------------------------------
    cost_rows = _read_csv(COST_FILE)
    if not cost_rows:
        warnings.append(f"未找到或为空：raw/{COST_FILE}")
    products: Dict[str, str] = {}
    for r in cost_rows:
        name = (r.get("产品名称") or "").strip()
        if name and name not in products:
            products[name] = (r.get("产品规格") or "").strip()

    if not products:
        # 兜底：从原材料明细里找产品名
        for r in _read_csv(MATERIAL_FILE):
            name = (r.get("产品名称") or "").strip()
            if name and name not in products:
                products[name] = (r.get("产品规格") or "").strip()

    for name, spec in products.items():
        _node(nodes, name, "product", name,
              {"规格": spec} if spec else {},
              f"[raw/{COST_FILE}:产品名称:{name}]")

    factories = []
    if cost_rows:
        factories.append("中药一厂")
    rows2 = _read_csv(COST_FILE_2)
    if rows2:
        factories.append("中药二厂")
    for f in factories:
        _node(nodes, f, "factory", f, {}, f"[raw/{COST_FILE}:工厂:{f}]")
        for name in products:
            edge(f, name, "生产", f"[raw/{COST_FILE}:产品名称:{name}]")

    # ---- 成本要素 ----------------------------------------------------------
    cost_elements = [
        ("直接材料", "直接材料(元/盒)"),
        ("直接人工", "直接人工(元/盒)"),
        ("制造费用", "制造费用(元/盒)"),
    ]
    for label, col in cost_elements:
        cid = f"成本:{label}"
        _node(nodes, cid, "cost", label, {}, f"[raw/{COST_FILE}:{col}]")
        for name in products:
            edge(name, cid, "成本构成", f"[raw/{COST_FILE}:{col}:{name}]")

    # ---- 原材料 ------------------------------------------------------------
    material_rows = _read_csv(MATERIAL_FILE)
    if not material_rows:
        warnings.append(f"未找到或为空：raw/{MATERIAL_FILE}")
    mat_seen = set()
    for r in material_rows:
        product = (r.get("产品名称") or "").strip()
        material = (r.get("原材料名称") or "").strip()
        if not product or not material:
            continue
        if material not in mat_seen:
            mat_seen.add(material)
            _node(nodes, material, "material", material,
                  {"单位消耗成本": (r.get("单位消耗成本(元/盒)") or "").strip()},
                  f"[raw/{MATERIAL_FILE}:原材料名称:{material}]")
        if product in products:
            edge(product, material, "消耗",
                 f"[raw/{MATERIAL_FILE}:{product}&{material}]")

    # ---- 行情药材 ----------------------------------------------------------
    market_rows = _read_csv(MARKET_FILE)
    if not market_rows:
        warnings.append(f"未找到或为空：raw/{MARKET_FILE}")
    market_names = set()
    for r in market_rows:
        name = (r.get("药材名称") or "").strip()
        if not name:
            continue
        market_names.add(name)
        mid = f"行情:{name}"
        _node(nodes, mid, "market", f"{name}行情",
              {"5月价格": (r.get("5月价格") or "").strip(),
               "趋势": (r.get("趋势分析") or "").strip()[:60]},
              f"[raw/{MARKET_FILE}:药材名称:{name}]")
        if name in mat_seen:
            edge(name, mid, "对应行情", f"[raw/{MARKET_FILE}:药材名称:{name}]")

    # ---- 设备 --------------------------------------------------------------
    eq_path = RAW / EQUIPMENT_FILE
    if eq_path.exists():
        for code, section, raw_line in _parse_equipment(
                eq_path.read_text(encoding="utf-8", errors="replace")):
            prefix = code.split("-")[1] if code.count("-") >= 2 else ""
            label = f"{EQUIP_NAMES.get(prefix, '设备')} {code}"
            _node(nodes, code, "equipment", label,
                  {"车间": section} if section else {},
                  f"[raw/{EQUIPMENT_FILE}:设备编号:{code}]")
            if "中药一厂" in factories:
                edge("中药一厂", code, "拥有设备",
                     f"[raw/{EQUIPMENT_FILE}:设备编号:{code}]")
    else:
        warnings.append(f"未找到：raw/{EQUIPMENT_FILE}")

    if not nodes:
        return {"generated_from": "raw/（没有可构图的数据）", "nodes": [],
                "edges": [], "stats": {"nodes": 0, "edges": 0,
                                       "by_type": {}, "by_relation": {}},
                "warnings": warnings or ["raw/ 下没有可用的数据文件"],
                "errors": ["无法从 raw/ 构图"]}

    by_type: Dict[str, int] = {}
    for n in nodes.values():
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    by_rel: Dict[str, int] = {}
    for e in edges:
        by_rel[e["relation"]] = by_rel.get(e["relation"], 0) + 1

    doc = {
        "generated_from": "raw/（确定性构图，无需 LLM）",
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges),
                  "by_type": dict(sorted(by_type.items())),
                  "by_relation": dict(sorted(by_rel.items()))},
        "warnings": warnings,
        "errors": [],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")

    if verbose:
        st = doc["stats"]
        print(f"raw 图谱已建立：{st['nodes']} 节点 / {st['edges']} 边")
        print(f"  节点类型: {st['by_type']}")
        print(f"  边类型  : {st['by_relation']}")
        for w in warnings[:6]:
            print(f"  ⚠️  {w}")
    return doc


def main() -> int:
    doc = build()
    return 1 if doc.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
