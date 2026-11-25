# -*- coding: utf-8 -*-
"""
聊天记录截图排序工具
=====================================================================
把微信 / QQ 等聊天截图，按"图中对话时间"从早到晚排序，
并复制重命名到输出目录，顺序即时间顺序。

识别的时间格式（微信 / QQ 常见）：
    2025年10月8日 21:35      10月8日 21:35
    10月8日 下午9:35         昨天 21:35 / 前天 09:12
    星期三 21:35 / 周三 21:35  10/8 21:35
    上午 10:23 / 下午 3:45 / 晚上 9:12

依赖：Windows 10/11 自带 OCR（无需安装第三方库）。
若 OCR 不可用，自动降级为按文件时间排序，并在对照表中标注"待核对"。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'}
TOOL_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------
# 一、文本归一化与时间解析
# ---------------------------------------------------------------------
AP_RE = r'(上午|下午|凌晨|中午|傍晚|深夜|晚上)?'

RE_FULL = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日' + AP_RE + r'(\d{1,2}):(\d{2})')
RE_MD = re.compile(r'(\d{1,2})月(\d{1,2})日' + AP_RE + r'(\d{1,2}):(\d{2})')
RE_REL = re.compile(r'(昨天|前天|今天|星期[一二三四五六日天]|周[一二三四五六日天])' + AP_RE + r'(\d{1,2}):(\d{2})')
RE_SLASH = re.compile(r'(?<!\d)(\d{1,2})[/\-](\d{1,2})' + AP_RE + r'(\d{1,2}):(\d{2})')
RE_TIME = re.compile(r'^' + AP_RE + r'(\d{1,2}):(\d{2})[^\d]{0,3}$')

WEEKDAY = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}

# OCR 常见误识（仅在"疑似时间行"上启用）
OCR_FIX_MAP = {
    ']': '1', '[': '1', '|': '1', '!': '1', 'l': '1', 'I': '1', 'i': '1',
    'O': '0', 'o': '0', 'D': '0', 'Q': '0',
}


def norm_text(s):
    """全角转半角、去掉所有空白。"""
    s = unicodedata.normalize('NFKC', s or '')
    s = re.sub(r'\s+', '', s)
    return s


def fix_ocr_digits(s):
    """对疑似时间行修正 OCR 误识的数字字符。"""
    if not ('月' in s or '日' in s or ':' in s):
        return s
    out = []
    for ch in s:
        out.append(OCR_FIX_MAP.get(ch, ch))
    return ''.join(out)


def to24(h, ap):
    """12 小时制 -> 24 小时制。"""
    if ap in ('下午', '晚上', '傍晚', '深夜'):
        return h + 12 if h < 12 else h
    if ap == '中午':
        return h if h >= 11 else h + 12
    if ap in ('上午', '凌晨'):
        return 0 if h == 12 else h
    return h


def safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def resolve(parsed, base_dt):
    """把解析结果落到具体 datetime；base_dt 为基准时刻（截图文件时间）。"""
    base = base_dt.date()
    kind = parsed['kind']
    h, mi = parsed['h'], parsed['mi']

    if kind == 'full':
        dt = safe_date(parsed['y'], parsed['m'], parsed['d'])
        return datetime(dt.year, dt.month, dt.day, h, mi) if dt else None

    if kind == 'md':
        d_this = safe_date(base.year, parsed['m'], parsed['d'])
        if d_this and d_this > base + timedelta(days=3):
            d_this = safe_date(base.year - 1, parsed['m'], parsed['d'])
        if not d_this:
            return None
        return datetime(d_this.year, d_this.month, d_this.day, h, mi)

    if kind == 'rel':
        rel = parsed['rel']
        if rel == '今天':
            d_target = base
        elif rel == '昨天':
            d_target = base - timedelta(days=1)
        elif rel == '前天':
            d_target = base - timedelta(days=2)
        else:
            key = rel[-1]
            if key not in WEEKDAY:
                return None
            delta = (base.weekday() - WEEKDAY[key]) % 7
            if delta == 0:
                delta = 7          # 微信当天会显示"今天"，故同一天按上周算
            d_target = base - timedelta(days=delta)
        return datetime(d_target.year, d_target.month, d_target.day, h, mi)

    if kind == 'time':
        # 微信仅在"当天"用纯时间分隔条；截图不会早于对话，
        # 若算出的时刻晚于截图时刻，说明跨了一天。
        dt = datetime(base.year, base.month, base.day, h, mi)
        if dt > base_dt + timedelta(hours=1):
            dt -= timedelta(days=1)
        return dt

    return None


def parse_time_line(text):
    """解析单行文本，返回 parsed dict 或 None。"""
    s = norm_text(text)
    if not s:
        return None

    for attempt in (s, fix_ocr_digits(s)):
        m = RE_FULL.search(attempt)
        if m:
            y, mo, d, ap, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5)), int(m.group(6))
            if 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60:
                return {'kind': 'full', 'y': y, 'm': mo, 'd': d, 'h': to24(h, ap), 'mi': mi, 'raw': text.strip()}

        m = RE_MD.search(attempt)
        if m:
            mo, d, ap, h, mi = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
            if 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60:
                return {'kind': 'md', 'm': mo, 'd': d, 'h': to24(h, ap), 'mi': mi, 'raw': text.strip()}

        m = RE_REL.search(attempt)
        if m:
            rel, ap, h, mi = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            if h < 24 and mi < 60:
                return {'kind': 'rel', 'rel': rel, 'h': to24(h, ap), 'mi': mi, 'raw': text.strip()}

        m = RE_SLASH.search(attempt)
        if m:
            mo, d, ap, h, mi = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
            if 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60:
                return {'kind': 'md', 'm': mo, 'd': d, 'h': to24(h, ap), 'mi': mi, 'raw': text.strip()}

        m = RE_TIME.match(attempt)
        if m:
            ap, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
            if h < 24 and mi < 60:
                return {'kind': 'time', 'h': to24(h, ap), 'mi': mi, 'raw': text.strip()}

    return None


STATUS_BAR_RATIO = 0.055   # 顶部手机状态栏区域：纯时间行落在这里时忽略


def pick_chat_time(lines, base_dt):
    """
    从 OCR 行中挑出该截图"对话时间"。
    lines: [{'text','ratio',...}]，ratio 为纵向相对位置 0~1
    规则：含日期的分隔条优先（取最靠上的）；若没有，才用纯时间（排除顶部状态栏）。
    """
    dated, timed = [], []

    for ln in lines:
        txt = ln.get('text', '')
        ratio = ln.get('ratio', 0)
        p = parse_time_line(txt)
        if not p:
            # 跨行拼接：只有日期 / 只有时间的相邻行
            continue
        if p['kind'] == 'time':
            timed.append((ratio, p))
        else:
            dated.append((ratio, p))

    # 跨行拼接尝试（OCR 把 "10月8日" 和 "21:35" 拆成两行时）
    if not dated and not timed:
        for i in range(len(lines) - 1):
            a, b = lines[i], lines[i + 1]
            if abs(a.get('ratio', 0) - b.get('ratio', 0)) > 0.012:
                continue
            merged = norm_text(a.get('text', '')) + norm_text(b.get('text', ''))
            p = parse_time_line(merged)
            if p and p['kind'] != 'time':
                dated.append((min(a.get('ratio', 0), b.get('ratio', 0)), p))

    pool = dated if dated else [(r, p) for r, p in timed if r >= STATUS_BAR_RATIO]
    if not pool:
        return None, None

    pool.sort(key=lambda x: x[0])          # 取最靠上的（即最早）
    ratio, parsed = pool[0]
    dt = resolve(parsed, base_dt)
    if dt is None:
        return None, parsed.get('raw')
    return dt, parsed.get('raw')


# ---------------------------------------------------------------------
# 二、OCR 调用
# ---------------------------------------------------------------------
def run_ocr(paths, mode='normal'):
    """调用 ocr_worker.ps1，返回 {路径: item} 与错误信息。"""
    ps1 = TOOL_DIR / 'ocr_worker.ps1'
    if not ps1.exists():
        return {}, '缺少 ocr_worker.ps1'
    if os.name != 'nt':
        return {}, '仅支持 Windows'

    tmp_dir = Path(tempfile.mkdtemp(prefix='chatocr_'))
    list_file = tmp_dir / 'list.txt'
    out_file = tmp_dir / 'out.json'
    list_file.write_text('\n'.join(str(p) for p in paths), encoding='utf-8')

    cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
           '-File', str(ps1), '-ListFile', str(list_file),
           '-OutFile', str(out_file), '-Mode', mode]

    creationflags = 0x08000000 if os.name == 'nt' else 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                creationflags=creationflags)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print('  ' + line, flush=True)
        proc.wait()
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return {}, f'调用 OCR 失败：{e}'

    result = {}
    err = ''
    if out_file.exists():
        try:
            payload = json.loads(out_file.read_text(encoding='utf-8'))
            if not payload.get('ok', False):
                err = payload.get('error', 'OCR 引擎不可用')
            for it in payload.get('items', []) or []:
                result[it.get('path', '')] = it
        except Exception as e:
            err = f'解析 OCR 结果失败：{e}'
    else:
        err = 'OCR 未产生结果文件'

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return result, err


# ---------------------------------------------------------------------
# 三、主流程
# ---------------------------------------------------------------------
def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]


def file_base_date(p):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime)
    except Exception:
        return datetime.now()


def collect_images(folder):
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda p: natural_key(p.name))
    return files


def archive_old_output(out_dir):
    """案件目录非空时，把上次结果移到 历史/ 子目录，不删除任何东西。"""
    existing = [p for p in out_dir.iterdir()
                if (p.is_file() or p.is_dir()) and p.name != '历史']
    if not existing:
        return None
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    hist = out_dir / '历史' / stamp
    hist.mkdir(parents=True, exist_ok=True)
    for p in existing:
        shutil.move(str(p), str(hist / p.name))
    return hist


def sanitize_name(s):
    """清理成合法文件夹名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '', s or '').strip().strip('. ')
    return s[:60] or '未命名'


def scan_input(in_dir):
    """
    扫描输入目录，返回 [(当事人名, [图片路径])]。
    输入目录下有子文件夹时，按子文件夹名作为当事人分批；
    根目录的散图归为"未分类"。
    """
    subs = sorted([d for d in in_dir.iterdir()
                   if d.is_dir() and not d.name.startswith(('.', '历史', '_'))],
                  key=lambda p: natural_key(p.name))
    groups = []
    for d in subs:
        imgs = collect_images(d)
        if imgs:
            groups.append((d.name, imgs))
    root_imgs = collect_images(in_dir)
    if root_imgs:
        groups.append((None, root_imgs))
    return groups


def extract_times(images, ocr_map, no_ocr):
    """逐图提取对话时间，返回 {路径: record}。"""
    records = {}
    need_retry = []

    for p in images:
        item = ocr_map.get(str(p))
        base = file_base_date(p)
        dt, raw = (None, None)
        if not no_ocr and item and item.get('ok'):
            dt, raw = pick_chat_time(item.get('lines', []), base)
        rec = {'path': p, 'dt': dt, 'raw': raw, 'base': base,
               'src': '图中时间' if dt else '待定',
               'lines': (item or {}).get('lineCount', 0),
               'size': f"{(item or {}).get('width', 0)}x{(item or {}).get('height', 0)}"}
        records[p] = rec
        if dt is None:
            need_retry.append(p)

    # 未识别的，用增强对比度重试一次
    if need_retry and not no_ocr:
        print(f'      {len(need_retry)} 张未识别出时间，用增强对比度重试…')
        retry_map, _ = run_ocr(need_retry, 'enhanced')
        for p in need_retry:
            rec = records[p]
            item = retry_map.get(str(p))
            if item and item.get('ok'):
                dt, raw = pick_chat_time(item.get('lines', []), rec['base'])
                if dt:
                    rec.update(dt=dt, raw=raw, src='图中时间(增强)', lines=item.get('lineCount', 0))

    return records


def sort_group(records):
    """组内排序：有图内时间的按时间，无时间的按文件时间排到末尾。"""
    resolved = [r for r in records if r['dt'] is not None]
    unresolved = [r for r in records if r['dt'] is None]
    resolved.sort(key=lambda r: (r['dt'], natural_key(r['path'].name)))
    for r in unresolved:
        r['dt'] = r['base']
        r['src'] = '文件时间(待核对)'
    unresolved.sort(key=lambda r: (r['dt'], natural_key(r['path'].name)))
    return resolved + unresolved, len(resolved), len(unresolved)


def write_group(ordered, case_dir, move=False, keep_old=False):
    """把一组排序结果落到 案件目录，返回 (文件数, 历史目录)。"""
    case_dir.mkdir(parents=True, exist_ok=True)
    hist = None
    if not keep_old:
        hist = archive_old_output(case_dir)

    review_dir = case_dir / '待核对'
    count = 0
    for i, r in enumerate(ordered, 1):
        is_review = r['src'].startswith('文件时间')
        target_dir = review_dir if is_review else case_dir
        if is_review:
            target_dir.mkdir(parents=True, exist_ok=True)
        new_name = f"{i:03d}_{r['dt']:%Y-%m-%d_%H%M}{r['path'].suffix.lower()}"
        dest = target_dir / new_name
        n = 1
        while dest.exists():
            dest = target_dir / f"{i:03d}_{r['dt']:%Y-%m-%d_%H%M}_{n}{r['path'].suffix.lower()}"
            n += 1
        if move:
            shutil.move(str(r['path']), str(dest))
        else:
            shutil.copy2(str(r['path']), str(dest))
        count += 1
    return count, hist


def main():
    ap = argparse.ArgumentParser(description='聊天记录截图按对话时间排序')
    ap.add_argument('--input', default=None, help='截图所在目录（默认：本目录/输入）')
    ap.add_argument('--output', default=None, help='结果根目录（默认：本目录/输出）')
    ap.add_argument('--case', default=None, help='当事人名称，全部截图归入一个《日期-当事人》文件夹')
    ap.add_argument('--date', default=None, help='文件夹名用的日期，格式 YYYY-MM-DD（默认今天）')
    ap.add_argument('--dry-run', action='store_true', help='只预览排序，不复制文件')
    ap.add_argument('--move', action='store_true', help='移动原件而非复制')
    ap.add_argument('--no-ocr', action='store_true', help='跳过 OCR，仅按文件时间排序')
    ap.add_argument('--keep-old', action='store_true', help='保留案件目录上次结果（默认移入 历史/）')
    args = ap.parse_args()

    in_dir = Path(args.input) if args.input else TOOL_DIR / '输入'
    out_root = Path(args.output) if args.output else TOOL_DIR / '输出'
    run_date = args.date or datetime.now().strftime('%Y-%m-%d')

    if not in_dir.exists():
        print(f'[错误] 输入目录不存在：{in_dir}')
        return 1

    # --- 分组 ---
    groups = scan_input(in_dir)
    if not groups:
        print(f'[提示] 输入目录中没有图片：{in_dir}')
        print(f'        支持的格式：{" ".join(sorted(IMAGE_EXTS))}')
        return 1

    # 指定了当事人名，就把所有图合并为一个案件处理
    if args.case:
        all_imgs = [p for _, imgs in groups for p in imgs]
        groups = [(args.case, all_imgs)]

    all_images = [p for _, imgs in groups for p in imgs]
    print(f'[1/4] 找到 {len(all_images)} 张图片，分属 {len(groups)} 个案件：')
    for case, imgs in groups:
        label = sanitize_name(case) if case else '未分类'
        print(f'      {label}：{len(imgs)} 张')

    # --- OCR ---
    ocr_map = {}
    if not args.no_ocr:
        print('[2/4] 正在识别图片中的时间（Windows 内置 OCR，首次调用需几秒启动）')
        ocr_map, ocr_err = run_ocr(all_images, 'normal')
        if ocr_err:
            print(f'      [警告] {ocr_err}，将按文件时间排序')
        else:
            print(f'      识别完成：{len(ocr_map)} 张')
    else:
        print('[2/4] 已跳过 OCR')

    # --- 解析 ---
    print('[3/4] 解析对话时间')
    records = extract_times(all_images, ocr_map, args.no_ocr)
    print(f'      识别成功 {sum(1 for r in records.values() if r["dt"] is not None)} 张')

    # --- 逐案排序输出 ---
    print()
    print('[4/4] 输出文件')
    total_done = 0
    for case, imgs in groups:
        case_label = sanitize_name(case) if case else '未分类'
        ordered, n_ok, n_bad = sort_group([records[p] for p in imgs])

        print()
        print(f'◆ {case_label}（{len(ordered)} 张，图中时间 {n_ok}，待核对 {n_bad}）')
        print('   序号  识别时间            图中时间原文        来源              原文件')
        for i, r in enumerate(ordered, 1):
            mark = '!' if r['src'].startswith('文件时间') else ' '
            raw = (r['raw'] or '—')[:16]
            print(f"   {i:>3}{mark} {r['dt']:%Y-%m-%d %H:%M}  {raw:<18}  {r['src']:<16}  {r['path'].name}")

        if args.dry_run:
            print(f'   → 将输出到：{out_root / f"{run_date}-{case_label}"}')
            continue

        case_dir = out_root / f'{run_date}-{case_label}'
        n, hist = write_group(ordered, case_dir, args.move, args.keep_old)
        total_done += n
        print(f'   → {case_dir}')
        if hist:
            print(f'     上次结果已移入：{hist}')
        if n_bad:
            print(f'     {n_bad} 张未识别时间，已放入「待核对」子文件夹')

    print()
    if args.dry_run:
        print('[预览模式] 未复制任何文件。去掉 --dry-run 即执行。')
    else:
        print(f'完成。共输出 {total_done} 个文件，位于：{out_root}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
