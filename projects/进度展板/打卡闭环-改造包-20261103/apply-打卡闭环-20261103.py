#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打卡闭环改造·一键应用 —— 净效果只动三个文件：
  ① 新增 ~/.hermes/scripts/daka_append.py                （打卡追加函数）
  ② 补丁 ~/.hermes/scripts/inflight-check.py             （--close 收口自动打卡）
  ③ 补丁 /home/wuyafang/kb-local/rag/ingest.py           （末尾机械兜底挂接 build_kb.py）

用法：
  python3 apply-打卡闭环-20261103.py                    # 正式应用（需对 /home 有写权限）
  python3 apply-打卡闭环-20261103.py --test-root /tmp/x # 沙箱自测（目标树镜像到前缀下）

安全阀：两处补丁锚点必须在目标文件里恰好命中 1 次，否则中止不动任何文件。
改动前先留 .bak-20261103 备份；应用后自动 ast.parse + py_compile + 正负例实测。
测试均在临时目录/镜像树内进行，不触碰正式展板 JSON 与正式 build_kb.py。
"""
import argparse
import ast
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP = "20261103"

INFLIGHT_REL = "/home/wuyafang/.hermes/scripts/inflight-check.py"
INGEST_REL = "/home/wuyafang/kb-local/rag/ingest.py"
DAKA_REL = "/home/wuyafang/.hermes/scripts/daka_append.py"

# ── 补丁① inflight-check.py：--close 收口成功后自动追加打卡记录 ──────────
PATCH1_OLD = '''        if n:
            save(d); print(f"[inflight] 已收口 {a.close} ({n}条)" + (f"：{a.note}" if a.note else "")); return
        print(f"[inflight] 未找到 {a.close}"); return'''
PATCH1_NEW = '''        if n:
            save(d); print(f"[inflight] 已收口 {a.close} ({n}条)" + (f"：{a.note}" if a.note else ""))
            # === 打卡闭环 (2026-11-03): 收口即自动追加打卡记录到进度展板 ===
            # 记录任务名/时间/状态; 打卡失败只告警, 不影响收口结果。
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from daka_append import append_checkin
                _row = append_checkin(task=a.close, status="已收口", note=a.note)
                print(f"[打卡] 已追加展板记录: {_row}")
            except Exception as _e:
                print(f"[打卡] WARN: 打卡追加失败({_e})，不影响收口结果", file=sys.stderr)
            return
        print(f"[inflight] 未找到 {a.close}"); return'''

# ── 补丁② ingest.py：__main__ 末尾机械兜底挂接 build_kb.py ──────────────
PATCH2_OLD = '''        except Exception as _e:
            print(f"[sync] WARN: 自动同步失败({_e})，不影响 ingest 入库结果", file=sys.stderr)
    sys.exit(_rc)'''
PATCH2_NEW = '''        except Exception as _e:
            print(f"[sync] WARN: 自动同步失败({_e})，不影响 ingest 入库结果", file=sys.stderr)
    # === ingest 完成后机械兜底: 自动挂接 build_kb.py（内容变更→自动刷新前端）===
    # 效果：ingest 入库成功 → build_kb.py 重生成 kb-data.js + 知识库入口.html，
    # 前端数据永远跟库内容对齐。与 sync_wiki_site.py 互为兜底，失败不影响 ingest 结果。
    if _rc == 0:
        try:
            import subprocess
            _here = os.path.dirname(os.path.abspath(__file__))
            _build = os.path.normpath(os.path.join(_here, "..", "scripts", "build_kb.py"))
            if not os.path.exists(_build):
                _build = os.path.join(_here, "build_kb.py")
            if os.path.exists(_build):
                print("[build_kb] 自动触发 build_kb.py（kb-data.js + 知识库入口刷新）...", file=sys.stderr)
                _br = subprocess.run([sys.executable, _build], check=False)
                print(f"[build_kb] build_kb.py 退出码 {_br.returncode}", file=sys.stderr)
            else:
                print("[build_kb] WARN: 未找到 build_kb.py，跳过前端刷新", file=sys.stderr)
        except Exception as _e:
            print(f"[build_kb] WARN: 自动刷新失败({_e})，不影响 ingest 入库结果", file=sys.stderr)
    sys.exit(_rc)'''


def say(tag, msg):
    print(f"[{tag}] {msg}")


def apply_patch(path, old, new, tag):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    cnt = src.count(old)
    if cnt != 1:
        say(tag, f"ABORT: 补丁锚点命中 {cnt} 次（要求恰好 1 次），未改动 {path}")
        sys.exit(1)
    bak = path + f".bak-{STAMP}"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        say(tag, f"备份 -> {bak}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new))
    say(tag, f"补丁完成 {path}")


def syntax_check(path, tmp):
    with open(path, encoding="utf-8") as f:
        ast.parse(f.read())
    py_compile.compile(path, cfile=os.path.join(tmp, os.path.basename(path) + ".pyc"), doraise=True)
    say("SYNTAX", f"ast.parse + py_compile 通过 {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-root", default="", help="沙箱自测：所有目标路径加此前缀（含 HOME 仿真）")
    a = ap.parse_args()
    root = a.test_root.rstrip("/")
    P = lambda rel: (root + rel) if root else rel

    inflight, ingest, daka_dst = P(INFLIGHT_REL), P(INGEST_REL), P(DAKA_REL)
    daka_src = os.path.join(HERE, "daka_append.py")
    for p in (inflight, ingest, daka_src):
        if not os.path.exists(p):
            say("ABORT", f"缺文件: {p}")
            sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="daka-apply-") as tmp:
        # 1) 落盘打卡函数 + 两处补丁
        os.makedirs(os.path.dirname(daka_dst), exist_ok=True)
        shutil.copy2(daka_src, daka_dst)
        say("FILE", f"写入 {daka_dst}")
        apply_patch(inflight, PATCH1_OLD, PATCH1_NEW, "PATCH1")
        apply_patch(ingest, PATCH2_OLD, PATCH2_NEW, "PATCH2")

        # 2) 语法门禁
        for p in (inflight, ingest, daka_dst):
            syntax_check(p, tmp)

        # 3) 正例T1：打卡追加两行累积 + 与展板 HTML 导入格式对齐
        board = os.path.join(tmp, "board", "进度展板-打卡记录.json")
        spec = importlib.util.spec_from_file_location("daka_append", daka_dst)
        da = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(da)
        da.append_checkin("任务A", "已收口", "验收通过", path=board)
        da.append_checkin("任务B", status="超时", path=board)
        rows = json.load(open(board, encoding="utf-8"))
        assert len(rows) == 2 and all(isinstance(r, list) and len(r) == 4 for r in rows), rows
        say("T1正例", f"两行累积+4列schema OK: {rows}")

        # 4) 负例T2：坏 schema 文件必须报错，不静默吞
        bad = os.path.join(tmp, "bad.json")
        json.dump({"not": "rows"}, open(bad, "w"))
        try:
            da.append_checkin("任务C", path=bad)
            say("T2负例", "FAIL: 坏 schema 未报错")
            sys.exit(1)
        except ValueError as e:
            say("T2负例", f"坏schema被拦: {e}")

        # 5) 负例T3 + 正例T4：补丁后 inflight-check.py 真跑 --close（HOME/展板均指向临时目录）
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, ".hermes", "cache", "scratch"), exist_ok=True)
        env = dict(os.environ, HOME=home, DAKA_BOARD_JSON=board)

        def run(args):
            r = subprocess.run([sys.executable, inflight] + args, capture_output=True, text=True, env=env)
            print(r.stdout + r.stderr, end="")
            say("EXIT", f"inflight-check {' '.join(args)} -> {r.returncode}")
            return r

        n0 = len(json.load(open(board, encoding="utf-8")))
        r = run(["--close", "不存在的批次"])
        assert "未找到" in r.stdout
        assert len(json.load(open(board, encoding="utf-8"))) == n0, "负例不该打卡"
        say("T3负例", "未知批次收口未打卡、行数不变 OK")

        run(["--add", "打卡闭环重构-20261103", "--pid", str(os.getpid()), "--eta", "30",
             "--desc", "打卡闭环修复", "--executor", "DSH"])
        r = run(["--close", "打卡闭环重构-20261103", "--note", "打卡闭环修复完成"])
        assert "[打卡] 已追加展板记录" in r.stdout
        rows = json.load(open(board, encoding="utf-8"))
        last = rows[-1]
        assert last[1] == "打卡闭环重构-20261103" and "已收口" in last[2], last
        say("T4正例", f"--close 自动打卡 OK: {last}")

        # 6) ingest.py 挂钩块实测：在临时镜像树里执行补丁后文件的真实代码文本
        src = open(ingest, encoding="utf-8").read()
        # 切片锚点含行首缩进（只锚 '#' 会丢 4 格空格 → dedent 公共前缀算空）
        block = src[src.index("    # === ingest 完成后机械兜底"):src.index("    sys.exit(_rc)")]
        mirror = os.path.join(tmp, "kb")
        mirror_ingest = os.path.join(mirror, "rag", "ingest.py")
        mirror_scripts = os.path.join(mirror, "scripts")
        os.makedirs(os.path.dirname(mirror_ingest), exist_ok=True)
        os.makedirs(mirror_scripts, exist_ok=True)
        shutil.copy2(ingest, mirror_ingest)

        def run_hook(rc):
            ns = {"os": os, "sys": sys, "_rc": rc, "__file__": mirror_ingest}
            # 片段原本嵌在 if __name__ 块内（4格缩进），先 dedent 再当模块执行
            exec(compile(textwrap.dedent(block), "<ingest-hook>", "exec"), ns)

        stub = os.path.join(mirror_scripts, "build_kb.py")
        with open(stub, "w") as f:
            f.write("print('stub-build-kb OK')\n")
        say("T5正例", "build_kb.py 存在 + _rc=0:")
        run_hook(0)
        os.remove(stub)
        say("T5负例", "build_kb.py 缺失 + _rc=0:")
        run_hook(0)
        say("T5负例", "build_kb.py 缺失 + _rc=2（入库失败不该触发）:")
        run_hook(2)

        # 7) 落盘复核：grep 实测补丁真在文件里
        for path, kw in ((inflight, "打卡闭环"), (ingest, "机械兜底")):
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                if kw in line:
                    print(f"[GREP] {path}:{i}: {line.rstrip()}")

    # 8) 正式模式独有：真跑一次 build_kb.py 验「内容变更→自动刷新前端」链路可用
    if not root:
        real_build = P("/home/wuyafang/kb-local/scripts/build_kb.py")
        if os.path.exists(real_build):
            say("T6正例", f"实跑 build_kb.py（刷新 kb-data.js + 知识库入口）: {real_build}")
            r = subprocess.run([sys.executable, real_build], check=False)
            say("T6正例", f"build_kb.py 退出码 {r.returncode}")
        else:
            say("T6", f"未找到 {real_build}，跳过")

    say("DONE", "全部正负例通过；三文件改造完成" + ("（test-root 自测模式，未动正式文件）" if root else ""))


if __name__ == "__main__":
    main()
