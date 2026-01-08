#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ===== 可调参数 =====
TICK_MS = 10          # 0.01s = 10ms
MAKE_BACKUP = True    # 是否生成 .bak 备份
RECURSIVE = True      # 目录模式是否递归扫描子目录
# ====================

# 匹配时间戳：[mm:ss.xx] / [m:ss.xxx]，小数位可 1~3
TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

# 粗略识别“时间戳行”：至少包含一个 [mm:ss...]
def has_timestamp(line: str) -> bool:
    return TS_RE.search(line) is not None

# 过滤掉标签行（如 [ar:xx] [ti:xx]），但注意：[00:01.00] 这种也有冒号
# 标签行一般是 [xx: ...] 且 xx 不是纯数字分钟+秒格式
META_RE = re.compile(r"^\s*\[[a-zA-Z]{1,4}:.+\]\s*$")

def is_metadata_line(line: str) -> bool:
    if META_RE.match(line):
        return True
    return False

def parse_ts_to_ms(m: int, s: int, frac: Optional[str]) -> Tuple[int, int]:
    """
    返回 (time_ms, frac_width)
    frac_width: 原小数位数（1~3），如果没有小数则为 0
    """
    base = (m * 60 + s) * 1000
    if frac is None:
        return base, 0
    w = len(frac)
    f = int(frac)
    if w == 1:
        ms = f * 100
    elif w == 2:
        ms = f * 10
    else:  # w == 3
        ms = f
    return base + ms, w

def format_ms_to_ts(time_ms: int, frac_width: int) -> str:
    """
    按 frac_width 输出：
    - 2 -> [mm:ss.xx]
    - 3 -> [mm:ss.xxx]
    - 1 -> [mm:ss.x]
    - 0 -> [mm:ss]（不建议改动无小数的行）
    """
    if time_ms < 0:
        time_ms = 0
    total_sec, ms = divmod(time_ms, 1000)
    mm, ss = divmod(total_sec, 60)

    if frac_width <= 0:
        return f"[{mm:02d}:{ss:02d}]"

    if frac_width == 1:
        frac = ms // 100
        return f"[{mm:02d}:{ss:02d}.{frac:01d}]"
    elif frac_width == 2:
        frac = ms // 10
        return f"[{mm:02d}:{ss:02d}.{frac:02d}]"
    else:  # 3
        return f"[{mm:02d}:{ss:02d}.{ms:03d}]"

@dataclass
class LineInfo:
    raw: str
    first_time_ms: Optional[int]          # 第一枚时间戳的毫秒
    first_frac_width: int                 # 第一枚时间戳的小数位
    # 该行所有时间戳（用于替换），存储：[(span_start, span_end, time_ms, frac_width)]
    stamps: List[Tuple[int, int, int, int]]

def analyze_line(line: str) -> LineInfo:
    stamps = []
    for m in TS_RE.finditer(line):
        mm = int(m.group(1))
        ss = int(m.group(2))
        frac = m.group(3)
        tms, w = parse_ts_to_ms(mm, ss, frac)
        stamps.append((m.start(), m.end(), tms, w))

    if not stamps:
        return LineInfo(raw=line, first_time_ms=None, first_frac_width=0, stamps=[])

    return LineInfo(
        raw=line,
        first_time_ms=stamps[0][2],
        first_frac_width=stamps[0][3],
        stamps=stamps
    )

def replace_line_timestamps(line_info: LineInfo, target_time_ms: int, new_time_ms: int) -> str:
    """
    把该行中所有 time_ms == target_time_ms 的时间戳替换为 new_time_ms（保持原小数位）
    """
    if not line_info.stamps:
        return line_info.raw

    chars = list(line_info.raw)
    # 逆序替换，避免 span 位移
    for start, end, tms, w in reversed(line_info.stamps):
        if w <= 0:
            continue  # 无小数的时间戳，默认不动
        if tms != target_time_ms:
            continue
        new_ts = format_ms_to_ts(new_time_ms, w)
        chars[start:end] = list(new_ts)
    return "".join(chars)

def process_lines_modify(lines: List[str]) -> Tuple[List[str], int]:
    """
    修改：连续相同时间戳 -> 第一行减去 0.01s
    已经是（当前 + 0.01s == 下一行）则跳过，避免重复修改。
    """
    infos = [analyze_line(ln) for ln in lines]
    out = lines[:]
    changed = 0

    for i in range(len(lines) - 1):
        a = infos[i]
        b = infos[i + 1]

        if a.first_time_ms is None or b.first_time_ms is None:
            continue
        if is_metadata_line(a.raw) or is_metadata_line(b.raw):
            continue
        # 只对带小数的时间戳动刀（否则你会得到奇怪的“无小数也能 -0.01”问题）
        if a.first_frac_width <= 0 or b.first_frac_width <= 0:
            continue

        # 如果已经是“错开 0.01s”的修复形态，就跳过
        if a.first_time_ms + TICK_MS == b.first_time_ms:
            continue

        # 目标：连续相同时间戳
        if a.first_time_ms == b.first_time_ms:
            t = a.first_time_ms
            new_t = t - TICK_MS
            if new_t < 0:
                continue

            # 避免改完之后，反而撞上上一行（形成新的重复/倒序）
            # 这里最多再退几 tick（一般不会发生，除非你有三连相同时间戳）
            prev_time = infos[i - 1].first_time_ms if i - 1 >= 0 else None
            while prev_time is not None and new_t == prev_time and new_t >= TICK_MS:
                new_t -= TICK_MS

            # 真要退到 0 还撞上 prev，那就算了（不强行制造时间穿越）
            if prev_time is not None and new_t == prev_time:
                continue

            new_line = replace_line_timestamps(a, t, new_t)
            if new_line != out[i]:
                out[i] = new_line
                # 更新 infos，保证后续判断一致
                infos[i] = analyze_line(new_line)
                changed += 1

    return out, changed

def process_lines_restore(lines: List[str]) -> Tuple[List[str], int]:
    """
    还原：检测到（当前 + 0.01s == 下一行） -> 当前改回与下一行相同
    """
    infos = [analyze_line(ln) for ln in lines]
    out = lines[:]
    changed = 0

    for i in range(len(lines) - 1):
        a = infos[i]
        b = infos[i + 1]

        if a.first_time_ms is None or b.first_time_ms is None:
            continue
        if is_metadata_line(a.raw) or is_metadata_line(b.raw):
            continue
        if a.first_frac_width <= 0 or b.first_frac_width <= 0:
            continue

        # 还原条件：正好差一个 tick
        if a.first_time_ms + TICK_MS == b.first_time_ms:
            # 将 a 的时间戳改回 b 的时间
            new_line = replace_line_timestamps(a, a.first_time_ms, b.first_time_ms)
            if new_line != out[i]:
                out[i] = new_line
                infos[i] = analyze_line(new_line)
                changed += 1

    return out, changed

def collect_lrc_files(path: str) -> List[str]:
    path = os.path.abspath(path)
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".lrc") else []
    if not os.path.isdir(path):
        return []

    files = []
    if RECURSIVE:
        for root, _, names in os.walk(path):
            for n in names:
                if n.lower().endswith(".lrc"):
                    files.append(os.path.join(root, n))
    else:
        for n in os.listdir(path):
            p = os.path.join(path, n)
            if os.path.isfile(p) and n.lower().endswith(".lrc"):
                files.append(p)
    return files

def read_text_file(fp: str) -> Tuple[str, List[str]]:
    # 尽量兼容各种歌词文件编码：utf-8-sig -> utf-8 -> gbk(兜底)
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(fp, "r", encoding=enc, newline="") as f:
                text = f.read()
            # 保留原换行风格：splitlines(True) 会保留 \n
            return enc, text.splitlines(True)
        except UnicodeDecodeError:
            continue
    # 实在不行就强行替换
    with open(fp, "r", encoding="utf-8", errors="replace", newline="") as f:
        text = f.read()
    return "utf-8(replace)", text.splitlines(True)

def write_text_file(fp: str, lines: List[str], encoding: str) -> None:
    with open(fp, "w", encoding=encoding.replace("(replace)", ""), newline="") as f:
        f.writelines(lines)

def backup_file(fp: str) -> None:
    bak = fp + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(fp, bak)

def process_file(fp: str, mode: str) -> Tuple[bool, int]:
    enc, lines = read_text_file(fp)
    if mode == "1":
        new_lines, changed = process_lines_modify(lines)
    else:
        new_lines, changed = process_lines_restore(lines)

    if changed > 0:
        if MAKE_BACKUP:
            backup_file(fp)
        write_text_file(fp, new_lines, enc)
        return True, changed
    return False, 0

def main():
    print("=== LRC 时间戳修复器：让翻译别和原文抢同一个座位 ===")
    print("1) 修改：连续相同时间戳 -> 第一行 -0.01s")
    print("2) 还原：检测到 -0.01s 形式 -> 恢复为相同时间戳")
    mode = input("请选择 1 或 2：").strip()
    if mode not in ("1", "2"):
        print("你输入的不是 1/2，我只能当场罢工。")
        return

    path = input("请输入 .lrc 文件路径 或 目录路径：").strip().strip('"').strip("'")
    files = collect_lrc_files(path)
    if not files:
        print("没找到任何 .lrc 文件。路径可能不对，或者你给了个 .txt（歌词：我不要面子的吗）")
        return

    total_changed_files = 0
    total_changed_lines = 0

    for fp in files:
        ok, changed = process_file(fp, mode)
        if ok:
            total_changed_files += 1
            total_changed_lines += changed
            print(f"[OK] {fp}  修改行数: {changed}")
        else:
            print(f"[SKIP] {fp}  无需修改")

    action = "修改" if mode == "1" else "还原"
    print(f"\n完成：{action}了 {total_changed_files}/{len(files)} 个文件，共处理 {total_changed_lines} 行。")
    if MAKE_BACKUP:
        print("已为发生改动的文件生成 .bak 备份（防止你未来穿越回来打我）。")

if __name__ == "__main__":
    main()
