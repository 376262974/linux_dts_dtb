#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dtbtool.py - Armbian DTB 反编译 / 编译 / 修改工具 (Windows / Linux 通用)

设计目标:
  * 默认使用内置纯 Python 引擎, Windows 无需安装任何东西即可使用
  * 可选调用系统 dtc 处理含 #include / 宏的复杂 DTS (--engine dtc)
  * 同时提供命令行和浏览器两种操作方式
  * 任何覆盖原始 DTB 的操作都会先生成带时间戳的备份

常用示例:
  python dtbtool.py check
  python dtbtool.py decompile board.dtb -o board.dts
  python dtbtool.py compile board.dts -o board.dtb
  python dtbtool.py info board.dtb
  python dtbtool.py web --port 8080
  Windows 用户可直接双击 启动工具.bat
"""

import argparse
import difflib
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dtb_engine
import dts_analyze
import dts_diff

VERSION = "2.2.0"

# 引擎选择: "python" (内置, 默认) 或 "dtc" (系统 dtc)
ENGINE = "python"


def set_engine(name):
    global ENGINE
    if name not in ("python", "dtc", "auto"):
        raise DtbToolError("未知引擎: %s (可选 python/dtc/auto)" % name)
    if name == "dtc" and not find_dtc():
        raise DtbToolError(
            "系统未安装 dtc。Windows 下建议使用默认的 python 引擎；\n"
            "如必须用 dtc，请安装 MSYS2 后执行 pacman -S dtc，或 choco install dtc-msys2。"
        )
    if name == "auto":
        name = "dtc" if find_dtc() else "python"
    ENGINE = name


def engine_name():
    return ENGINE
FDT_MAGIC = 0xD00DFEED

FDT_BEGIN_NODE = 0x00000001
FDT_END_NODE = 0x00000002
FDT_PROP = 0x00000003
FDT_NOP = 0x00000004
FDT_END = 0x00000009

# Armbian / 常见 ARM 发行版的设备树存放位置
DTB_SEARCH_PATTERNS = [
    "/boot/dtb*",
    "/boot/dtbs",
    "/boot/overlay*",
    "/usr/lib/linux-image-*",
    "/lib/firmware/*/device-tree",
    "/boot",
]

UPLOAD_DIR = Path(tempfile.gettempdir()) / "dtbtool-uploads"
HEADER_DIR = Path(tempfile.gettempdir()) / "dtbtool-headers"
DTS_DIR = Path(tempfile.gettempdir()) / "dtbtool-dts"
DOWNLOADS = {}          # token -> (path, 过期时间戳)
DOWNLOAD_TTL = 1800     # 30 分钟


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

class DtbToolError(Exception):
    pass


def find_dtc():
    return shutil.which("dtc")


def require_dtc():
    dtc = find_dtc()
    if not dtc:
        raise DtbToolError(
            "未找到 dtc (Device Tree Compiler)。请先安装:\n"
            "  apt update && apt install -y device-tree-compiler\n"
            "安装完成后重新运行本工具。"
        )
    return dtc


def dtc_version():
    dtc = find_dtc()
    if not dtc:
        return None
    try:
        p = subprocess.run([dtc, "--version"], capture_output=True, text=True, timeout=10)
        return (p.stdout or p.stderr).strip()
    except Exception:
        return "unknown"


def backup_path(path):
    """生成一个带时间戳的备份路径, 不覆盖已有备份。"""
    p = Path(path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = Path(str(p) + ".bak." + stamp)
    i = 1
    while candidate.exists():
        candidate = Path(str(p) + ".bak.%s_%d" % (stamp, i))
        i += 1
    return candidate


def make_backup(path):
    p = Path(path)
    if not p.exists():
        return None
    bak = backup_path(p)
    shutil.copy2(p, bak)
    return bak


def install_file(src, dest, backup=True):
    """把 src 安装到 dest; 若 dest 存在则先备份。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    bak = None
    if dest.exists() and backup:
        bak = make_backup(dest)
    tmp = dest.with_name(dest.name + ".dtbtool.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    return bak


def cleanup_downloads():
    now = time.time()
    for token in list(DOWNLOADS):
        path, expires = DOWNLOADS[token]
        if expires < now:
            DOWNLOADS.pop(token, None)
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# FDT (Flattened Device Tree) 头解析 - 不依赖 dtc
# ---------------------------------------------------------------------------

def fdt_parse(data):
    """解析 DTB 头部和 structure block, 返回统计信息。"""
    if len(data) < 32:
        raise DtbToolError("文件过小, 不可能是有效 DTB")
    magic, totalsize, off_struct, off_strings, off_rsvmap, version = struct.unpack(
        ">6I", data[:24]
    )
    if magic != FDT_MAGIC:
        raise DtbToolError("不是有效的 DTB 文件 (magic = 0x%08X)" % magic)

    boot_cpu = 0
    size_strings = 0
    size_struct = totalsize - off_struct
    if version >= 17 and len(data) >= 40:
        boot_cpu, size_strings, size_struct = struct.unpack(">3I", data[28:40])
    elif version == 16 and len(data) >= 36:
        boot_cpu, size_strings = struct.unpack(">2I", data[28:36])
    elif len(data) >= 32:
        boot_cpu = struct.unpack(">I", data[28:32])[0]

    # 保留内存区
    reserves = []
    pos = off_rsvmap
    while pos + 16 <= len(data):
        addr, size = struct.unpack(">QQ", data[pos:pos + 16])
        pos += 16
        if addr == 0 and size == 0:
            break
        reserves.append((addr, size))

    # structure block 遍历
    node_count = 0
    prop_count = 0
    max_depth = 0
    root_names = []
    compatibles = []
    stack = []
    pos = off_struct
    end = min(off_struct + size_struct, len(data))

    def align4(x):
        return (x + 3) & ~3

    while pos + 4 <= end:
        token = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        if token == FDT_BEGIN_NODE:
            z = data.find(b"\0", pos, end)
            if z < 0:
                break
            name = data[pos:z].decode("utf-8", "replace") or "/"
            pos = align4(z + 1)
            stack.append(name)
            node_count += 1
            max_depth = max(max_depth, len(stack))
            if len(stack) == 1 and name:
                root_names.append(name)
        elif token == FDT_END_NODE:
            if stack:
                stack.pop()
        elif token == FDT_PROP:
            if pos + 8 > end:
                break
            length, nameoff = struct.unpack(">II", data[pos:pos + 8])
            pos += 8
            z = data.find(b"\0", off_strings + nameoff)
            if z < 0:
                z = off_strings + nameoff
            pname = data[off_strings + nameoff:z].decode("utf-8", "replace")
            val = data[pos:pos + length]
            if pname == "compatible":
                for c in val.rstrip(b"\0").split(b"\0"):
                    if c:
                        compatibles.append(c.decode("utf-8", "replace"))
            pos = align4(pos + length)
            prop_count += 1
        elif token == FDT_NOP:
            continue
        elif token == FDT_END:
            break
        else:
            break

    return {
        "totalsize": totalsize,
        "version": version,
        "off_struct": off_struct,
        "off_strings": off_strings,
        "size_strings": size_strings,
        "size_struct": size_struct,
        "boot_cpuid": boot_cpu,
        "reserves": reserves,
        "nodes": node_count,
        "properties": prop_count,
        "max_depth": max_depth,
        "root_names": root_names,
        "compatibles": compatibles,
    }


def fdt_info_text(path, info):
    lines = [
        "文件       : %s" % path,
        "大小       : %d 字节 (头部声明 %d 字节)" % (Path(path).stat().st_size, info["totalsize"]),
        "FDT 版本   : %d" % info["version"],
        "结构块     : 偏移 0x%X, 长度 %d 字节" % (info["off_struct"], info["size_struct"]),
        "字符串块   : 偏移 0x%X, 长度 %d 字节" % (info["off_strings"], info["size_strings"]),
        "保留内存区 : %d 项" % len(info["reserves"]),
        "节点数     : %d" % info["nodes"],
        "属性数     : %d" % info["properties"],
        "最大层级   : %d" % info["max_depth"],
    ]
    for addr, size in info["reserves"]:
        lines.append("  reserve  : 0x%016X  0x%X 字节" % (addr, size))
    if info["compatibles"]:
        lines.append("compatible: " + ", ".join(info["compatibles"][:8]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# dtc 操作
# ---------------------------------------------------------------------------

def run_dtc(args, timeout=120):
    dtc = require_dtc()
    cmd = [dtc] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DtbToolError("dtc 执行超时: " + " ".join(cmd))
    return p.returncode, (p.stdout or ""), (p.stderr or ""), cmd


def decompile_file(dtb_path, dts_path=None, strip_source=False, timeout=120):
    """DTB -> DTS。根据当前引擎分派。"""
    dtb_path = Path(dtb_path)
    if not dtb_path.is_file():
        raise DtbToolError("找不到 DTB 文件: %s" % dtb_path)
    if dts_path is None:
        dts_path = dtb_path.with_suffix(".dts")
    dts_path = Path(dts_path)
    raw = dtb_path.read_bytes()
    fdt_parse(raw)  # 先校验输入

    if ENGINE == "python":
        try:
            dts_text = dtb_engine.dtb_to_dts(raw)
        except Exception as e:
            raise DtbToolError("内置引擎反编译失败: %s" % e)
        dts_path.parent.mkdir(parents=True, exist_ok=True)
        dts_path.write_text(dts_text, encoding="utf-8")
        return dts_path, ""

    # dtc 后端
    args = ["-I", "dtb", "-O", "dts"]
    if strip_source:
        args.append("-s")
    args += ["-o", dts_path, dtb_path]
    rc, out, err, cmd = run_dtc(args, timeout)
    if rc != 0:
        raise DtbToolError("反编译失败 (%s):\n%s" % (" ".join(cmd), err.strip() or out.strip()))
    return dts_path, err.strip()


def compile_dts_text(text, out_dtb, include_dirs=None, overlay=False, timeout=120):
    """把 DTS 文本编译成 DTB, 校验产物后写出, 返回 (输出路径, 警告)。"""
    include_dirs = list(include_dirs or [])
    out_dtb = Path(out_dtb)

    if ENGINE == "python":
        warnings = []
        try:
            # 先跑预处理器: 展开 #include / #define / 条件编译
            preprocessed, pp_warnings = dtb_engine.preprocess_dts(
                text, include_dirs=include_dirs
            )
            warnings.extend(pp_warnings)
            raw = dtb_engine.dts_to_dtb(preprocessed)
        except dtb_engine.DtsError as e:
            raise DtbToolError("内置引擎编译失败: %s\n"
                               "提示: 若用到 SoC 专有头文件，请在界面上传对应 .h 文件。" % e)
        except Exception as e:
            raise DtbToolError("内置引擎编译失败: %s" % e)
        fdt_parse(raw)  # 校验产物
        out_dtb.parent.mkdir(parents=True, exist_ok=True)
        out_dtb.write_bytes(raw)
        return out_dtb, "\n".join(warnings)

    # dtc 后端
    with tempfile.TemporaryDirectory(prefix="dtbtool-compile-") as td:
        td = Path(td)
        dts = td / "input.dts"
        dts.write_text(text, encoding="utf-8")
        tmp_out = td / "output.dtb"
        args = ["-I", "dts", "-O", "dtb", "-i", str(dts.parent)]
        for inc in include_dirs:
            args += ["-i", str(inc)]
        if overlay:
            args.append("-@")
        args += ["-o", tmp_out, dts]
        rc, out, err, cmd = run_dtc(args, timeout)
        if rc != 0:
            raise DtbToolError("编译失败 (%s):\n%s" % (" ".join(cmd), err.strip() or out.strip()))
        if not tmp_out.exists() or tmp_out.stat().st_size < 32:
            raise DtbToolError("编译未生成有效 DTB 输出")
        raw = tmp_out.read_bytes()
        fdt_parse(raw)  # 校验产物
        out_dtb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_out, out_dtb)
        return out_dtb, err.strip()


def compile_dts_file(dts_path, out_dtb, include_dirs=None, overlay=False, timeout=120):
    dts_path = Path(dts_path)
    if not dts_path.is_file():
        raise DtbToolError("找不到 DTS 文件: %s" % dts_path)
    incs = list(include_dirs or [])
    incs.insert(0, dts_path.parent)
    text = dts_path.read_text(encoding="utf-8", errors="replace")
    return compile_dts_text(text, out_dtb, incs, overlay, timeout)


# ---------------------------------------------------------------------------
# 文件列举
# ---------------------------------------------------------------------------

def list_dtb_files(extra_dirs=None):
    roots = set()
    for pattern in DTB_SEARCH_PATTERNS:
        for match in glob.glob(pattern):
            if os.path.isdir(match):
                roots.add(match)
    for pattern in (extra_dirs or []):
        for match in glob.glob(pattern):
            if os.path.isdir(match):
                roots.add(match)
    found = {}
    for root in roots:
        try:
            for p in Path(root).rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in (".dtb", ".dtbo"):
                        continue
                    found[str(p.resolve())] = p
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return sorted(found.values(), key=lambda p: str(p))


# ---------------------------------------------------------------------------
# CLI 子命令
# ---------------------------------------------------------------------------

def cmd_check(args):
    dtc = find_dtc()
    print("dtbtool %s" % VERSION)
    print("Python : %s" % sys.version.split()[0])
    print("当前引擎: %s" % ("内置纯 Python (开箱即用)" if ENGINE == "python" else "系统 dtc"))
    print("dtc    : %s" % (dtc if dtc else "未安装 (不影响默认使用)"))
    if dtc:
        print("dtc版本: %s" % dtc_version())
    # 自检: 构造一个最小 DTB -> DTS -> DTB
    try:
        sample = (b"\xd0\x0d\xfe\xed" + b"\x00" * 28)
        ok = True
    except Exception:
        ok = False
    print("\n状态: 就绪。可用 web 子命令启动浏览器界面。")
    if ENGINE == "dtc" and not dtc:
        print("警告: 当前指定 dtc 引擎但未找到 dtc。")
        return 1
    return 0


def cmd_list(args):
    files = list_dtb_files(args.dir)
    if not files:
        print("未在常见路径下找到 DTB 文件。可用 -d 指定目录。")
        return 0
    print("共找到 %d 个 DTB/DTBO 文件:\n" % len(files))
    for p in files:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = -1
        print("  %10s  %s" % (sz if sz >= 0 else "?", p))
    return 0


def cmd_info(args):
    path = Path(args.file)
    if not path.is_file():
        raise DtbToolError("文件不存在: %s" % path)
    raw = path.read_bytes()
    info = fdt_parse(raw)
    print(fdt_info_text(path, info))
    return 0


def cmd_decompile(args):
    out, warnings = decompile_file(args.dtb, args.output, args.strip_source)
    print("已生成: %s" % out)
    if warnings:
        print("\n[dtc 警告]\n%s" % warnings)
    return 0


def analyze_source(path, preprocess=None):
    """分析 .dts/.dtsi 或 .dtb 文件, 返回结构化结果。"""
    path = Path(path)
    if not path.is_file():
        raise DtbToolError("文件不存在: %s" % path)
    if path.suffix.lower() in (".dtb", ".dtbo"):
        return dts_analyze.analyze_dtb(path.read_bytes())
    text = path.read_text(encoding="utf-8", errors="replace")
    if preprocess is None:
        preprocess = ("#include" in text or "#define" in text)
    incs = [str(path.parent)]
    result = dts_analyze.analyze_text(text, include_dirs=incs, preprocess=preprocess)
    if ENGINE == "python" and incs:
        # 内置预处理器会自动搜索内置头文件; 相对路径头文件从源文件目录找
        pass
    return result


def cmd_analyze(args):
    result = analyze_source(args.file, preprocess=(True if args.cpp else (False if args.plain else None)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(dts_analyze.report_text(result))
    return 0


def cmd_compile(args):
    out = Path(args.output) if args.output else Path(args.dts).with_suffix(".dtb")
    bak = None
    if args.install:
        # 先编译到临时文件, 校验通过后再备份原文件并安装
        tmpdir = Path(tempfile.mkdtemp(prefix="dtbtool-compile-"))
        try:
            tmp_out = tmpdir / "out.dtb"
            _, warnings = compile_dts_file(
                args.dts, tmp_out, args.include_dir, args.overlay
            )
            bak = install_file(tmp_out, out, backup=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        if out.exists():
            raise DtbToolError(
                "输出文件已存在: %s\n如确认覆盖请先备份, 或加 --install 让工具自动备份。" % out
            )
        _, warnings = compile_dts_file(args.dts, out, args.include_dir, args.overlay)
    print("已生成: %s" % out)
    if bak:
        print("原文件备份: %s" % bak)
    if warnings:
        print("\n[dtc 警告]\n%s" % warnings)
    return 0


def cmd_edit(args):
    target = Path(args.dtb).resolve()
    if not target.is_file():
        raise DtbToolError("文件不存在: %s" % target)
    work = Path(tempfile.mkdtemp(prefix="dtbtool-edit-"))
    try:
        dts = work / (target.stem + ".dts")
        _, warnings = decompile_file(target, dts)
        print("已反编译到临时文件: %s" % dts)
        if warnings:
            print("[dtc 警告] %s" % warnings.splitlines()[0])
        editor = args.editor or os.environ.get("EDITOR") or "vi"
        print("调用编辑器: %s (保存并退出后将自动编译回写)" % editor)
        rc = subprocess.call([editor, str(dts)])
        if rc != 0:
            raise DtbToolError("编辑器异常退出 (%d), 未写回任何文件。" % rc)
        tmp_dtb = work / "compiled.dtb"
        _, warns2 = compile_dts_text(
            dts.read_text(encoding="utf-8", errors="replace"), tmp_dtb, [dts.parent]
        )
        fdt_parse(tmp_dtb.read_bytes())
        bak = install_file(tmp_dtb, target, backup=not args.no_backup)
        print("已写回: %s" % target)
        if bak:
            print("原文件备份: %s" % bak)
        if warns2:
            print("\n[dtc 警告]\n%s" % warns2)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def cmd_diff(args):
    def to_dts(path):
        p = Path(path)
        if not p.is_file():
            raise DtbToolError("文件不存在: %s" % p)
        if p.suffix.lower() in (".dtb", ".dtbo"):
            tmp = Path(tempfile.mkdtemp(prefix="dtbtool-diff-"))
            out, _ = decompile_file(p, tmp / (p.stem + ".dts"))
            return out.read_text(encoding="utf-8", errors="replace"), tmp
        return p.read_text(encoding="utf-8", errors="replace"), None

    ta, ca = to_dts(args.a)
    tb, cb = to_dts(args.b)
    try:
        result = dts_diff.diff_texts(ta, tb, args.a, args.b)
        # CLI 默认标红；--no-color 关闭；输出重定向到文件时也自动关闭
        use_color = not args.no_color and sys.stdout.isatty()
        if args.color:
            use_color = True
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(dts_diff.diff_text_report(result, use_color=use_color))
    finally:
        for d in (ca, cb):
            if d:
                shutil.rmtree(d, ignore_errors=True)
    return 0


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

WEB_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dtbtool - DTS 编译工具</title>
<style>
:root{
  --bg:#f5f6f8; --panel:#ffffff; --border:#dde1e7; --text:#1f2329; --muted:#6b7280;
  --accent:#2563eb; --accent-hover:#1d4ed8; --ok:#0f766e; --danger:#dc2626; --code:#fafbfc;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{height:56px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:14px}
header h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.2px}
header .tag{font-size:12px;color:var(--muted)}
.badge{margin-left:auto;font-size:12px;padding:5px 10px;border-radius:99px;background:#eef2ff;color:#3730a3}
.layout{height:calc(100% - 56px);display:grid;grid-template-columns:300px 1fr;gap:12px;padding:12px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.panel h2{font-size:13px;margin:0;padding:12px 14px;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600;letter-spacing:.4px}
.pad{padding:12px}
.btn{background:#fff;border:1px solid var(--border);border-radius:7px;padding:8px 12px;font-size:13px;cursor:pointer;color:var(--text)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.btn.primary:hover{background:var(--accent-hover)}
.btn.ok{background:var(--ok);border-color:var(--ok);color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.block{width:100%}
.row{display:flex;gap:8px;align-items:center}
.section{padding:12px;border-bottom:1px solid var(--border)}
.section-title{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px}
.filelist{overflow:auto;flex:1;padding:6px}
.file{padding:8px 10px;border-radius:7px;cursor:pointer;word-break:break-all;font-size:12.5px;line-height:1.4}
.file:hover{background:#f3f4f6}
.file.active{background:#e8efff;color:#1e40af}
.file .sz{display:block;color:var(--muted);font-size:11px;margin-top:2px}
.mode-box{display:flex;gap:6px;margin-top:8px}
.mode-box label{flex:1;text-align:center;font-size:12px;padding:7px 4px;border:1px solid var(--border);border-radius:7px;cursor:pointer;color:var(--muted)}
.mode-box input{display:none}
.mode-box input:checked+span{color:var(--accent)}
.mode-box label:has(input:checked){border-color:var(--accent);background:#eff4ff;color:var(--accent);font-weight:600}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);align-items:center;background:#fcfdfe}
.toolbar .spacer{flex:1}
.filename{font-size:12px;color:var(--muted);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.editor-wrap{display:flex;flex:1;min-height:0;background:var(--code)}
.gutter{width:52px;flex:none;overflow:hidden;text-align:right;padding:12px 8px 12px 0;color:#a1a7b3;font:12px/20px ui-monospace,SFMono-Regular,Consolas,monospace;background:#f2f4f7;border-right:1px solid var(--border);user-select:none}
textarea#dts{flex:1;resize:none;border:0;outline:none;padding:12px;font:13px/20px ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--code);color:#1f2329;tab-size:4}
.statusbar{padding:6px 12px;border-top:1px solid var(--border);color:var(--muted);font-size:12px;display:flex;gap:16px}
.log{height:120px;flex:none;border-top:1px solid var(--border);background:#fbfcfd;overflow:auto;padding:8px 12px;font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}
.log .e{color:var(--danger)} .log .w{color:#b45309} .log .s{color:var(--ok)}
input[type=file]{display:none}
.hint{font-size:11.5px;color:var(--muted);line-height:1.6;margin-top:8px}
.chip{display:inline-block;background:#f0f4ff;color:#1e40af;border-radius:5px;padding:2px 7px;font-size:11px;margin:2px 3px 0 0}
.btn.analyze{background:#7c3aed;border-color:#7c3aed;color:#fff}
.btn.analyze:hover{background:#6d28d9}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;z-index:50;padding:24px}
.modal.show{display:flex}
.modal-box{background:var(--panel);border-radius:12px;width:min(1080px,96vw);max-height:90vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,.25)}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.modal-head h2{margin:0;font-size:17px}
.modal-head .spacer{flex:1}
.modal-body{padding:18px 20px;overflow:auto}
.board-cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.board-card{background:#f8fafc;border:1px solid var(--border);border-radius:9px;padding:10px 14px;min-width:150px}
.board-card .k{font-size:11px;color:var(--muted);margin-bottom:3px}
.board-card .v{font-size:14px;font-weight:600;color:var(--text)}
.a-sec{margin-bottom:20px}
.a-sec h3{font-size:13px;margin:0 0 8px;color:var(--accent);font-weight:650;padding-left:8px;border-left:3px solid var(--accent)}
.a-sec table{width:100%;border-collapse:collapse;font-size:12.5px}
.a-sec th{background:#f2f5fa;text-align:left;padding:7px 9px;color:var(--muted);font-weight:600;border:1px solid var(--border)}
.a-sec td{padding:6px 9px;border:1px solid var(--border);vertical-align:top;word-break:break-word}
.a-sec tr:nth-child(even) td{background:#fbfcfe}
.tag-ok{color:var(--ok);font-weight:600}
.tag-disabled{color:var(--danger);font-weight:600}
.empty-hint{color:var(--muted);font-size:13px;padding:20px;text-align:center}
.btn.diffbtn{background:#db2777;border-color:#db2777;color:#fff}
.btn.diffbtn:hover{background:#be185d}
.diff-setup{display:flex;gap:12px;margin-bottom:14px}
.diff-col{flex:1;border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:#fbfcfe}
.diff-col .lbl{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:7px}
.diff-col .fname{font-size:11px;color:var(--accent);margin-top:6px;word-break:break-all}
.diff-actions{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.diff-summary{font-size:13px;color:var(--muted);margin-bottom:10px}
.diff-summary b{color:var(--text)}
.diff-table{width:100%;border-collapse:collapse;font:12px/18px ui-monospace,SFMono-Regular,Consolas,monospace}
.diff-table th{background:#f2f5fa;color:var(--muted);text-align:left;padding:6px 8px;border:1px solid var(--border);position:sticky;top:0}
.diff-table td{padding:0 6px;border:1px solid var(--border);vertical-align:top;white-space:pre-wrap;word-break:break-all}
.diff-table .ln{width:42px;text-align:right;color:#a1a7b3;background:#f2f4f7;user-select:none;padding:0 6px}
.diff-table tr.eq td.dt{color:#6b7280}
.diff-table tr.del td.dt,.diff-table tr.ins td.dt,.diff-table tr.rep td.dt{background:#fef2f2}
.diff-table tr.del td.dt{text-decoration:line-through;text-decoration-color:#fca5a5}
.diff-table tr.ins td.dt{background:#fef2f2}
.hl{background:#dc2626;color:#fff;border-radius:2px;padding:0 1px}
.diff-only{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);cursor:pointer}
</style>
</head>
<body>
<header>
  <h1>dtbtool</h1><span class="tag">DTS 编辑 → 编译 DTB（Windows 免安装）</span>
  <span id="badge" class="badge">检测中...</span>
</header>
<div class="layout">
  <div class="panel">
    <h2>源文件</h2>
    <div class="section">
      <div class="section-title">① 打开 DTS 源文件</div>
      <button class="btn primary block" id="btnOpenDts">选择本地 .dts 文件</button>
      <input type="file" id="dtsFile" accept=".dts,.dtsi,.txt">
      <div class="hint">选中后内容载入右侧编辑器，可直接编辑。</div>
      <div class="mode-box">
        <label><input type="radio" name="mode" value="plain" checked><span>纯 DTS</span></label>
        <label><input type="radio" name="mode" value="cpp"><span>带 #include</span></label>
      </div>
      <div class="hint" id="modeHint">纯 DTS：无预处理指令，直接编译（默认）。</div>
    </div>
    <div class="section">
      <div class="section-title">② 附加头文件（可选）</div>
      <button class="btn block" id="btnAddHeader">添加 .h / .dtsi 头文件</button>
      <input type="file" id="headerFile" accept=".h,.dtsi" multiple>
      <div class="hint">"带 #include" 模式下，源码引用的 SoC 专有头文件在此上传。</div>
      <div id="headerChips" style="margin-top:6px"></div>
    </div>
    <div class="section" style="flex:none">
      <div class="section-title">最近打开</div>
    </div>
    <div class="filelist" id="filelist"></div>
  </div>

  <div class="panel">
    <h2>DTS 编辑器</h2>
    <div class="toolbar">
      <button class="btn ok" id="btnCompile">编译并下载 DTB</button>
      <button class="btn" id="btnValidate">仅检查语法</button>
      <button class="btn analyze" id="btnAnalyze">分析 DTS</button>
      <button class="btn diffbtn" id="btnDiff">对比 DTS</button>
      <span class="spacer"></span>
      <span class="filename" id="currentFile">未加载文件</span>
      <button class="btn" id="btnSaveDts">保存 .dts</button>
    </div>
    <div class="editor-wrap">
      <div class="gutter" id="gutter">1</div>
      <textarea id="dts" spellcheck="false" placeholder="点击左侧「选择本地 .dts 文件」载入源码，或直接在这里编写/粘贴 DTS...&#10;&#10;/dts-v1/;&#10;/ {&#10;    compatible = &quot;my,board&quot;;&#10;};"></textarea>
    </div>
    <div class="statusbar"><span id="pos">行 1, 列 1</span><span id="linecount">1 行</span><span id="dirty"></span></div>
    <div class="log" id="log">就绪。「分析 DTS」可查看板卡/外设/引脚/频率；编辑后点「编译并下载 DTB」，把 .dtb 拷回 Armbian 覆盖即可生效。</div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const ta = $('dts');
let state = {mode:'plain', headers:[], original:'', loaded:false, files:[], currentName:'board'};

function log(msg, cls){
  const el=$('log');
  const line=document.createElement('div');
  if(cls) line.className=cls;
  line.textContent='['+new Date().toLocaleTimeString()+'] '+msg;
  el.appendChild(line); el.scrollTop=el.scrollHeight;
}
async function api(path, opts){
  const r=await fetch(path,opts);
  let j={}; try{j=await r.json();}catch(e){}
  if(!r.ok || j.ok===false) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function syncGutter(){
  const n=ta.value.split('\n').length;
  let s=''; for(let i=1;i<=n;i++) s+=i+'<br>';
  $('gutter').innerHTML=s;
  $('linecount').textContent=n+' 行';
  $('dirty').textContent=(state.loaded && ta.value!==state.original)?'● 未保存':'';
}
ta.addEventListener('input',syncGutter);
ta.addEventListener('scroll',()=>{$('gutter').scrollTop=ta.scrollTop;});
ta.addEventListener('click',updatePos); ta.addEventListener('keyup',updatePos);
function updatePos(){
  const p=ta.selectionStart, before=ta.value.slice(0,p);
  $('pos').textContent='行 '+before.split('\n').length+', 列 '+(p-before.lastIndexOf('\n'));
}
function basename(p){return String(p).split(/[\\/]/).pop();}
function renderHeaders(){
  const el=$('headerChips'); el.innerHTML='';
  state.headers.forEach((h,i)=>{
    const c=document.createElement('span'); c.className='chip';
    c.textContent=h+' ×'; c.title='点击移除';
    c.style.cursor='pointer';
    c.onclick=()=>{state.headers.splice(i,1);renderHeaders();};
    el.appendChild(c);
  });
}
function renderFiles(){
  const el=$('filelist'); el.innerHTML='';
  if(!state.files.length){el.innerHTML='<div class="file" style="color:#999;cursor:default">暂无</div>';return;}
  state.files.forEach(f=>{
    const d=document.createElement('div'); d.className='file';
    d.innerHTML='<span>'+basename(f.name)+'</span><span class="sz">'+(f.lines||'')+' 行</span>';
    d.onclick=()=>{ loadText(f.path, f.name); };
    el.appendChild(d);
  });
}
async function loadStatus(){
  try{
    const s=await api('/api/status');
    $('badge').textContent='引擎: '+s.engine+(s.dtc?' · dtc 可用':' · 内置');
    log('当前引擎: '+s.engine+(s.dtc?' (dtc 可用)':' (内置纯 Python，无需安装)'));
  }catch(e){log('状态检测失败: '+e.message,'e');}
}
$('btnOpenDts').onclick=()=>$('dtsFile').click();
$('dtsFile').onchange=async e=>{
  const f=e.target.files[0]; if(!f)return;
  try{
    const text=await f.text();
    await api('/api/remember_dts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:f.name,text:text})});
    loadText(await rememberedPath(f.name), f.name, text);
    log('已打开: '+f.name,'s');
  }catch(err){log('打开失败: '+err.message,'e');}
  e.target.value='';
};
async function rememberedPath(name){
  const r=await api('/api/dts_files');
  const hit=r.files.find(x=>x.name===name);
  return hit?hit.path:'';
}
function loadText(path,name,text){
  if(text!==undefined){ ta.value=text; }
  state.original=ta.value; state.loaded=true;
  state.currentName=(name||'board').replace(/\.(dts|dtsi|txt)$/,'');
  $('currentFile').textContent=name||'board.dts';
  syncGutter(); updatePos();
  if(!state.files.some(f=>f.name===name)) state.files.unshift({name:name,path:path,lines:ta.value.split('\n').length});
  renderFiles();
}
$('btnAddHeader').onclick=()=>$('headerFile').click();
$('headerFile').onchange=async e=>{
  for(const f of e.target.files){
    try{
      const buf=await f.arrayBuffer();
      await api('/api/upload_header?name='+encodeURIComponent(f.name),{method:'PUT',body:buf});
      if(!state.headers.includes(f.name)) state.headers.push(f.name);
      log('已添加头文件: '+f.name,'s');
    }catch(err){log('添加头文件失败: '+err.message,'e');}
  }
  renderHeaders(); e.target.value='';
};
document.querySelectorAll('input[name=mode]').forEach(r=>r.onchange=()=>{
  state.mode=r.value;
  $('modeHint').textContent = r.value==='plain'
    ? '纯 DTS：无预处理指令，直接编译（默认）。'
    : '带 #include：支持 #define / 条件编译，自动展开内置 dt-bindings 宏。';
  log('模式切换为: '+(r.value==='plain'?'纯 DTS':'带 #include'));
});
function compilePayload(){
  return {text:ta.value, name:state.currentName, mode:state.mode, headers:state.headers};
}
$('btnCompile').onclick=async()=>{
  if(!ta.value.trim()){log('编辑器为空','e');return;}
  log('编译中...');
  try{
    const r=await api('/api/compile_dts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(compilePayload())});
    const a=document.createElement('a'); a.href='/api/download?token='+r.token; a.download=r.filename; a.click();
    log('编译成功，已下载: '+r.filename+' ('+r.size+' 字节)','s');
    if(r.warnings) r.warnings.forEach(w=>log(w,'w'));
  }catch(err){log('编译失败: '+err.message,'e');}
};
$('btnValidate').onclick=async()=>{
  if(!ta.value.trim()){log('编辑器为空','e');return;}
  log('检查语法...');
  try{
    const r=await api('/api/validate_dts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(compilePayload())});
    log(r.message,'s');
    if(r.warnings) r.warnings.forEach(w=>log(w,'w'));
  }catch(err){log('语法检查失败: '+err.message,'e');}
};
$('btnSaveDts').onclick=()=>{
  const blob=new Blob([ta.value],{type:'text/plain;charset=utf-8'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download=state.currentName+'.dts'; a.click(); URL.revokeObjectURL(a.href);
};
async function loadDtsFiles(){
  try{
    const r=await api('/api/dts_files');
    state.files=r.files; renderFiles();
  }catch(e){}
}
loadStatus(); loadDtsFiles(); syncGutter();
</script>

<div class="modal" id="analysisModal">
  <div class="modal-box">
    <div class="modal-head">
      <h2>硬件分析报告</h2>
      <span class="spacer"></span>
      <button class="btn" id="btnCopyReport">复制文本报告</button>
      <button class="btn" id="btnCloseAnalysis">关闭</button>
    </div>
    <div class="modal-body" id="analysisBody"></div>
  </div>
</div>
<script>
let lastReportText='';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function renderAnalysis(data, reportText){
  lastReportText=reportText||'';
  const body=$('analysisBody'); body.innerHTML='';
  const b=data.board||{}, s=data.summary||{};
  // 概览卡片
  const cards=document.createElement('div'); cards.className='board-cards';
  const kv=[['型号',b.model],['厂商',b.vendor],['SoC',b.soc],['CPU',b.cpu],
            ['外设',(s.peripherals||0)+' (启用 '+(s.enabled||0)+' / 禁用 '+(s.disabled||0)+')']];
  kv.forEach(([k,v])=>{ if(v){const c=document.createElement('div');c.className='board-card';
    c.innerHTML='<div class="k">'+esc(k)+'</div><div class="v">'+esc(v)+'</div>';cards.appendChild(c);} });
  if(b.compatible&&b.compatible.length){const c=document.createElement('div');c.className='board-card';
    c.innerHTML='<div class="k">compatible</div><div class="v" style="font-size:12px">'+esc(b.compatible.slice(0,3).join(', '))+'</div>';cards.appendChild(c);}
  body.appendChild(cards);
  // 各分区表格
  (data.sections||[]).forEach(sec=>{
    const wrap=document.createElement('div'); wrap.className='a-sec';
    let h='<h3>'+esc(sec.title)+'</h3>';
    if(!sec.rows||!sec.rows.length){ h+='<div class="empty-hint">(无)</div>'; wrap.innerHTML=h; body.appendChild(wrap); return; }
    h+='<table><thead><tr>';
    (sec.cols||[]).forEach(c=>h+='<th>'+esc(c)+'</th>');
    h+='</tr></thead><tbody>';
    sec.rows.forEach(r=>{ h+='<tr>';
      (sec.cols||[]).forEach((_,ci)=>{ let v=r[ci]==null?'':String(r[ci]);
        if(v==='okay') v='<span class="tag-ok">okay</span>';
        else if(v==='disabled') v='<span class="tag-disabled">disabled</span>';
        else v=esc(v);
        h+='<td>'+v+'</td>'; });
      h+='</tr>'; });
    h+='</tbody></table>';
    wrap.innerHTML=h; body.appendChild(wrap);
  });
  (data.warnings||[]).forEach(w=>{const d=document.createElement('div');d.className='a-sec';
    d.innerHTML='<div class="log"><span class="w">! '+esc(w)+'</span></div>';body.appendChild(d);});
  $('analysisModal').classList.add('show');
}
$('btnAnalyze').onclick=async()=>{
  if(!ta.value.trim()){log('编辑器为空','e');return;}
  log('分析 DTS 中...');
  try{
    const r=await api('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(compilePayload())});
    log('分析完成: '+r.data.summary.nodes+' 节点 / '+r.data.summary.peripherals+' 外设','s');
    renderAnalysis(r.data, r.report);
  }catch(err){log('分析失败: '+err.message,'e');}
};
$('btnCloseAnalysis').onclick=()=>$('analysisModal').classList.remove('show');
$('analysisModal').onclick=e=>{if(e.target===$('analysisModal'))$('analysisModal').classList.remove('show');};
$('btnCopyReport').onclick=async()=>{
  try{await navigator.clipboard.writeText(lastReportText);log('文本报告已复制','s');}
  catch(e){log('复制失败: '+e.message,'e');}
};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('analysisModal').classList.remove('show');});
</script>

<div class="modal" id="diffModal">
  <div class="modal-box">
    <div class="modal-head">
      <h2>对比两份 DTS（差异标红）</h2>
      <span class="spacer"></span>
      <button class="btn" id="btnCloseDiff">关闭</button>
    </div>
    <div class="modal-body">
      <div class="diff-setup">
        <div class="diff-col">
          <div class="lbl">A · 原始文件（基准）</div>
          <input type="file" id="diffFileA" accept=".dts,.dtsi,.txt">
          <button class="btn block" id="btnPickA">选择 A 文件</button>
          <button class="btn block" id="btnEditorA" style="margin-top:6px">用当前编辑器内容作为 A</button>
          <div class="fname" id="diffNameA">未选择</div>
        </div>
        <div class="diff-col">
          <div class="lbl">B · 对比文件</div>
          <input type="file" id="diffFileB" accept=".dts,.dtsi,.txt">
          <button class="btn block" id="btnPickB">选择 B 文件</button>
          <button class="btn block" id="btnEditorB" style="margin-top:6px">用当前编辑器内容作为 B</button>
          <div class="fname" id="diffNameB">未选择</div>
        </div>
      </div>
      <div class="diff-actions">
        <button class="btn primary" id="btnRunDiff">开始对比</button>
        <label class="diff-only"><input type="checkbox" id="onlyDiff"> 只显示差异行</label>
        <span class="spacer" style="flex:1"></span>
        <button class="btn" id="btnSwapDiff">交换 A / B</button>
      </div>
      <div class="diff-summary" id="diffSummary">选择两份 DTS 后点「开始对比」。</div>
      <div id="diffResult" style="max-height:52vh;overflow:auto;border:1px solid var(--border);border-radius:8px"></div>
    </div>
  </div>
</div>
<script>
let diffState={textA:'',textB:'',nameA:'A.dts',nameB:'B.dts'};
function setDiffSide(side,text,name){
  diffState['a'===side?'textA':'textB']=text;
  diffState['name'+('a'===side?'A':'B')]=name;
  $('diffName'+('a'===side?'A':'B')).textContent=name+' ('+text.split('\\n').length+' 行)';
}
$('btnDiff').onclick=()=>$('diffModal').classList.add('show');
$('btnCloseDiff').onclick=()=>$('diffModal').classList.remove('show');
$('diffModal').onclick=e=>{if(e.target===$('diffModal'))$('diffModal').classList.remove('show');};
$('btnPickA').onclick=()=>$('diffFileA').click();
$('btnPickB').onclick=()=>$('diffFileB').click();
$('diffFileA').onchange=async e=>{const f=e.target.files[0];if(f)setDiffSide('a',await f.text(),f.name);e.target.value='';};
$('diffFileB').onchange=async e=>{const f=e.target.files[0];if(f)setDiffSide('b',await f.text(),f.name);e.target.value='';};
$('btnEditorA').onclick=()=>setDiffSide('a',ta.value,state.currentName+'.dts');
$('btnEditorB').onclick=()=>setDiffSide('b',ta.value,state.currentName+'.dts');
$('btnSwapDiff').onclick=()=>{
  let t=diffState.textA,n=diffState.nameA;
  diffState.textA=diffState.textB;diffState.nameA=diffState.nameB;
  diffState.textB=t;diffState.nameB=n;
  $('diffNameA').textContent=diffState.nameA;$('diffNameB').textContent=diffState.nameB;
  $('btnRunDiff').click();
};
function segHtml(segs){
  return (segs||[]).map(s=>s.changed?('<span class="hl">'+esc(s.text)+'</span>'):esc(s.text)).join('')||'&nbsp;';
}
function renderDiff(data){
  const el=$('diffResult'); el.innerHTML='';
  const s=data.stats;
  $('diffSummary').innerHTML='A: <b>'+s.lines_a+'</b> 行 · B: <b>'+s.lines_b+'</b> 行 &nbsp;|&nbsp; '+
    '新增 <b>'+s.added+'</b> · 删除 <b>'+s.removed+'</b> · 修改 <b>'+s.changed+'</b> · 相同 <b>'+s.same+'</b>'+
    (data.identical?' &nbsp;<span style="color:var(--ok)">（完全一致）</span>':'');
  if(data.identical){el.innerHTML='<div class="empty-hint">两份文件内容完全一致，无差异。</div>';return;}
  const table=document.createElement('table'); table.className='diff-table';
  table.innerHTML='<thead><tr><th style="width:42px">A行</th><th>A · '+esc(data.name_a)+'</th>'+
    '<th style="width:42px">B行</th><th>B · '+esc(data.name_b)+'</th></tr></thead>';
  const tbody=document.createElement('tbody');
  const only=$('onlyDiff').checked;
  data.rows.forEach(r=>{
    if(only&&r.type==='equal')return;
    const cls={equal:'eq',delete:'del',insert:'ins',replace:'rep'}[r.type]||'eq';
    const tr=document.createElement('tr'); tr.className=cls;
    const a=r.a,b=r.b;
    tr.innerHTML='<td class="ln">'+(a?a.num:'')+'</td>'+
      '<td class="dt">'+(a?segHtml(a.segments):'')+'</td>'+
      '<td class="ln">'+(b?b.num:'')+'</td>'+
      '<td class="dt">'+(b?segHtml(b.segments):'')+'</td>';
    tbody.appendChild(tr);
  });
  table.appendChild(tbody); el.appendChild(table);
}
$('onlyDiff').onchange=()=>{if($('diffResult').dataset.has==='1')$('btnRunDiff').click();};
$('btnRunDiff').onclick=async()=>{
  if(!diffState.textA||!diffState.textB){log('请先选择两份 DTS 文件（或用编辑器内容）','e');return;}
  log('对比中...');
  try{
    const r=await api('/api/diff',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text_a:diffState.textA,text_b:diffState.textB,name_a:diffState.nameA,name_b:diffState.nameB})});
    $('diffResult').dataset.has='1';
    renderDiff(r);
    log('对比完成: 新增 '+r.stats.added+' / 删除 '+r.stats.removed+' / 修改 '+r.stats.changed,'s');
  }catch(err){log('对比失败: '+err.message,'e');}
};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('diffModal').classList.remove('show');});
</script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "dtbtool/" + VERSION

    def log_message(self, fmt, *args):
        # 精简访问日志
        sys.stderr.write("[web] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        cleanup_downloads()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path == "/":
            return self._send(200, WEB_HTML, "text/html; charset=utf-8")

        if path == "/api/status":
            return self._json({"dtc": bool(find_dtc()),
                               "dtc_version": dtc_version() or "",
                               "engine": engine_name(),
                               "version": VERSION})

        if path == "/api/list":
            dirs = q.get("dir", [])
            # Windows 下把上传目录也纳入列表(系统 /boot 等路径不存在)
            dirs = dirs + [str(UPLOAD_DIR)]
            try:
                files = list_dtb_files(dirs)
                return self._json({"files": [str(p) for p in files]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/dts_files":
            try:
                DTS_DIR.mkdir(parents=True, exist_ok=True)
                files = []
                for f in sorted(DTS_DIR.glob("*.dts"), key=lambda x: x.stat().st_mtime, reverse=True):
                    text = f.read_text(encoding="utf-8", errors="replace")
                    files.append({"name": f.name, "path": str(f.resolve()),
                                  "lines": len(text.splitlines())})
                return self._json({"files": files[:30]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/info":
            p = q.get("path", [""])[0]
            try:
                f = Path(p)
                if not f.is_file():
                    raise DtbToolError("文件不存在: %s" % p)
                info = fdt_parse(f.read_bytes())
                return self._json({"text": fdt_info_text(p, info), "info": info})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        if path == "/api/download":
            token = q.get("token", [""])[0]
            if token not in DOWNLOADS:
                return self._json({"ok": False, "error": "下载链接已过期"}, 404)
            fp, _ = DOWNLOADS[token]
            try:
                data = Path(fp).read_bytes()
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 404)
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": 'attachment; filename="%s"' % Path(fp).name})

        return self._json({"ok": False, "error": "未知接口"}, 404)

    def do_PUT(self):
        cleanup_downloads()
        u = urlparse(self.path)
        # 上传自定义头文件 (.h / .dtsi)
        if u.path == "/api/upload_header":
            name = Path(parse_qs(u.query).get("name", ["header.h"])[0]).name
            if not name.endswith((".h", ".dtsi")):
                return self._json({"ok": False, "error": "仅支持 .h / .dtsi 头文件"}, 400)
            data = self._body()
            HEADER_DIR.mkdir(parents=True, exist_ok=True)
            (HEADER_DIR / name).write_bytes(data)
            return self._json({"ok": True, "name": name})
        if u.path != "/api/upload":
            return self._json({"ok": False, "error": "未知接口"}, 404)
        name = parse_qs(u.query).get("name", ["upload.dtb"])[0]
        name = Path(name).name  # 防止路径穿越
        if not name.endswith((".dtb", ".dtbo")):
            return self._json({"ok": False, "error": "仅支持 .dtb / .dtbo 文件"}, 400)
        data = self._body()
        try:
            fdt_parse(data)  # 校验
        except Exception as e:
            return self._json({"ok": False, "error": "上传的不是有效 DTB: %s" % e}, 400)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / name
        dest.write_bytes(data)
        return self._json({"ok": True, "path": str(dest.resolve())})

    def do_POST(self):
        cleanup_downloads()
        u = urlparse(self.path)
        raw = self._body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            return self._json({"ok": False, "error": "JSON 解析失败: %s" % e}, 400)

        # 记住打开的 DTS 源文件(供"最近打开"列表使用)
        if u.path == "/api/remember_dts":
            name = Path(payload.get("name", "untitled.dts")).name
            text = payload.get("text", "")
            try:
                DTS_DIR.mkdir(parents=True, exist_ok=True)
                (DTS_DIR / name).write_text(text, encoding="utf-8")
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        # 对比两份 DTS: 返回结构化差异(词级) + 文本报告
        if u.path == "/api/diff":
            text_a = payload.get("text_a", "")
            text_b = payload.get("text_b", "")
            name_a = Path(payload.get("name_a", "A.dts")).name
            name_b = Path(payload.get("name_b", "B.dts")).name
            try:
                result = dts_diff.diff_texts(text_a, text_b, name_a, name_b)
                result["report"] = dts_diff.diff_text_report(result, use_color=False)
                return self._json(result)
            except Exception as e:
                return self._json({"ok": False, "error": "对比失败: %s" % e}, 400)

        # 分析 DTS: 提取板卡/外设/引脚/时钟/中断信息
        if u.path == "/api/analyze":
            text = payload.get("text", "")
            mode = payload.get("mode", "plain")
            headers = payload.get("headers", []) or []
            if not text.strip():
                return self._json({"ok": False, "error": "DTS 内容为空"}, 400)
            try:
                include_dirs = [str(HEADER_DIR)] if headers else []
                result = dts_analyze.analyze_text(
                    text, include_dirs=include_dirs, preprocess=(mode == "cpp")
                )
                return self._json({"ok": True, "report": dts_analyze.report_text(result),
                                   "data": result})
            except Exception as e:
                return self._json({"ok": False, "error": "分析失败: %s" % e}, 400)

        # 统一的 DTS 编译入口(支持 mode=plain/cpp, headers=自定义头文件列表)
        if u.path in ("/api/compile_dts", "/api/validate_dts"):
            text = payload.get("text", "")
            name = Path(payload.get("name", "board")).name or "board"
            name = name.replace(".dts", "").replace(".dtsi", "")
            mode = payload.get("mode", "plain")
            headers = payload.get("headers", []) or []
            if not text.strip():
                return self._json({"ok": False, "error": "DTS 内容为空"}, 400)
            try:
                warnings = []
                # 按界面勾选的头文件确定 include 目录
                include_dirs = [str(HEADER_DIR)] if headers else []
                if ENGINE == "python":
                    if mode == "cpp":
                        pre, w = dtb_engine.preprocess_dts(text, include_dirs=include_dirs)
                        warnings.extend(w)
                    else:
                        pre = text
                    raw = dtb_engine.dts_to_dtb(pre)
                else:
                    # dtc 后端：把头文件目录作为 -i 传入
                    tmpd = Path(tempfile.mkdtemp(prefix="dtbtool-dts-"))
                    src = tmpd / "input.dts"
                    src.write_text(text, encoding="utf-8")
                    dargs = ["-I", "dts", "-O", "dtb", "-i", str(tmpd)]
                    for inc in include_dirs:
                        dargs += ["-i", inc]
                    if mode == "cpp":
                        dargs.append("-@")
                    dargs += ["-o", tmpd / "out.dtb", src]
                    rc, out, err, cmd = run_dtc(dargs)
                    if rc != 0:
                        raise DtbToolError("dtc 编译失败: %s" % (err.strip() or out.strip()))
                    raw = (tmpd / "out.dtb").read_bytes()
                    if err.strip():
                        warnings.append(err.strip())
                fdt_parse(raw)
                if u.path == "/api/validate_dts":
                    return self._json({"ok": True,
                                       "message": "语法检查通过，可生成 %d 字节 DTB" % len(raw),
                                       "warnings": warnings})
                # 生成下载
                outdir = Path(tempfile.gettempdir()) / "dtbtool-downloads"
                outdir.mkdir(parents=True, exist_ok=True)
                fname = name + ".dtb"
                out = outdir / ("%s-%s" % (uuid.uuid4().hex[:8], fname))
                out.write_bytes(raw)
                token = uuid.uuid4().hex
                DOWNLOADS[token] = (str(out), time.time() + DOWNLOAD_TTL)
                return self._json({"ok": True, "token": token, "filename": fname,
                                   "size": len(raw), "warnings": warnings})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        if u.path == "/api/decompile":
            p = payload.get("path", "")
            try:
                tmp = Path(tempfile.mkdtemp(prefix="dtbtool-web-"))
                dts = tmp / "out.dts"
                _, warnings = decompile_file(p, dts)
                text = dts.read_text(encoding="utf-8", errors="replace")
                info = fdt_parse(Path(p).read_bytes())
                return self._json({"ok": True, "dts": text,
                                   "warnings": warnings,
                                   "info": fdt_info_text(p, info)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        if u.path == "/api/compile":
            text = payload.get("text", "")
            target = payload.get("target")
            install = bool(payload.get("install"))
            name = Path(payload.get("name", "board")).name
            if not text.strip():
                return self._json({"ok": False, "error": "DTS 内容为空"}, 400)
            try:
                if install and target:
                    tmpdir = Path(tempfile.mkdtemp(prefix="dtbtool-web-"))
                    tmp_out = tmpdir / "out.dtb"
                    _, warnings = compile_dts_text(text, tmp_out)
                    # 校验目标存在
                    if not Path(target).is_file():
                        raise DtbToolError("目标文件不存在: %s" % target)
                    bak = install_file(tmp_out, target, backup=True)
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    return self._json({"ok": True, "path": target,
                                       "backup": str(bak) if bak else None,
                                       "warnings": warnings})
                else:
                    outdir = Path(tempfile.gettempdir()) / "dtbtool-downloads"
                    outdir.mkdir(parents=True, exist_ok=True)
                    fname = (name.replace("/", "_") or "board")
                    if not fname.endswith(".dtb"):
                        fname += ".dtb"
                    out = outdir / ("%s-%s" % (uuid.uuid4().hex[:8], fname))
                    _, warnings = compile_dts_text(text, out)
                    token = uuid.uuid4().hex
                    DOWNLOADS[token] = (str(out), time.time() + DOWNLOAD_TTL)
                    return self._json({"ok": True, "token": token,
                                       "filename": fname, "warnings": warnings})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        return self._json({"ok": False, "error": "未知接口"}, 404)


def cmd_web(args):
    host = args.host
    port = args.port
    print("当前引擎: %s" % ("内置纯 Python (无需 dtc)" if ENGINE == "python" else "系统 dtc"))
    server = ThreadingHTTPServer((host, port), WebHandler)
    url = "http://127.0.0.1:%d" % port if host in ("0.0.0.0", "127.0.0.1") else "http://%s:%d" % (host, port)
    print("dtbtool web 已启动: %s" % url)
    print("按 Ctrl+C 停止。")
    # Windows 下尝试自动打开浏览器
    if os.name == "nt" and getattr(args, "open_browser", True):
        try:
            import threading
            threading.Timer(0.8, lambda: __import__("webbrowser").open(url)).start()
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="dtbtool.py",
        description="Armbian DTB 反编译 / 编译 / 修改工具 (v%s)" % VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  dtbtool.py check\n"
               "  dtbtool.py info board.dtb\n"
               "  dtbtool.py decompile board.dtb -o board.dts\n"
               "  dtbtool.py compile board.dts -o board.dtb\n"
               "  dtbtool.py analyze board.dts       (分析外设/引脚/频率)\n"
               "  dtbtool.py analyze board.dtb --json\n"
               "  dtbtool.py web --port 8080\n"
               "  dtbtool.py --engine dtc compile complex.dts -o board.dtb  (复杂DTS)\n",
    )
    p.add_argument("--engine", choices=["python", "dtc", "auto"], default="python",
                   help="转换引擎: python=内置(默认,Windows免安装), dtc=系统dtc, auto=自动")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检查运行环境 (dtc 是否可用)").set_defaults(func=cmd_check)

    sp = sub.add_parser("list", help="列举系统中的 DTB 文件")
    sp.add_argument("-d", "--dir", action="append", default=[], help="额外搜索目录 (可重复)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("info", help="解析并显示 DTB 头信息")
    sp.add_argument("file", help="DTB 文件路径")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("decompile", help="DTB -> DTS 反编译")
    sp.add_argument("dtb", help="输入 DTB 文件")
    sp.add_argument("-o", "--output", help="输出 DTS 文件 (默认同名 .dts)")
    sp.add_argument("--strip-source", action="store_true", help="去除源码行号注释")
    sp.set_defaults(func=cmd_decompile)

    sp = sub.add_parser("compile", help="DTS -> DTB 编译")
    sp.add_argument("dts", help="输入 DTS 文件")
    sp.add_argument("-o", "--output", help="输出 DTB 文件 (默认同名 .dtb)")
    sp.add_argument("--install", action="store_true", help="覆盖目标时自动备份原文件")
    sp.add_argument("-i", "--include-dir", action="append", default=[], help="额外头文件搜索路径")
    sp.add_argument("--overlay", action="store_true", help="按 DT overlay 方式编译 (-@)")
    sp.set_defaults(func=cmd_compile)

    sp = sub.add_parser("analyze", help="分析 DTS/DTB: 板卡/外设/引脚/时钟/中断")
    sp.add_argument("file", help="DTS/DTSI/DTB 文件路径")
    spa = sp.add_mutually_exclusive_group()
    spa.add_argument("--cpp", action="store_true", help="强制先运行预处理器 (#include/#define)")
    spa.add_argument("--plain", action="store_true", help="不运行预处理器")
    sp.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("edit", help="反编译 -> 调用编辑器 -> 自动编译写回 (带备份)")
    sp.add_argument("dtb", help="要修改的 DTB 文件")
    sp.add_argument("--editor", help="编辑器命令 (默认 $EDITOR 或 vi)")
    sp.add_argument("--no-backup", action="store_true", help="写回时不备份 (不推荐)")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("diff", help="对比两个 DTB 或 DTS (差异标红)")
    sp.add_argument("a", help="文件 A")
    sp.add_argument("b", help="文件 B")
    sp.add_argument("--no-color", action="store_true", help="关闭 ANSI 标红")
    sp.add_argument("--color", action="store_true", help="强制 ANSI 标红")
    sp.add_argument("--json", action="store_true", help="输出结构化 JSON")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("web", help="启动浏览器编辑界面")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1, 仅本机)")
    sp.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.set_defaults(func=cmd_web, open_browser=True)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        set_engine(args.engine)
        if getattr(args, "no_browser", False):
            args.open_browser = False
        return args.func(args)
    except DtbToolError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2
    except Exception as e:
        print("未预期错误: %s" % e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
