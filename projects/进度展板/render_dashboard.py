#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_dashboard.py — 进度展板动态渲染（接真实数据源）。

挂在总地图 N4/N5 · 进度展板自动刷新链（与 refresh_dashboard.py 互补）：
  - 本脚本：源 stamp 变化才重建 HTML；--force 可强制重建。
  - refresh_dashboard.py：兼容派单/收口与 cron 的薄入口，只调用本脚本。

数据源（真源）：
  1. wiki/ 下 L* 目录 → 篇数 / 总字数 / 深篇（≥6000字）统计
  2. ~/.hermes/scripts/inflight-check.py → 在途单 / 待收口单
  3. ~/.hermes/shared-tasks.db → 任务账本 done/claimed/pending
  4. raw/_task_orders/RAG全库整理执行总图-20261103.md → WBS 状态、批次、M 系列
  5. wiki/总网流程/OKR-2026Q4.md → O/KR/Initiatives（唯一目标真源，只读渲染）

目标追踪卡口径（照抄 OKR 文件「展板渲染规则」）：
  - O 不显示百分比，只显示「达标 KR 数 / 总 KR 数 + 定性判断」
  - KR 状态四态：⚪未测 / 🔴受阻 / 🟡进行中 / ✅达标；Confidence 与 Owner 独立字段
  - 护栏、改进曲线、工程分母、关键路径均读 OKR 真源；护栏不参与 KR 达标计数
  - 举措→KR 是矩阵：一条举措可挂多个 KR，渲染时在每个 KR 下重复出现

加权公式（透明可查，展板底部注明口径）：
  RAG 进度 = 0.30·wiki深度达标率 + 0.25·批次完成度
           + 0.25·主题WBS完成度 + 0.20·在途健康度
  多智能体 = 0.30·M系列完成度 + 0.30·账本完成率
           + 0.25·前端交付完成度 + 0.15·在途健康度

视觉：保留原有深色主题 / 卡片 / 进度条，仅替换数据来源。
"""
from __future__ import annotations

import datetime
import argparse
import html
import fcntl
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ---------- 路径常量 ----------
ROOT = Path("/mnt/c/MyData/Documents/wuyafang-l")
WIKI = ROOT / "wiki"
WBS = ROOT / "raw/_task_orders/RAG全库整理执行总图-20261103.md"
DASH = ROOT / "projects/进度展板/进度展板-RAG与多智能体协同-20261102.html"
DB = Path.home() / ".hermes/shared-tasks.db"
INFLIGHT = Path.home() / ".hermes/scripts/inflight-check.py"
OKR = WIKI / "总网流程/OKR-2026Q4.md"
STAMP = DASH.parent / ".dashboard-stamp.json"
# OKR 证据落点里出现的裸文件名/命令 → 本库内的可点击定位（只做路径映射，不改内容）
OKR_EVIDENCE_PATHS = {
    "rag/eval_ragas.py": ROOT / "rag/eval_ragas.py",
    "dispatch-guard.py": Path.home() / ".hermes/scripts/dispatch-guard.py",
    "jev_accept.py": Path.home() / ".hermes/scripts/jev_accept.py",
    "inflight-check.py": INFLIGHT,
}

DEEP_THRESHOLD = 6000          # 深篇字数阈值（沿用 build_kb.py）
VOLUME_TARGET = 200            # wiki 总篇数体量目标（软目标）

# ---------- 数据源 1：wiki L* 扫描 ----------
def scan_wiki() -> dict:
    """扫描 wiki/L* 目录，统计篇数/字数/深篇。"""
    total = 0
    deep = 0
    layers = {}
    if not WIKI.exists():
        return {"total": 0, "deep": 0, "layers": {}}
    for d in sorted(WIKI.iterdir()):
        if not d.is_dir() or not re.match(r"^L\d+", d.name):
            continue
        n = w = dp = 0
        for f in d.rglob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", txt, flags=re.S | re.I)
            txt = re.sub(r"<[^>]+>", " ", txt)
            words = (len(re.findall(r"[一-鿿]", txt))
                     + len(re.findall(r"[A-Za-z]{2,}", txt)))
            n += 1
            w += words
            if words >= DEEP_THRESHOLD:
                dp += 1
        layers[d.name] = {"n": n, "w": w, "deep": dp}
        total += n
        deep += dp
    return {"total": total, "deep": deep, "layers": layers}


# ---------- 数据源 1b：RAG 灌库库（chroma） ----------
CHROMA = Path.home() / "kb-local/rag/index/chroma.sqlite3"
BOOKS_TOTAL = 71


def scan_rag_db() -> dict:
    """读 chroma：总来源数 / books 已灌 / chunk 数。"""
    if not CHROMA.exists():
        return {"sources": 0, "books": 0, "chunks": 0, "books_pct": 0.0}
    try:
        con = sqlite3.connect(f"file:{CHROMA.as_posix()}?mode=ro", uri=True)
        cur = con.cursor()
        src = cur.execute(
            "SELECT count(DISTINCT string_value) FROM embedding_metadata WHERE key='source'"
        ).fetchone()[0]
        books = cur.execute(
            "SELECT count(DISTINCT string_value) FROM embedding_metadata "
            "WHERE key='source' AND string_value LIKE '%books%'"
        ).fetchone()[0]
        chunks = cur.execute("SELECT count(*) FROM embeddings").fetchone()[0]
        con.close()
    except Exception:
        return {"sources": 0, "books": 0, "chunks": 0, "books_pct": 0.0}
    return {"sources": src, "books": books, "chunks": chunks,
            "books_pct": round(books / BOOKS_TOTAL * 100, 1)}


# ---------- 数据源 1c：三源融合状态 ----------
def scan_fusion_status() -> dict:
    """扫描 wiki/L*，统计融合清单、WHY 三段缺口与工作快照。"""
    layers = {}
    if WIKI.exists():
        for d in sorted(WIKI.iterdir()):
            if not d.is_dir() or not re.match(r"^L\d+", d.name):
                continue
            # 正文排除融合清单与 .fusion-work 工作稿。
            md_files = [
                f for f in d.rglob("*.md")
                if not f.name.startswith("00-")
                and ".fusion-work" not in f.relative_to(d).parts
            ]
            miss3 = 0
            for f in md_files:
                try:
                    txt = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if not re.search(r"^#+\s*WHY", txt, flags=re.M):
                    miss3 += 1
            snapshot_dir = d / ".fusion-work"
            snapshot = (sum(1 for f in snapshot_dir.rglob("*") if f.is_file())
                        if snapshot_dir.exists() else 0)
            layers[d.name] = {
                "has_checklist": any(f.is_file() for f in d.glob("00-*.md")),
                "md_total": len(md_files),
                "miss3": miss3,
                "snapshot": snapshot,
            }
    return {
        "layers": layers,
        "checklists": sum(v["has_checklist"] for v in layers.values()),
        "miss3_total": sum(v["miss3"] for v in layers.values()),
        "snapshot_total": sum(v["snapshot"] for v in layers.values()),
    }


# ---------- 数据源 2：inflight ----------
def scan_inflight() -> dict:
    """调用 inflight-check.py，解析跑中 / 待收口数量。"""
    if not INFLIGHT.exists():
        return {"running": 0, "pending_close": 0, "raw": "inflight-check 不存在"}
    try:
        out = subprocess.run(
            [sys.executable, str(INFLIGHT)],
            capture_output=True, text=True, timeout=15,
            # 不复制/读取宿主环境（其中可能含凭据）；只注入递归保护与运行必需值。
            env={"DASHBOARD_RENDER_ACTIVE": "1", "HOME": "/home/wuyafang",
                 "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        text = out.stdout + out.stderr
    except Exception as e:
        return {"running": 0, "pending_close": 0, "raw": f"调用失败: {e}"}

    running = 0
    pending = 0
    m = re.search(r"跑中\((\d+)\)", text)
    if m:
        running = int(m.group(1))
    m = re.search(r"待收口\((\d+)\)", text)
    if m:
        pending = int(m.group(1))
    return {"running": running, "pending_close": pending, "raw": text.strip()}


# ---------- 数据源 3：shared-tasks.db ----------
def scan_tasks() -> dict:
    """读任务账本状态分布。"""
    if not DB.exists():
        return {"total": 0, "done": 0, "claimed": 0, "pending": 0}
    try:
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        rows = dict(con.execute(
            "SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall())
        con.close()
    except Exception:
        return {"total": 0, "done": 0, "claimed": 0, "pending": 0}
    done = rows.get("done", 0)
    claimed = rows.get("claimed", 0)
    pending = rows.get("pending", 0)
    return {"total": done + claimed + pending,
            "done": done, "claimed": claimed, "pending": pending}


# ---------- 数据源 4：执行总图 WBS ----------
def scan_wbs() -> dict:
    """从执行总图解析批次 / 主题 WBS / M 系列 / 前端交付项。"""
    if not WBS.exists():
        return {"batches": [], "topics": [], "m_series": [], "frontend": []}
    txt = WBS.read_text(encoding="utf-8", errors="ignore")

    # 批次（去重：批1 出现两次，取最新状态 🔄）
    batches: dict[str, str] = {}
    for m in re.finditer(r"###\s+批(\d+)\s*·\s*([^✅🔄⏳\n]+?)\s*(✅|🔄|⏳)", txt):
        num, name, sym = m.group(1), m.group(2).strip(), m.group(3)
        batches[num] = sym  # 后出现的覆盖前面的（批1 最新在"剩余主题 ⏳"）
    batch_list = [(f"批{k}", v) for k, v in sorted(batches.items(), key=lambda x: int(x[0]))]

    # 主题 WBS（表格行 ✅/🔄/⏳）
    topics = []
    for line in txt.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        sym = next((sy for sy in ("✅", "🔄", "⏳", "❌")
                    if any(sy in c for c in cells)), None)
        if not sym:
            continue
        # 跳过表头分隔行
        if re.match(r"^[\s\-:|]+$", cells[0]):
            continue
        # 取第一个非空且不含状态符号的较长 cell
        text = ""
        for c in cells:
            c_clean = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", c).strip()
            if c_clean and sym not in c_clean and len(c_clean) > 2:
                text = c_clean
                break
        if text:
            topics.append((sym, text))

    # M 系列（M1-M4，多智能体协同线）
    m_series = []
    # 匹配 **M1 xxx：✅** 或 ### 多智能体协同线 · M4 xxx ✅
    for m in re.finditer(r"\b(M\d+)\b[^✅🔄⏳\n]{0,60}?(✅|🔄|⏳)", txt):
        num, sym = m.group(1), m.group(2)
        if (num, sym) not in m_series:
            m_series.append((num, sym))
    # 去重按 M 编号保留第一个状态
    seen = {}
    for num, sym in m_series:
        if num not in seen:
            seen[num] = sym
    m_series = sorted(seen.items(), key=lambda x: int(x[0][1:]))

    # 前端交付三件套（看板 / 选型 / ACP）
    # 执行总图未列时，回退读旧展板 HTML；再退化到全 ⏳ 兜底
    frontend = []
    for line in txt.splitlines():
        s = line.strip()
        for kw in ("前端看板", "选型决策", "ACP 协议", "ACP协议"):
            if kw in s:
                sym = next((sy for sy in ("✅", "🔄", "⏳") if sy in s), None)
                if sym:
                    frontend.append((kw, sym))
    seen_f = {}
    for kw, sym in frontend:
        if kw not in seen_f:
            seen_f[kw] = sym
    frontend = sorted(seen_f.items())

    return {"batches": batch_list, "topics": topics,
            "m_series": m_series, "frontend": frontend}


def scan_fusion(wbs_text: str) -> dict:
    """融合进度独立于 WBS；L4 仅认明确完成且有实测凭据的主题。"""
    states = {}
    b32_status = "未标注"
    b32_rows = {}
    sections = re.finditer(r"^###\s+([^\n]+)\n(.*?)(?=^#{1,3}\s|\Z)",
                           wbs_text, flags=re.M | re.S)
    for section in sections:
        title, body = section.groups()
        if re.search(r"批1\s*·\s*L4", title):
            default = next((s for s in ("🔄", "⏳", "✅") if s in title), "⏳")
            topics = re.finditer(
                r"^-\s+\*\*B1\.(0?[1-8])\b([^\n]*)\n?(.*?)(?=^-\s+\*\*B1\.|\Z)",
                body, flags=re.M | re.S)
            for topic in topics:
                num, heading, evidence = topic.groups()
                status = next((s for s in ("🔄", "⏳", "✅") if s in heading), default)
                if status == "✅":
                    verified = any(
                        re.search(r"(?:实测|验收凭据|收口证据)\s*[（(]?[^：:\n]*[：:]", line)
                        and not re.search(r"尚未|未取得|未获得|未核实|未完成|待补|缺少|无实测", line)
                        for line in evidence.splitlines()
                    )
                    if not verified:
                        status = "🔄"
                states[int(num)] = status
        if re.search(r"批3\b", title):
            marker = re.search(r"B3\.2\s+三源融合与替换\s*[：:]\s*([✅🔄⏳])", body)
            if marker:
                b32_status = marker.group(1)
            for line in body.splitlines():
                article = re.match(r"\|\s*B3\.2-(\d+)\b.*?\|\s*([✅🔄⏳])\s*\|", line)
                if article:
                    b32_rows[article.group(1)] = article.group(2)
    done = sum(s == "✅" for s in states.values())
    running = sum(s == "🔄" for s in states.values())
    return {
        "l4": {"done": done, "running": running, "todo": 8 - done - running,
               "total": 8, "pct": round(done / 8 * 100, 1)},
        "b32": {"done": sum(s == "✅" for s in b32_rows.values()),
                "total": len(b32_rows), "status": b32_status},
    }


# ---------- 数据源 5：OKR 目标真源 ----------
OKR_KR_STATUSES = {
    "⚪": {"key": "untested", "label": "未测", "cls": "okr-untested"},
    "🔴": {"key": "blocked", "label": "受阻", "cls": "okr-blocked"},
    "🟡": {"key": "wip", "label": "进行中", "cls": "okr-wip"},
    "✅": {"key": "done", "label": "达标", "cls": "okr-done"},
}
OKR_QUALITATIVE = {
    "untested": "无基线，不知道好坏",
    "blocked": "测得出但不达标或有前置坏掉",
    "wip": "有进展未达标",
    "done": "已达标",
}
_KR_ID = re.compile(r"^\*\*\s*(KR[0-9.]+)\s*\*\*\s*")
_INIT_ID = re.compile(r"^(I[0-9.]+)\s+(.*)$", flags=re.S)
_REF_FIX = re.compile(r"\bKR-VS-KP\b")
# [2026-11-10 举措结构化] 自由文本 → 结构化块的字段正则
_INIT_BLOCK_ID = re.compile(r"^- id:\s*(I[0-9.]+)\s*$", flags=re.M)
_INIT_FIELD = re.compile(r"^  (desc|progress|blocking|evidence|owner):\s*(.*)$")
_INIT_SUP_KR = re.compile(r"^    - kr:\s*(KR[0-9.]+)\s*#?\s*(?:role=(\S+))?\s*(?:weight=([0-9.]+))?")
_INIT_DF = re.compile(r"^    (in|out):\s*\[(.*)\]\s*$")
_DF_TOKEN = re.compile(r"([^\s,()]+)\((auto|manual)\)")
# [2026-11-10 O2 精简] 已删除的 KR 编号，不渲染死锚点
_RETIRED_KRS = {"KR2.5", "KR2.6", "KR2.7"}


def _md_strip(s: str) -> str:
    """行内 markdown → 纯文本（**粗体** / `代码` / [链接](url) 保留可见文字）。"""
    s = _REF_FIX.sub("KR", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\w)`([^`]+)`(?!\w)", r"\1", s)
    return re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", s).strip()


def _md_inline(s: str) -> str:
    """行内 markdown → 富文本 HTML（粗体/代码），其余转义。保持真源文案原样。"""
    s = _REF_FIX.sub("KR", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    parts = re.split(r"(\*\*[^*]+\*\*|(?<!\w)`[^`]+`(?!\w))", s)
    out = []
    for p in parts:
        if p.startswith("**") and p.endswith("**") and len(p) > 4:
            out.append(f"<b>{esc(p[2:-2])}</b>")
        elif p.startswith("`") and p.endswith("`") and len(p) > 2:
            out.append(f"<code>{esc(p[1:-1])}</code>")
        else:
            out.append(esc(p))
    return "".join(out)


def _table_cells(line: str) -> list:
    """markdown 表格行 → 去掉粗体标记后的单元格纯文本；非表格返回 []。"""
    s = line.strip()
    if not s.startswith("|"):
        return []
    s = s.replace(r"\|", "\x00")  # 转义竖线不当作分隔符
    cells = [c.strip().replace("\x00", "|") for c in s.strip().strip("|").split("|")]
    if not any(cells) or all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
        return []
    return cells


def _evidence_target(tok: str) -> str:
    """证据 token → file:// 链接；库内路径不存在返回空串（宁可不可点，不造死链）。"""
    t = tok.strip().strip("`")
    path = OKR_EVIDENCE_PATHS.get(t)
    if path is None and ("/" in t or t.endswith((".py", ".md", ".html", ".json"))):
        path = ROOT / t
    if path is None or not Path(path).exists():
        return ""
    return f"file://{Path(path).as_posix()}"


def okr_evidence_html(text: str) -> str:
    """证据落点渲染：可点路径→<a>，代码块/命令→<code>，其余原样加粗。"""
    if not text.strip():
        return '<span class="okr-evi-missing">⚠️ 真源未给证据落点</span>'
    out = []
    for seg in re.finditer(r"`[^`]+`|\*\*[^*]+\*\*|[^`*]+", text):
        p = seg.group(0)
        if p.startswith("`"):
            inner = p.strip("`")
            href = _evidence_target(inner)
            out.append(f'<code>{esc(inner)}</code>' if not href
                       else f'<a class="okr-evi" href="{href}"><code>{esc(inner)}</code></a>')
        elif p.startswith("**"):
            inner = p[2:-2]
            href = _evidence_target(inner)
            out.append(f'<a class="okr-evi" href="{href}">{esc(inner)}</a>' if href
                       else f"<b>{esc(inner)}</b>")
        elif p.strip():
            out.append(esc(p))
    return "".join(out)


def init_meta_html(it: dict) -> str:
    """[2026-11-10] 结构化举措元信息：进度、主/辅权重、数据流、blocking 标记。"""
    if not it.get("supports"):
        return ""
    parts = []
    sup = " ".join(f'<span class="okr-weight">{esc(s["role"])} {s["weight"]:g}</span>'
                   for s in it["supports"])
    parts.append(f'<span class="okr-meta">进度 {it["progress"]:g}% · 权重 {sup}</span>')
    if it.get("blocking"):
        parts.append('<span class="okr-tag">⛔ blocking</span>')
    df = it.get("dataflow", {})
    inb = "、".join(f'{esc(t["ref"])}({t["mode"]})' for t in df.get("in", [])) or "—"
    outb = "、".join(f'{esc(t["ref"])}({t["mode"]})' for t in df.get("out", [])) or "—"
    parts.append(f'<span class="okr-meta">数据流 in: {inb} | out: {outb}</span>')
    return "".join(parts)


def okr_initiative_html(text: str, slug: str) -> str:
    """举措文案：`命令/脚本` 存在就变可点，文中的 KrX.Y 变成跳该 KR 的锚点。"""
    out = []
    refs = r"`[^`]+`|KR(\d+\.\d+)|(?:^|(?<=[\s（(]))I(\d+\.\d+)(?=\b)"
    cursor = 0
    for seg in re.finditer(refs, text, flags=re.S):
        out.append(esc(text[cursor:seg.start()]))
        cursor = seg.end()
        p = seg.group(0)
        if not p:
            continue
        if p.startswith("`"):
            inner = p.strip("`")
            href = _evidence_target(inner)
            out.append(f'<a class="okr-evi" href="{href}"><code>{esc(inner)}</code></a>' if href
                       else f"<code>{esc(inner)}</code>")
        elif seg.group(1):
            kid = f"KR{seg.group(1)}"
            # [2026-11-10] 已删除的 KR 编号（O2 精简后 KR2.5/2.6/2.7 不复存在）不生成死锚点
            if kid in _RETIRED_KRS:
                out.append(f"<b>{kid}</b>")
            else:
                out.append(f'<a class="okr-chip" href="#{slug}-{kid.lower()}">{kid}</a>')
        elif seg.group(2):
            out.append(f'<a class="okr-chip" href="#{slug}-i{seg.group(2)}">I{seg.group(2)}</a>')
    out.append(esc(text[cursor:]))
    return "".join(out)


def okr_verdict(o: dict) -> dict:
    """O 的定性判断：只用真源里的 KR 状态计数，不算百分比。"""
    counts = {s: sum(1 for k in o["krs"] if k["sym"] == s) for s in OKR_KR_STATUSES}
    parts = [f"{OKR_KR_STATUSES[s]['label']} {n}" for s, n in counts.items() if n]
    if not o["total"]:
        return {"counts": counts, "value": "无可测 KR", "verdict": "真源未给出 KR，需先定 KR"}
    if counts["✅"] == o["total"]:
        verdict = "本 O 已达标"
    elif counts["🔴"]:
        verdict = f"{counts['🔴']} 条受阻，是当前主矛盾；其余按举措推进"
    elif counts["🟡"]:
        verdict = "有进展但未达标，按举措继续推进"
    else:
        verdict = "全部未测——先测基线再定幅度"
    return {"counts": counts,
            "value": f"达标 {counts['✅']} / {o['total']}",
            "verdict": f"{verdict}（{' · '.join(parts)}）"}


def _parse_okr_initiatives(body: str) -> list:
    """Initiatives 段 → [{id, text, knum, krefs: [KR id]}]，按真源顺序。"""
    # [2026-11-06 修举措丢失] 同一 O 下可能有多段 Initiatives（如 O2 先有「Initiatives（飞书团队协作）」
    # 再有「**Initiatives**」），原实现只取第一段、且标题必须恰为 **Initiatives**，导致举措漏挂。
    # 现改为收集全部段并合并。
    heads = list(re.finditer(r"^\*\*Initiatives[^*\n]*\*\*\s*\n((?:\s*[-*]\s+[^\n]*\n?)+)",
                             body, flags=re.M))
    if not heads:
        return []
    init = []
    seen = set()
    lines = []
    for m in heads:
        lines.extend(m.group(1).strip().splitlines())
    for line in lines:
        raw = line.strip().lstrip("-* ").strip()
        # [2026-11-10] 结构化块行（「- id: I1.1」）在 _parse_okr_init_blocks 解析，跳过
        if raw.startswith("id:"):
            continue
        km = _INIT_ID.match(raw)
        if not km:
            continue
        iid, text = km.group(1), km.group(2).strip()
        if iid in seen:
            continue
        seen.add(iid)
        krefs = []
        # [2026-11-06 修举措漏挂] 真源写法有「KR2.4/2.5」「KR2.4-2.7」等缩写，
        # 原正则只认完整「KR<n>.<n>」，导致 KR2.5 被判「没有任何举措支撑」。
        for km2 in re.finditer(r"KR\s*(\d+)\.(\d+)((?:\s*[/／、]\s*\d+(?:\.\d+)?)*)", text):
            major, minor, tail = km2.group(1), km2.group(2), km2.group(3) or ""
            cand = [f"KR{major}.{minor}"]
            for seg in re.split(r"[/／、]", tail):
                seg = seg.strip()
                if not seg:
                    continue
                cand.append(f"KR{seg}" if "." in seg else f"KR{major}.{seg}")
            for ref in cand:
                if ref not in krefs:
                    krefs.append(ref)
        basis = "explicit"
        if not krefs:
            basis = "prefix"
            krefs = [_prefix_kref(iid)]
        init.append({"id": iid, "text": text, "krefs": krefs,
                     "knum": iid[1:].split(".")[0], "basis": basis})
    return init


def _prefix_kref(iid: str) -> str:
    """真源未显式写 KR 时，按编号前缀兜底挂到同号 O 的第一个 KR。"""
    return f"KR{iid[1:].split('.')[0]}.1"


# ---------- [2026-11-10 举措结构化] 结构化块解析 + KR 推进度公式 ----------
def _parse_df_tokens(raw: str) -> list:
    """dataflow 串 'src(mode), src2(mode)' → [{'ref','mode'}]，留空返回 []。"""
    return [{"ref": m.group(1).strip(), "mode": m.group(2)}
            for m in _DF_TOKEN.finditer(raw)]


def _parse_okr_init_blocks(body: str) -> list:
    """结构化举措块（- id: …\n  desc: …）→ [{id, desc, supports, progress, …}]。"""
    blocks = []
    for m in _INIT_BLOCK_ID.finditer(body):
        start = m.end() + 1
        nxt = _INIT_BLOCK_ID.search(body, start)
        end = nxt.start() if nxt else len(body)
        seg = body[start:end]
        it = {"id": m.group(1), "desc": "", "supports": [], "progress": 0,
              "blocking": False, "evidence": "", "owner": "", "dataflow": {"in": [], "out": []}}
        mode = None
        for line in seg.splitlines():
            if line.strip() in ("support:", "dataflow:"):
                mode = line.strip()[:-1]
                continue
            fm = _INIT_FIELD.match(line)
            if fm:
                k, v = fm.group(1), fm.group(2).split("  #")[0].strip()
                if k == "desc":
                    it["desc"] = v
                elif k == "progress":
                    try:
                        it["progress"] = float(v)
                    except ValueError:
                        it["progress"] = 0.0
                elif k == "blocking":
                    it["blocking"] = v.lower() == "true"
                elif k == "evidence":
                    it["evidence"] = v
                elif k == "owner":
                    it["owner"] = v
                continue
            sm = _INIT_SUP_KR.match(line)
            if sm and mode == "support":
                it["supports"].append({"kr": sm.group(1), "role": sm.group(2) or "主",
                                       "weight": float(sm.group(3)) if sm.group(3) else 0.0})
                continue
            dm = _INIT_DF.match(line)
            if dm and mode == "dataflow":
                it["dataflow"][dm.group(1)] = _parse_df_tokens(dm.group(2))
        if it["id"]:
            blocks.append(it)
    return blocks


def kr_progress(kr: dict, init_by_id: dict) -> dict:
    """KR 推进度（只在展板侧算，不回写真源）：
    推进度 = Σ(举措进度×权重) / Σ(权重)；
    blocking 门：任一 blocking 举措未到 100%，KR 推进度上限 = 该举措进度（取最小）。
    返回 {pct, cap, blocking_caps, weight_sum}。"""
    num = den = 0.0
    caps = []
    for iid in kr["inits"]:
        it = init_by_id.get(iid)
        if not it:
            continue
        sup = next((s for s in it.get("supports", []) if s["kr"] == kr["id"]), None)
        w = sup["weight"] if sup else 0.0
        num += it["progress"] * w
        den += w
        if it["blocking"] and it["progress"] < 100:
            caps.append((it["id"], it["progress"]))
    pct = (num / den) if den > 0 else None
    cap = min((c[1] for c in caps), default=None)
    if pct is not None and cap is not None:
        pct = min(pct, cap)
    return {"pct": round(pct, 1) if pct is not None else None,
            "cap": cap, "blocking_caps": caps, "weight_sum": round(den, 4)}


def validate_initiatives(okr: dict) -> list:
    """结构化举措校验：每条贡献权重和 == 1.0（±0.005）、in 字段非空。返回问题清单。"""
    issues = []
    for iid, it in sorted(okr.get("init_by_id", {}).items()):
        wsum = round(sum(s["weight"] for s in it.get("supports", [])), 4)
        if it.get("supports") and abs(wsum - 1.0) > 0.005:
            issues.append(f"⚠️ {iid} 贡献权重和 {wsum} ≠ 1.0（需归一化）")
        if not it.get("dataflow", {}).get("in"):
            issues.append(f"⚠️ {iid} 数据流 in 字段为空（需非空）")
    return issues


def _parse_okr_register(txt: str, title: str) -> list:
    """读取指定二级标题中的首张登记表，不将护栏等表混入 KR。"""
    section = re.search(r"^##\s+" + re.escape(title) + r"\s*\n(.*?)(?=^##\s|\Z)",
                        txt, flags=re.M | re.S)
    if not section:
        return []
    header, rows = None, []
    for line in section.group(1).splitlines():
        cells = _table_cells(line)
        if not cells:
            if header and rows and not line.strip().startswith("|"):
                break
            continue
        if header is None:
            header = [_md_strip(c) for c in cells]
        else:
            rows.append(dict(zip(header, cells + [""] * max(0, len(header) - len(cells)))))
    return rows


def scan_okr() -> dict:
    """解析 OKR 真源：O/KR/举措、四态、护栏、里程碑与口径登记表。"""
    result = {"exists": False, "path": OKR, "os": [], "legend": [],
              "issues": [], "knum": {}, "krefs": {}, "init_by_id": {},
              "link_basis_tags": {}, "guardrails": [], "milestones": [],
              "measurement_notes": []}
    if not OKR.exists():
        result["issues"].append(f"OKR 真源不存在：{OKR}")
        return result
    txt = OKR.read_text(encoding="utf-8", errors="ignore")
    txt = re.sub(r"\A---\n.*?\n---\n", "", txt, flags=re.S)  # 去 frontmatter
    result["exists"] = True
    result["guardrails"] = _parse_okr_register(txt, "质量红线/护栏")
    result["milestones"] = _parse_okr_register(txt, "关键路径/里程碑")
    result["measurement_notes"] = _parse_okr_register(txt, "度量口径与快照说明")

    # [2026-11-06 修展板漏目标] 标题允许「O1（降级·次要）：…」这类括号后缀，
    # 原正则要求 O\d+ 后紧跟冒号，导致 O1/O2 整块被跳过（展板只显示 O3、KR2.x 全不显示）。
    bodies = re.findall(r"^##\s+(O\d+)\s*(?:（[^）\n]*）|\([^)\n]*\))?\s*[：:]\s*([^\n]+)\n(.*?)(?=^##\s|\Z)",
                        txt, flags=re.M | re.S)
    for oid, oname, body in bodies:
        krm = re.search(r"^\*\*为什么\*\*\s*[：:]\s*(.+)$", body, flags=re.M)
        o = {"id": oid, "name": oname.strip(),
             "why": _md_strip(krm.group(1)) if krm else ""}
        if not o["why"]:
            result["issues"].append(f"{oid} 缺「为什么」一行")

        knum = oid[1:]
        krs = []
        header = None
        for line in body.splitlines():
            if not line.strip().startswith("|"):
                header = None  # 离开表格；|---|---| 分隔行走 _table_cells 返回 []，表头保留
                continue
            cells = _table_cells(line)
            if not cells:  # |---| 分隔行：保留表头
                continue
            if header is None:
                header = [_md_strip(c) for c in cells]
                if "KR" not in header:
                    header = None  # 不是 KR 表（如 O 后的其它表）不接管
                continue
            if len(cells) < len(header):
                cells = cells + [""] * (len(header) - len(cells))
            raw = dict(zip(header, cells))
            cell = {k: _md_strip(v) for k, v in raw.items()}
            kid_m = _KR_ID.match(raw["KR"])
            if not kid_m:
                continue
            kid = kid_m.group(1)
            st = (cell.get("状态") or "").strip()
            sym = next((s for s in OKR_KR_STATUSES if st.startswith(s)), None)
            if sym is None:
                sym = next((s for s in OKR_KR_STATUSES if s in st), None)
            if sym is None:
                sym = next((s for s in OKR_KR_STATUSES if s in line), "⚪")
            evi = (raw.get("证据落点") or "").strip()  # 证据里带 `代码`，保留原样给渲染器
            kr = {
                "id": kid, "knum": kid[2:].split(".")[0],
                "title": _md_strip(raw["KR"][kid_m.end():]),
                "type": cell.get("类型", "").strip(),
                "target": cell.get("目标值", "").strip(),
                "current": cell.get("当前值", "").strip(),
                "status_raw": st or "未标注",
                "confidence": cell.get("信心", "").strip(),
                "owner": cell.get("Owner", "").strip(),
                "sym": sym,
                "state": OKR_KR_STATUSES[sym]["key"],
                "evidence": evi,
                "inits": [],
            }
            if not evi:
                result["issues"].append(f"{kid} 缺证据落点（Google Playbook 硬要求）")
            if not re.match(r"^[高中低][：:]\s*\S", kr["confidence"]):
                result["issues"].append(f"{kid} 缺有效信心（高/中/低及依据）")
            if not kr["owner"]:
                result["issues"].append(f"{kid} 缺 Owner")
            krs.append(kr)
            if kid in result["krefs"]:
                result["issues"].append(f"KR 编号重复：{kid}")
            result["krefs"][kid] = oid
        if not krs:
            result["issues"].append(f"{oid} 下没有解析到 KR 表")
        o["krs"] = krs
        o["initiatives"] = _parse_okr_initiatives(body)
        # [2026-11-10] 结构化举措块与自由文本并存时合并（id 去重，结构化优先）
        blocks = _parse_okr_init_blocks(body)
        by_id = {it["id"]: it for it in o["initiatives"]}
        for b in blocks:
            legacy = by_id.get(b["id"])
            if legacy is None:
                o["initiatives"].append({"id": b["id"], "text": b["desc"], "krefs": [],
                                         "knum": b["id"][1:].split(".")[0], "basis": "structured"})
            else:
                legacy["text"] = b["desc"]
                legacy["basis"] = "structured"
                legacy["krefs"] = [s["kr"] for s in b["supports"]]
                if not legacy["krefs"]:
                    legacy["basis"] = "prefix"
                    legacy["krefs"] = [_prefix_kref(b["id"])]
            b["text"] = b["desc"]
            b["krefs"] = [s["kr"] for s in b["supports"]] or [_prefix_kref(b["id"])]
            b.setdefault("knum", b["id"][1:].split(".")[0])
            b.setdefault("basis", "structured" if b["supports"] else "prefix")
            by_id[b["id"]] = b
        o["initiatives"] = [by_id[i["id"]] for i in o["initiatives"]] or list(by_id.values())
        o["init_blocks"] = list(by_id.values())
        o["done"] = sum(k["state"] == "done" for k in krs)
        o["total"] = len(krs)
        result["os"].append(o)
        result["knum"][knum] = oid

    # 举措 → KR 关联（矩阵）：显式 KR 优先，其次编号前缀兜底
    for o in result["os"]:
        for it in o["initiatives"]:
            result["init_by_id"][it["id"]] = it
            for kref in it["krefs"]:
                if kref not in result["krefs"]:
                    result["issues"].append(f"{it['id']} 指向不存在的 {kref}")

    # 矩阵挂载 + 反向索引
    kr_by_id = {k["id"]: k for o in result["os"] for k in o["krs"]}
    for o in result["os"]:
        for it in o["initiatives"]:
            for kref in it["krefs"]:
                kr = kr_by_id.get(kref)
                if kr is not None and it["id"] not in kr["inits"]:
                    kr["inits"].append(it["id"])
            # 一对多：该举措的某个 KR 还被同 O 内另一条举措挂着（兜底挂载不算真矩阵）
            if it["basis"] == "explicit" and any(
                    sum(r in x["krefs"] for x in o["initiatives"] if x["basis"] == "explicit") > 1
                    for r in it["krefs"]):
                result["link_basis_tags"].setdefault("shared", []).append(it["id"])
            # 多对一：该举措自身挂多个 KR（真矩阵的另一半）
            if len(it["krefs"]) > 1:
                result["link_basis_tags"].setdefault("multi", []).append(it["id"])
            if it["basis"] == "prefix":
                result["link_basis_tags"].setdefault("prefix", []).append(it["id"])
        if o["total"] and not any(k["inits"] for k in o["krs"]):
            result["issues"].append(f"{o['id']} 的 KR 没有任何举措支撑")
    for o in result["os"]:
        for k in o["krs"]:
            if not k["inits"]:
                result["issues"].append(f"{k['id']} 没有任何举措支撑")
    # [2026-11-10] 结构化举措校验：权重和==1.0、in 字段非空
    result["issues"].extend(validate_initiatives(result))

    # 四态图例（照抄 OKR「展板渲染规则」表）
    for line in txt.splitlines():
        cells = _table_cells(line)
        if len(cells) >= 3:
            sym = next((s for s in OKR_KR_STATUSES if s in cells[0]), None)
            if sym and "含义" not in cells[1]:
                result["legend"].append({"sym": sym, "meaning": _md_strip(cells[1]),
                                         "how": _md_strip(cells[2])})
    return result


FRONTEND_FALLBACK = [("前端看板", "⏳"), ("选型决策", "⏳"), ("ACP 协议", "⏳")]


def scan_frontend_from_html(old_html: str) -> list:
    """从旧展板 HTML 抓前端交付清单（含状态符号）。"""
    if not old_html:
        return []
    out = []
    for kw in ("前端看板", "选型决策", "ACP"):
        # 找 <li class="...">✅/🔄/⏳ ...kw... </li>
        m = re.search(
            r'<li[^>]*>([✅🔄⏳])[^<]*' + re.escape(kw) + r'[^<]*</li>',
            old_html,
        )
        if m:
            out.append((kw, m.group(1)))
    return out


# ---------- 进度计算 ----------
def _rate(items, weight_map=None) -> float:
    """(状态, ...) 列表 → 加权完成率。weight_map 默认 done=1 active=0.5 todo=0"""
    if not items:
        return 0.0
    wm = weight_map or {"✅": 1.0, "🔄": 0.5, "⏳": 0.0, "❌": 0.0}
    return sum(wm.get(s, 0.0) for s in items) / len(items)


def compute_rag(wiki: dict, wbs: dict, inflight: dict) -> dict:
    depth_rate = wiki["deep"] / max(1, wiki["total"])
    batch_rate = _rate([s for _, s in wbs["batches"]])
    topic_rate = _rate([s for s, _ in wbs["topics"]])
    inflight_total = inflight["running"] + inflight["pending_close"]
    health = 1.0 / (1.0 + inflight_total)

    progress = round(100 * (
        0.30 * depth_rate +
        0.25 * batch_rate +
        0.25 * topic_rate +
        0.20 * health
    ))
    return {
        "progress": max(0, min(100, progress)),
        "depth_rate": depth_rate,
        "batch_rate": batch_rate,
        "topic_rate": topic_rate,
        "health": health,
        "inflight_total": inflight_total,
    }


def compute_agent(wbs: dict, tasks: dict, inflight: dict) -> dict:
    m_rate = _rate([s for _, s in wbs["m_series"]])
    task_rate = tasks["done"] / max(1, tasks["total"])
    frontend_rate = _rate([s for _, s in wbs["frontend"]])
    inflight_total = inflight["running"] + inflight["pending_close"]
    health = 1.0 / (1.0 + inflight_total)

    progress = round(100 * (
        0.30 * m_rate +
        0.30 * task_rate +
        0.25 * frontend_rate +
        0.15 * health
    ))
    return {
        "progress": max(0, min(100, progress)),
        "m_rate": m_rate,
        "task_rate": task_rate,
        "frontend_rate": frontend_rate,
        "health": health,
    }


# ---------- HTML 渲染 ----------
CSS = """  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    line-height: 1.6;
    padding: 24px;
    max-width: 1200px;
    margin: 0 auto;
  }
  h1 {
    font-size: 22px;
    color: #f0f6fc;
    border-bottom: 2px solid #30363d;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }
  h1 .date { font-size: 14px; color: #8b949e; font-weight: normal; margin-left: 12px; }
  .nav { background: rgba(255, 255, 255, 0.06); border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; margin-bottom: 24px; }
  .nav a { color: #c4b5fd; text-decoration: none; overflow-wrap: anywhere; }
  .nav a:hover, .nav a:focus-visible { color: #ddd6fe; text-decoration: underline; }
  .project-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .project-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 16px;
  }
  .project-title { font-size: 18px; color: #f0f6fc; font-weight: 600; }
  .project-stage {
    font-size: 13px;
    padding: 3px 10px;
    border-radius: 12px;
    background: #1f6feb33;
    color: #58a6ff;
    border: 1px solid #1f6feb66;
  }
  .progress-row { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .progress-bar-wrap { flex: 1; height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); border-radius: 4px; transition: width 0.3s; }
  .progress-num { font-size: 14px; color: #58a6ff; font-weight: 600; min-width: 48px; text-align: right; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 18px; }
  .stat-box { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 12px; text-align: center; }
  .stat-box .label { font-size: 11px; color: #8b949e; margin-bottom: 4px; }
  .stat-box .value { font-size: 18px; color: #f0f6fc; font-weight: 600; }
  .stat-box .value.warn { color: #d29922; }
  .stat-box .value.good { color: #3fb950; }
  .stat-box .value.bad { color: #f85149; }
  .wbs-title { font-size: 14px; color: #f0f6fc; font-weight: 600; margin-bottom: 10px; padding-left: 4px; }
  .wbs-list { list-style: none; margin-bottom: 16px; }
  .wbs-list li { padding: 6px 8px; border-left: 3px solid #30363d; margin-bottom: 4px; font-size: 13.5px; background: #0d1117; border-radius: 0 4px 4px 0; }
  .wbs-list li.done { border-left-color: #3fb950; }
  .wbs-list li.active { border-left-color: #d29922; }
  .wbs-list li.todo { border-left-color: #8b949e; }
  .next-action { background: #1f6feb11; border: 1px solid #1f6feb44; border-radius: 6px; padding: 12px; font-size: 13.5px; }
  .next-action strong { color: #58a6ff; }
  .next-action ul { margin-left: 20px; margin-top: 6px; }
  .checkin-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
  .checkin-section h2 { font-size: 16px; color: #f0f6fc; margin-bottom: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border: 1px solid #30363d; padding: 8px 10px; text-align: left; }
  th { background: #21262d; color: #f0f6fc; font-weight: 600; }
  td[contenteditable="true"]:hover { background: #1c2128; }
  td[contenteditable="true"]:focus { background: #1f6feb22; outline: 1px solid #1f6feb; }
  .btn { background: #238636; color: #fff; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; margin-bottom: 10px; }
  .btn:hover { background: #2ea043; }
  .btn.secondary { background: #30363d; }
  .btn.secondary:hover { background: #484f58; }
  .formula { font-size: 11px; color: #8b949e; line-height: 1.7; padding: 8px 12px; background: #0d1117; border-radius: 6px; margin-top: 8px; border: 1px dashed #30363d; }
  .formula b { color: #58a6ff; }
  /* ---- OKR 目标追踪卡 ---- */
  .okr-o { border: 1px solid #30363d; border-radius: 6px; padding: 14px 16px; margin-bottom: 16px; background: #0d1117; }
  .okr-o-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
  .okr-o-title { font-size: 15.5px; color: #f0f6fc; font-weight: 600; }
  .okr-o-title .okr-id { color: #c4b5fd; margin-right: 6px; }
  .okr-ratio { font-size: 14px; color: #58a6ff; font-weight: 600; border: 1px solid #1f6feb66; background: #1f6feb22; border-radius: 12px; padding: 2px 10px; white-space: nowrap; }
  #okr .project-header { flex-wrap: wrap; gap: 8px; }
  #okr .stats-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-bottom: 8px; }
  .okr-kr-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: 10px; align-items: start; margin-top: 10px; }
  .okr-kr { min-width: 0; overflow-wrap: anywhere; border-radius: 6px; padding: 10px 12px; border: 1px solid #30363d; background: #161b22; border-left-width: 5px; }
  .okr-kr.okr-untested { border-left-color: #6e7681; background: #6e76811f; }
  .okr-kr.okr-blocked  { border-left-color: #f85149; background: #f851491f; }
  .okr-kr.okr-wip      { border-left-color: #d29922; background: #d299221f; }
  .okr-kr.okr-done     { border-left-color: #3fb950; background: #3fb9501f; }
  .okr-kr-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
  .okr-kr-id { font-size: 13.5px; color: #f0f6fc; font-weight: 600; }
  .okr-kr-id .okr-knum { color: #8b949e; font-weight: normal; margin-left: 6px; }
  .okr-lamp { font-size: 12px; padding: 1px 8px; border-radius: 10px; white-space: nowrap; border: 1px solid #30363d; }
  .okr-lamp.okr-untested { background: #6e76814d; color: #c9d1d9; }
  .okr-lamp.okr-blocked  { background: #f8514966; color: #ffdcd7; }
  .okr-lamp.okr-wip      { background: #d2992266; color: #fff1c9; }
  .okr-lamp.okr-done     { background: #3fb95066; color: #d7ffe0; }
  .okr-kr-row { font-size: 12.5px; display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 6px; margin-top: 3px; }
  .okr-kr-row .k { color: #8b949e; }
  .okr-kr-row.evi code { background: #21262d; border-radius: 3px; padding: 0 4px; color: #a5d6ff; }
  .okr-kr-row.evi a code { color: #79c0ff; text-decoration: underline; }
  .okr-evi-missing { color: #d29922; }
  .okr-note, .okr-verdict { font-size: 12px; color: #8b949e; margin: 6px 0; }
  .okr-support { margin: 10px 0 0 8px; padding-left: 10px; border-left: 2px solid #c4b5fd55; }
  .okr-support-label { font-size: 12px; color: #c4b5fd; margin-bottom: 3px; }
  .okr-support-list { list-style: none; }
  .okr-init { padding: 4px 0; font-size: 12.5px; }
  .okr-init + .okr-init { border-top: 1px solid #30363d; }
  .okr-init-id { color: #c4b5fd; font-weight: 600; margin-right: 5px; }
  .okr-tag { font-size: 11px; color: #d29922; border: 1px solid #d2992244; border-radius: 8px; padding: 0 6px; white-space: nowrap; }
  .okr-init-text code { background: #21262d; border-radius: 3px; padding: 0 4px; color: #a5d6ff; }
  .okr-meta { display: block; font-size: 11px; color: #8b949e; margin-top: 2px; }
  .okr-weight { display: inline-block; background: #c4b5fd22; border: 1px solid #c4b5fd44; border-radius: 8px; padding: 0 5px; margin-right: 3px; }
  .okr-prog-row { font-size: 11px; font-weight: normal; margin-top: 2px; color: #79c0ff; }
  .okr-prog { white-space: nowrap; }
  .okr-divergence { border: 1px solid #d2992266; background: #d299221a; border-radius: 6px; padding: 6px 10px; font-size: 12px; margin: 6px 0; color: #ffd479; }
  a.okr-chip { color: #c4b5fd; text-decoration: none; border: 1px solid #c4b5fd55; border-radius: 8px; padding: 0 5px; font-size: 11.5px; }
  a.okr-chip:hover { background: #c4b5fd22; }
  .okr-legend { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 12px; font-size: 12px; }
  .okr-legend span.lamp { padding: 1px 8px; border-radius: 10px; border: 1px solid #30363d; }
  .okr-banner { border: 1px solid #d2992266; background: #d299221a; border-radius: 6px; padding: 10px 12px; font-size: 13px; margin-bottom: 14px; }
  .okr-banner ul { margin: 6px 0 0 20px; }
  .okr-banner.bad { border-color: #f8514966; background: #f851491a; }
  .okr-register { margin: 16px 0; padding: 12px; border: 1px solid #30363d; border-radius: 6px; }
  .okr-register h3 { font-size: 15px; color: #f0f6fc; }
  .okr-table-wrap { overflow-x: auto; margin-top: 8px; }
  .okr-register table { min-width: 840px; font-size: 12px; }
  .okr-register td { vertical-align: top; min-width: 90px; }
  .okr-register .okr-evi { color: #79c0ff; overflow-wrap: anywhere; }
  #okr-guardrails { border-left: 4px solid #f85149; background: #f851490a; }
  #okr [id] { scroll-margin-top: 12px; }
"""

STATUS_TO_CLASS = {"✅": "done", "🔄": "active", "⏳": "todo", "❌": "todo"}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_li(items, prefix_map=None) -> str:
    """items: [(sym, text)] → <li> HTML。prefix_map 用于转换符号。"""
    lines = []
    for sym, text in items:
        cls = STATUS_TO_CLASS.get(sym, "todo")
        show_sym = (prefix_map or {}).get(sym, sym)
        lines.append(f'    <li class="{cls}">{show_sym} {esc(text)}</li>')
    return "\n".join(lines)


def render_rag_card(wiki: dict, wbs: dict, inflight: dict, rag: dict, ragdb: dict,
                    fusion: dict, fusion_status: dict) -> str:
    l4, b32 = fusion["l4"], fusion["b32"]
    checklist_cls = ("good" if fusion_status["checklists"] >= 12
                     else "warn" if fusion_status["checklists"] > 0 else "bad")
    miss3_cls = "good" if fusion_status["miss3_total"] == 0 else "warn"
    snapshot_cls = "good" if fusion_status["snapshot_total"] == 0 else "warn"
    layers_html = "".join(
        f'<div class="stat-box"><div class="label">{esc(k)}</div>'
        f'<div class="value">{v["n"]}篇·深{v["deep"]}</div></div>'
        for k, v in list(wiki["layers"].items())[:4]
    )
    wbs_li = render_li([(s, t) for s, t in wbs["topics"]])
    return f"""<!-- ========== 项目一：RAG知识库重构 ========== -->
<div class="project-card">
  <div class="project-header">
    <div class="project-title">🔍 RAG知识库重构</div>
    <div class="project-stage">阶段：全量灌库补全 + 前端信息架构重构</div>
  </div>

  <div class="progress-row">
    <div class="progress-bar-wrap"><div class="progress-bar" style="width: {rag['progress']}%"></div></div>
    <div class="progress-num">{rag['progress']}%</div>
  </div>

  <div class="stats-grid">
    <div class="stat-box"><div class="label">wiki 总篇数</div><div class="value">{wiki['total']}</div></div>
    <div class="stat-box"><div class="label">深篇 (≥6k字)</div><div class="value good">{wiki['deep']}</div></div>
    <div class="stat-box"><div class="label">已灌来源/切片</div><div class="value">{ragdb['sources']} / {ragdb['chunks']}</div></div>
    <div class="stat-box"><div class="label">书籍灌库 {ragdb['books']}/{BOOKS_TOTAL}</div><div class="value warn">{ragdb['books_pct']}%</div></div>
    <div class="stat-box"><div class="label">深度达标率</div><div class="value">{rag['depth_rate']*100:.1f}%</div></div>
    <div class="stat-box"><div class="label">批次完成度</div><div class="value">{rag['batch_rate']*100:.1f}%</div></div>
    <div class="stat-box"><div class="label">主题 WBS 完成度</div><div class="value">{rag['topic_rate']*100:.1f}%</div></div>
    <div class="stat-box"><div class="label">在途单 (跑中+待收口)</div><div class="value warn">{inflight['running']}+{inflight['pending_close']}</div></div>
  </div>

  <div class="wbs-title">融合深化</div>
  <div class="stats-grid">
    <div class="stat-box"><div class="label">L4 八主题融合 · 已收口/总数</div><div class="value good">{l4['done']}/{l4['total']} · {l4['pct']:.1f}%</div><div class="label">🔄 执行中 {l4['running']} · ⏳ 待办 {l4['todo']}</div></div>
    <div class="stat-box"><div class="label">B3.2 三源融合 · 已完成篇数</div><div class="value good">{b32['done']}篇 / {b32['total']}篇</div><div class="label">状态：{esc(b32['status'])}</div></div>
    <div class="stat-box"><div class="label">融合清单</div><div class="value {checklist_cls}">{fusion_status['checklists']}/12 层</div></div>
    <div class="stat-box"><div class="label">缺三段</div><div class="value {miss3_cls}">{fusion_status['miss3_total']} 篇</div></div>
    <div class="stat-box"><div class="label">待清快照</div><div class="value {snapshot_cls}">{fusion_status['snapshot_total']} 个</div></div>
  </div>

  <div class="wbs-title">WBS 任务清单（{len(wbs['topics'])} 项）</div>
  <ul class="wbs-list">
{wbs_li}
  </ul>

  <div class="next-action">
    <strong>🚀 下一步动作：</strong>
    <ul>
      <li>推进批1 L4-RAG工程八主题融合（当前 🔄执行中）</li>
      <li>启动批3-批8 主题收敛（当前 ⏳待办）</li>
      <li>收口在途 {inflight['running']+inflight['pending_close']} 张单（跑中 {inflight['running']} + 待收口 {inflight['pending_close']}）</li>
    </ul>
  </div>

  <div class="formula">
    <b>进度口径</b>：0.30×深度达标率({rag['depth_rate']*100:.1f}%) + 0.25×批次完成度({rag['batch_rate']*100:.1f}%) + 0.25×主题WBS({rag['topic_rate']*100:.1f}%) + 0.20×在途健康({rag['health']*100:.1f}%) = <b>{rag['progress']}%</b>
  </div>
</div>
"""


def render_agent_card(wbs: dict, tasks: dict, inflight: dict, agent: dict) -> str:
    m_li = render_li([(s, f"{num} 多智能体协同线") for num, s in wbs["m_series"]])
    fe_li = render_li([(s, kw) for kw, s in wbs["frontend"]])
    return f"""<!-- ========== 项目二：多智能体协同前端 ========== -->
<div class="project-card">
  <div class="project-header">
    <div class="project-title">🤖 多智能体协同前端聊天可展示系统</div>
    <div class="project-stage">阶段：后端基础设施完成 · 前端看板开发中</div>
  </div>

  <div class="progress-row">
    <div class="progress-bar-wrap"><div class="progress-bar" style="width: {agent['progress']}%"></div></div>
    <div class="progress-num">{agent['progress']}%</div>
  </div>

  <div class="stats-grid">
    <div class="stat-box"><div class="label">M 系列完成度</div><div class="value good">{agent['m_rate']*100:.0f}%</div></div>
    <div class="stat-box"><div class="label">任务账本</div><div class="value">{tasks['total']}条</div></div>
    <div class="stat-box"><div class="label">done/claimed/pending</div><div class="value">{tasks['done']}/{tasks['claimed']}/{tasks['pending']}</div></div>
    <div class="stat-box"><div class="label">账本完成率</div><div class="value">{agent['task_rate']*100:.1f}%</div></div>
    <div class="stat-box"><div class="label">前端交付完成度</div><div class="value warn">{agent['frontend_rate']*100:.0f}%</div></div>
    <div class="stat-box"><div class="label">在途单</div><div class="value warn">{inflight['running']+inflight['pending_close']}</div></div>
  </div>

  <div class="wbs-title">M 系列工作包</div>
  <ul class="wbs-list">
{m_li}
  </ul>

  <div class="wbs-title">前端交付清单</div>
  <ul class="wbs-list">
{fe_li}
  </ul>

  <div class="next-action">
    <strong>🚀 下一步动作：</strong>
    <ul>
      <li>基于 shared-tasks.db 数据，开发前端看板+聊天流可视化界面</li>
      <li>完成 Flightdeck vs OpenHands Agent Canvas 选型决策</li>
      <li>启动 ACP 协议对接设计</li>
    </ul>
  </div>

  <div class="formula">
    <b>进度口径</b>：0.30×M系列({agent['m_rate']*100:.0f}%) + 0.30×账本完成率({agent['task_rate']*100:.1f}%) + 0.25×前端交付({agent['frontend_rate']*100:.0f}%) + 0.15×在途健康({agent['health']*100:.1f}%) = <b>{agent['progress']}%</b>
  </div>
</div>
"""


def render_okr_register(rows: list, anchor: str, title: str, note: str) -> str:
    """展现真源登记表；只渲染，不重算或推断约束与排程。"""
    if not rows:
        return (f'<section class="okr-register" id="{anchor}"><h3>{esc(title)}</h3>'
                '<p class="okr-note">真源未提供登记表，待补；不自行推断。</p></section>')
    headers = list(rows[0])
    head = ''.join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(
        '<td>' + (okr_evidence_html(row.get(h, '')) if h == "证据落点"
                  else _md_inline(row.get(h, ''))) + '</td>'
        for h in headers) + '</tr>' for row in rows)
    return (f'<section class="okr-register" id="{anchor}"><h3>{esc(title)}</h3>'
            f'<p class="okr-note">{esc(note)}</p><div class="okr-table-wrap">'
            f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></section>')


def render_okr_card(okr: dict) -> str:
    """单一主视图：O → KR(目标·当前·状态·证据) → 就地支撑举措。"""
    if not okr.get("exists"):
        return f"""<!-- ========== 项目三：OKR 目标追踪 ========== -->
<div class="project-card" id="okr">
  <div class="project-header"><div class="project-title">🎯 目标追踪（OKR 2026 Q4）</div></div>
  <div class="okr-banner bad">⚠️ OKR 真源未找到：{esc(str(okr.get('path')))} —— 目标区为空，不自行补全。</div>
</div>
"""
    slug = "okr"
    total_kr = sum(o["total"] for o in okr["os"])
    total_done = sum(o["done"] for o in okr["os"])
    total_counts = {s: sum(1 for o in okr["os"] for k in o["krs"] if k["sym"] == s)
                    for s in OKR_KR_STATUSES}
    involved = sorted({i for o in okr["os"] for k in o["krs"] for i in k["inits"]},
                      key=lambda x: (int(x[1:].split(".")[0]), int(x.split(".")[1])))
    prefix_ids = [x for x in okr["link_basis_tags"].get("prefix", []) if x in involved]
    lamps = "".join(
        f'<span class="lamp {OKR_KR_STATUSES[s]["cls"]}">{s} {OKR_KR_STATUSES[s]["label"]} '
        f'{total_counts[s]}</span>' for s in OKR_KR_STATUSES
    )

    blocks = []
    rendered_inits = set()
    for o in okr["os"]:
        v = okr_verdict(o)
        krs_html = ""
        for k in o["krs"]:
            st = OKR_KR_STATUSES[k["sym"]]
            # [2026-11-10] KR 推进度（公式算）+ 分歧警示（状态灯 vs 推进度双轨分离）
            kp = kr_progress(k, okr["init_by_id"])
            pct_html = ('<span class="okr-prog">推进度 {:.1f}%（公式）</span>'.format(kp["pct"])
                        if kp["pct"] is not None
                        else '<span class="okr-prog">推进度：无可算举措</span>')
            cap_html = ""
            if kp["blocking_caps"]:
                caps = "、".join(f"{iid} {p:g}%" for iid, p in kp["blocking_caps"])
                cap_html = f'<div class="okr-note">⛔ blocking 依赖门：{esc(caps)} 未到 100%，推进度已封顶</div>'
            divergence = ""
            if k["sym"] == "✅" and kp["pct"] is not None and kp["pct"] < 80:
                divergence = (f'<div class="okr-divergence">⚠️ 分歧警示：状态灯已达标，但举措汇总推进度 '
                              f'{kp["pct"]:g}% < 80%——请复核是举措数据未更新还是达标结论过早</div>')
            support_items = []
            for iid in k["inits"]:
                it = okr["init_by_id"][iid]
                # 首次出现保留原锚点，矩阵副本用 KR 限定，避免重复 DOM id。
                anchor = (f"{slug}-{iid.lower()}" if iid not in rendered_inits
                          else f"{slug}-{k['id'].lower()}-{iid.lower()}")
                rendered_inits.add(iid)
                tag = (' <span class="okr-tag">暂挂</span>' if it["basis"] == "prefix"
                       else f' <span class="okr-tag">↔ {len(it["krefs"])} KR</span>'
                       if len(it["krefs"]) > 1 else '')
                support_items.append(
                    f'<li class="okr-init" id="{anchor}">'
                    f'<span class="okr-init-id">{esc(iid)}</span>'
                    f'<span class="okr-init-text">{okr_initiative_html(it["text"], slug)}</span>'
                    f'{init_meta_html(it)}{tag}</li>')
            support = ('<ul class="okr-support-list">' + ''.join(support_items) + '</ul>'
                       if support_items else '<span class="okr-note">未标支撑举措</span>')
            krs_html += f"""      <div class="okr-kr {st['cls']}" id="{slug}-{k['id'].lower()}">
        <div class="okr-kr-head">
          <div class="okr-kr-id">{esc(k['id'])} {esc(k['title'])}<span class="okr-knum">{esc(k['type'])}</span></div>
          <div class="okr-lamp {st['cls']}">{k['sym']} {st['label']}<div class="okr-prog-row">{pct_html}</div></div>
        </div>
        {cap_html}{divergence}
        <div class="okr-kr-row"><span class="k">目标值</span><span>{_md_inline(k['target'])}</span></div>
        <div class="okr-kr-row"><span class="k">当前值</span><span>{_md_inline(k['current'])}</span></div>
        <div class="okr-kr-row"><span class="k">信心</span><span class="okr-confidence">信心：{esc(k['confidence'])}</span></div>
        <div class="okr-kr-row"><span class="k">Owner</span><span class="okr-owner">{esc(k['owner'])}</span></div>
        <div class="okr-kr-row evi"><span class="k">证据落点</span><span>{okr_evidence_html(k['evidence'])}</span></div>
        <div class="okr-support"><div class="okr-support-label">↳ 支撑</div>{support}</div>
      </div>
"""
        blocks.append(f"""  <div class="okr-o" id="{slug}-{o['id'].lower()}">
    <div class="okr-o-head">
      <div class="okr-o-title"><span class="okr-id">{esc(o['id'])}</span>{esc(o['name'])}</div>
      <div class="okr-ratio">{v['value']}</div>
    </div>
    <div class="okr-verdict">{esc(v['verdict'].split('；')[0].split('（')[0])}</div>
    <div class="okr-note">为什么：{esc(o['why'])}</div>
    <div class="okr-kr-grid">
{krs_html}    </div>
  </div>
""")

    issues_html = ""
    if okr["issues"]:
        items = "".join(f"<li>{esc(x)}</li>" for x in okr["issues"])
        issues_html = (f'<details class="okr-banner"><summary>⚠️ 真源待补 {len(okr["issues"])} 项</summary>'
                       f'<ul>{items}</ul></details>')

    guards_html = render_okr_register(
        okr.get("guardrails", []), "okr-guardrails", "质量红线/护栏（独立于 KR 打分）",
        "不计入 KR 达标数。触发即拦截 + 强制复盘：冻结受影响结论/依赖动作，实测纠正、留证、独立复核后恢复。"
        "改进下降不抵销违规；日观测不是周基线。此处为约束语义，本轮未实现自动拦截门禁。")
    milestones_html = render_okr_register(
        okr.get("milestones", []), "okr-milestones", "关键路径/里程碑",
        "测基线 → 定 KR 目标值 → 算进度 → 排计划。以下为计划检查点，未执行不算进展；前置未验收则后续阻塞。")
    measurement_html = render_okr_register(
        okr.get("measurement_notes", []), "okr-measurement-notes", "度量口径与快照说明",
        "原始快照、重渲染时的运行统计与账本登记分开显示；未测不补 0，不用全库分母替代工程主题分母。")

    return f"""<!-- ========== 项目三：OKR 目标追踪 ========== -->
<div class="project-card" id="okr">
  <div class="project-header">
    <div class="project-title">🎯 目标追踪（OKR 2026 Q4）</div>
    <div class="project-stage">
      <a href="file://{OKR.as_posix()}" style="color:#58a6ff;text-decoration:none">📄 OKR-2026Q4.md</a>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-box"><div class="label">O 总数</div><div class="value">{len(okr['os'])}</div></div>
    <div class="stat-box"><div class="label">KR 达标 / 总数</div><div class="value good">{total_done} / {total_kr}</div></div>
    <div class="stat-box"><div class="label">举措总数</div><div class="value">{len(involved)}</div></div>
  </div>

  <div class="okr-legend">{lamps}</div>
  <div class="okr-note">四色灯表示当前状态；Confidence（信心：高/中/低）是季度达成判断，不是状态灯或完成率。信心依据随实测复核，不因登记在跑而升档。</div>
  {f'<div class="okr-note">{len(prefix_ids)} 条举措未标 KR，按编号暂挂，关联待确认。</div>' if prefix_ids else ''}

{issues_html}
{''.join(blocks)}
{guards_html}
{milestones_html}
{measurement_html}
</div>
"""


CHECKIN_JS = """<script>
function addRow() {
  const tbody = document.querySelector('#checkinTable tbody');
  const tr = document.createElement('tr');
  tr.innerHTML = '<td contenteditable="true"></td><td contenteditable="true"></td><td contenteditable="true"></td><td contenteditable="true"></td>';
  tbody.appendChild(tr);
}
function getRows() {
  return [...document.querySelectorAll('#checkinTable tbody tr')].map(tr =>
    [...tr.children].map(td => td.innerText)
  );
}
function renderRows(rows) {
  const tbody = document.querySelector('#checkinTable tbody');
  tbody.innerHTML = '';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = r.map(c => `<td contenteditable="true">${c}</td>`).join('');
    tbody.appendChild(tr);
  });
}
function exportData() {
  const rows = getRows();
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '进度展板-打卡记录.json';
  a.click();
  URL.revokeObjectURL(a.href);
  alert('已导出 进度展板-打卡记录.json，请将文件保存到本HTML所在的目录以便下次导入');
}
function importData(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const rows = JSON.parse(reader.result);
      if (!Array.isArray(rows)) throw new Error('格式不对：应为行数组');
      renderRows(rows);
      alert('已导入打卡记录');
    } catch (e) {
      alert('导入失败：' + e.message);
    }
    event.target.value = '';
  };
  reader.readAsText(file);
}
</script>"""


def extract_checkin_rows(old_html: str) -> str:
    """从旧 HTML 里抽出打卡记录 <tbody> 内容，保留用户编辑数据。"""
    m = re.search(r'(<table id="checkinTable">.*?<tbody>)(.*?)(</tbody>)',
                  old_html, flags=re.S)
    return m.group(2) if m else ""


def upsert_today_checkin(old_rows_html: str, snapshot: dict) -> str:
    """同日只留最新快照；其余行及人工下一步保留原始 HTML，最多30行。"""
    now = snapshot["now"]
    today = now.strftime("%Y-%m-%d")
    rows = re.findall(r"<tr\b[^>]*>.*?</tr\s*>", old_rows_html,
                      flags=re.S | re.I)
    cells = [re.findall(r"<td\b[^>]*>(.*?)</td\s*>", row,
                        flags=re.S | re.I) for row in rows]

    def is_today(row_cells):
        date = html.unescape(re.sub(r"<[^>]+>", "", row_cells[0])).strip() if row_cells else ""
        return re.match(re.escape(today) + r"(?:\s|$)", date) is not None

    previous = next((cs for cs in cells if is_today(cs)), cells[0] if cells else [])
    next_step = previous[3] if len(previous) >= 4 else ""
    wiki, ragdb = snapshot["wiki"], snapshot["ragdb"]
    tasks, inflight = snapshot["tasks"], snapshot["inflight"]
    progress = (
        f"自动刷新({now:%H:%M})：wiki {wiki['total']}篇/深篇{wiki['deep']}；"
        f"已灌来源{ragdb['sources']}、chunk {ragdb['chunks']}；"
        f"books {ragdb['books']}/{BOOKS_TOTAL}；"
        f"任务账本{tasks['done']}done/{tasks['claimed']}claimed/{tasks['pending']}pending；"
        f"在途{inflight['running']}跑中+{inflight['pending_close']}待收口"
    )
    new_row = '\n      <tr>\n' + "\n".join(
        f'        <td contenteditable="true">{value}</td>'
        for value in (today, "RAG知识库重构 + 多智能体协同", esc(progress), next_step)
    ) + '\n      </tr>'
    history = [row for row, cs in zip(rows, cells) if not is_today(cs)]
    if len(history) > 29:
        def history_date(index):
            date = re.search(r"\d{4}-\d{2}-\d{2}", history[index])
            return date.group() if date else "9999-99-99"
        keep = set(sorted(range(len(history)), key=history_date, reverse=True)[:29])
        history = [row for i, row in enumerate(history) if i in keep]
    return new_row + "\n" + "\n".join(history) + "\n    "


def render_checkin_section(rows_html: str) -> str:
    return f"""<!-- ========== 打卡记录 ========== -->
<div class="checkin-section">
  <h2>📝 打卡记录</h2>
  <button class="btn" onclick="addRow()">➕ 新增一行</button>
  <button class="btn secondary" onclick="exportData()">💾 导出JSON文件</button>
  <button class="btn secondary" onclick="document.getElementById('importFile').click()">📂 导入JSON文件</button>
  <input type="file" id="importFile" accept=".json,application/json" style="display:none" onchange="importData(event)">
  <table id="checkinTable">
    <thead>
      <tr><th style="width:110px">日期</th><th style="width:180px">项目</th><th>今日进展</th><th style="width:220px">下一步</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="font-size:12px;color:#8b949e;margin-top:8px">💡 点击表格单元格可直接编辑；点击「导出JSON文件」会下载 进度展板-打卡记录.json（和本HTML放同目录），「导入JSON文件」选择该文件即可恢复数据（不再使用浏览器存储）。</p>
</div>
"""


def build_html(wiki, wbs, tasks, inflight, rag, agent, old_html, ragdb, fusion,
               fusion_status, okr=None) -> str:
    okr = okr if okr is not None else {"exists": False, "path": OKR, "issues": [],
                                       "os": [], "legend": [], "link_basis_tags": {}}
    timestamp = datetime.datetime.now()
    now = timestamp.strftime("%Y-%m-%d %H:%M")
    checkin_rows = upsert_today_checkin(extract_checkin_rows(old_html), {
        "now": timestamp, "wiki": wiki, "ragdb": ragdb,
        "tasks": tasks, "inflight": inflight,
    })

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>项目进度展板 - RAG知识库重构 & 多智能体协同前端</title>
<style>
{CSS}</style>
</head>
<body>

<h1>📊 项目执行全景图进度展板 <span class="date">{now}</span></h1>
<div class="nav"><a href="file:///mnt/c/MyData/Documents/wuyafang-l/raw/_task_orders/RAG全库整理执行总图-20261103.md">📋 RAG全库整理执行总图（计划+进度真源）</a> &nbsp;·&nbsp; <a href="#okr">🎯 目标追踪（OKR）</a> &nbsp;·&nbsp; <a href="file://{OKR.as_posix()}">📄 OKR-2026Q4.md（目标真源）</a></div>

{render_okr_card(okr)}

{render_rag_card(wiki, wbs, inflight, rag, ragdb, fusion, fusion_status)}

{render_agent_card(wbs, tasks, inflight, agent)}

{render_checkin_section(checkin_rows)}

{CHECKIN_JS}

</body>
</html>
"""


def _render() -> int:
    print("[render] 扫描 wiki L* ...")
    wiki = scan_wiki()
    print(f"  篇数={wiki['total']}  深篇={wiki['deep']}")

    print("[render] 融合状态 ...")
    fusion_status = scan_fusion_status()
    print(f"  清单={fusion_status['checklists']}/12  "
          f"缺三段={fusion_status['miss3_total']}篇  "
          f"快照={fusion_status['snapshot_total']}个")

    print("[render] 跑 inflight-check ...")
    inflight = scan_inflight()
    print(f"  跑中={inflight['running']}  待收口={inflight['pending_close']}")

    print("[render] 读 shared-tasks.db ...")
    tasks = scan_tasks()
    print(f"  total={tasks['total']}  done={tasks['done']}  "
          f"claimed={tasks['claimed']}  pending={tasks['pending']}")

    print("[render] 读 chroma 灌库库 ...")
    ragdb = scan_rag_db()
    print(f"  来源={ragdb['sources']}  books={ragdb['books']}/{BOOKS_TOTAL}  "
          f"chunks={ragdb['chunks']}")

    print("[render] 解析执行总图 ...")
    wbs = scan_wbs()
    fusion = scan_fusion(WBS.read_text(encoding="utf-8", errors="ignore") if WBS.exists() else "")
    print(f"  融合深化 L4={fusion['l4']}  B3.2={fusion['b32']}")
    old_html = DASH.read_text(encoding="utf-8", errors="ignore") if DASH.exists() else ""
    # 前端清单：执行总图 → 旧展板 → 兜底
    if not wbs["frontend"]:
        fe = scan_frontend_from_html(old_html)
        wbs["frontend"] = fe if fe else list(FRONTEND_FALLBACK)
    print(f"  批次={len(wbs['batches'])}  主题={len(wbs['topics'])}  "
          f"M系列={len(wbs['m_series'])}  前端={len(wbs['frontend'])}")

    print("[render] 解析 OKR 目标真源 ...")
    okr = scan_okr()
    print(f"  O={len(okr['os'])}  KR={sum(o['total'] for o in okr['os'])}  "
          f"举措={len(okr.get('init_by_id', {}))}  待修={len(okr['issues'])}")

    rag = compute_rag(wiki, wbs, inflight)
    agent = compute_agent(wbs, tasks, inflight)
    print(f"[render] RAG 进度 = {rag['progress']}%")
    print(f"[render] 多智能体进度 = {agent['progress']}%")

    new_html = build_html(wiki, wbs, tasks, inflight, rag, agent, old_html, ragdb, fusion,
                          fusion_status, okr)

    DASH.write_text(new_html, encoding="utf-8")
    print(f"[render] ✅ 已写入 {DASH} ({len(new_html)} 字符)")
    return 0


def source_stamp() -> dict:
    """只 stat 真源，不读正文；覆盖 OKR/WBS、wiki、库和在途，避免只盯两篇造成回归。"""
    paths = {OKR, WBS, Path(__file__).resolve(), DB, CHROMA, INFLIGHT,
             Path.home() / ".hermes/cache/scratch/inflight.json"}
    for db in (DB, CHROMA):
        paths.add(Path(str(db) + "-wal"))
    if WIKI.exists():
        for layer in WIKI.iterdir():
            if layer.is_dir() and re.match(r"^L\d+", layer.name):
                paths.update(layer.rglob("*.md"))
                snapshots = layer / ".fusion-work"
                if snapshots.is_dir():
                    paths.update(p for p in snapshots.rglob("*") if p.is_file())
    sources = {}
    for path in sorted(paths):
        try:
            stat = path.stat()
            sources[str(path)] = [stat.st_mtime_ns, stat.st_size]
        except FileNotFoundError:
            sources[str(path)] = None
    # 每天至少重建一次，保留每日打卡；HTML/本 stamp 不参与源，避免自触发。
    return {"version": 1, "day": datetime.date.today().isoformat(), "sources": sources}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="展板按源 stamp 幂等刷新（一次性，无常驻进程）")
    parser.add_argument("--force", action="store_true", help="忽略 stamp，强制重渲染")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    with DASH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = source_stamp()
        try:
            previous = json.loads(STAMP.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            previous = None
        if not args.force and before == previous and DASH.stat().st_size:
            print(f"[stamp] 源未变化，跳过重渲染；耗时={time.perf_counter() - started:.3f}s；"
                  f"产物大小={DASH.stat().st_size}B")
            return 0
        reason = "--force" if args.force else "源变化/首次运行/产物缺失/stamp失效"
        print(f"[stamp] 命中重渲染：{reason}；跟踪源={len(before['sources'])}")
        result = _render()
        if result != 0:
            return result
        # 只在成功且渲染期间真源未再变化时提交 stamp；否则下次调用仍会重建。
        if source_stamp() == before:
            temporary = STAMP.with_suffix(".json.tmp")
            try:
                temporary.write_text(json.dumps(before, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
                temporary.replace(STAMP)
            finally:
                temporary.unlink(missing_ok=True)
            print(f"[stamp] 已记录 {STAMP.name}")
        else:
            print("[stamp] 渲染期间源再次变化，未提交 stamp，下次调用重试")
        print(f"[refresh] 已重渲染；耗时={time.perf_counter() - started:.3f}s；"
              f"产物大小={DASH.stat().st_size}B")
        return 0


if __name__ == "__main__":
    sys.exit(main())
