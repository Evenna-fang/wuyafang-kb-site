#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打卡追加函数 —— 收口时自动把打卡记录追加到进度展板 JSON。

被 inflight-check.py --close 收口时调用；一条记录 = 任务名、时间、状态 三要素。

文件格式与展板 HTML「导入JSON文件」严格对齐（硬约束，别改成对象数组）：
  进度展板-打卡记录.json = JSON 行数组，每行是 4 列单元格：
    [日期, 项目, 今日进展, 下一步]
  与 HTML 表格列序一致——HTML 的 exportData() 写出的就是 [[...],[...]] 结构，
  importData() 只接受 Array.isArray(rows) 且每行按 r.map 渲染，行不是数组会渲染报错。

字段对应：日期=时间 · 项目=任务名 · 今日进展=状态(+备注) · 下一步=下一步/备注。

环境变量 DAKA_BOARD_JSON 可覆盖目标路径（测试/迁移用，缺省即展板正式文件）。
"""
import json
import os
import tempfile
from datetime import datetime

BOARD_JSON = os.environ.get(
    "DAKA_BOARD_JSON",
    "/mnt/c/MyData/Documents/wuyafang-l/projects/进度展板/进度展板-打卡记录.json")


def append_checkin(task, status="已收口", note="", path=None, when=None):
    """追加一行打卡记录到展板 JSON，返回追加的行（list[str]）。

    task   : 任务名（在途单 batch 名）
    status : 状态（如 已收口 / closed / 超时）
    note   : 备注（如 --close 的收口说明）；为空则状态列只写 status
    path   : 目标 JSON 路径，默认 BOARD_JSON；测试可指向临时文件
    when   : datetime，缺省取当前时间
    """
    path = path or BOARD_JSON
    when = when or datetime.now()

    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            raise ValueError(f"打卡文件格式不对（应为行数组）: {path}")

    status_cell = status if not note else f"{status}：{note}"
    row = [when.strftime("%Y-%m-%d %H:%M"), str(task), status_cell, "-"]
    rows.append(row)

    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".daka-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半损坏打卡文件
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return row
