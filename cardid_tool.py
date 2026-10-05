# -*- coding: utf-8 -*-
"""
卡密工具 (Card Password Tool)
===========================

移植自 ygo-cdb-editor/www/app.js 里的「卡密」子系统：

  - 随机生成卡密 (drawCardId / assignNewCardId)
        在 9 亿段 [900000000, 900099998] 内随机取一个卡片密码，
        并避开已存在的卡密（查重）。

  - 检测卡密 (collectKnownIds / scanLibFolder)
        判断一个卡片密码是否合法、属于官方 8 位密码段还是自定义 9 亿段，
        以及是否已经存在于已载入的卡库 (.cdb) 中（重复）。

  - 中文检索系列码
        从游戏 zh-CN/strings.conf 的 !setname 名称表检索 CDB 系列码，
        同时支持中文名、十进制和 0x 十六进制代码。
        可随机生成未用码、检测重复并写入游戏配置（写入前备份）。

用法：
    python cardid_tool.py            # 启动图形界面
    python cardid_tool.py --selftest # 仅自检核心逻辑（无界面）

依赖：仅 Python 标准库 (tkinter / sqlite3 / random)。可选的 setnames.json 可由游戏配置生成。
"""

import os
import sys
import json
import random
import sqlite3
import shutil
import tempfile

from gen_setnames import parse_setnames

# ---- 卡密范围（与项目 app.js 保持一致）--------------------------------
# app.js: return 900000000 + Math.floor(Math.random() * 99999);
#   Math.random() in [0,1) -> *99999 in [0,99999) -> floor in [0,99998]
#   => [900000000, 900099998]
CUSTOM_BASE = 900_000_000
CUSTOM_SPAN = 99_999            # 区间长度（不含上限，randrange 用）
CUSTOM_MIN = CUSTOM_BASE
CUSTOM_MAX = CUSTOM_BASE + CUSTOM_SPAN - 1   # 900_099_998

# 官方 8 位密码段上限（cards.cdb 实测最大 id = 99_995_595，均 <= 8 位）
OFFICIAL_MAX = 99_999_999
DEFAULT_GAME_STRINGS = r"D:\Game\MDPro3\Data\locales\zh-CN\strings.conf"


# ----------------------------------------------------------------------
# 纯逻辑（无界面，便于自检与复用）
# ----------------------------------------------------------------------
def draw_card_id(lo=CUSTOM_MIN, hi=CUSTOM_MAX):
    """在 [lo, hi] 内等概率随机抽一个卡密。

    默认区间即项目 app.js 的 9 亿段 drawCardId()：
        return 900000000 + Math.floor(Math.random() * 99999);
    """
    lo = int(lo)
    hi = int(hi)
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def classify(card_id, lo=CUSTOM_MIN, hi=CUSTOM_MAX):
    """返回 (kind, label)。kind: 'custom' | 'official' | 'invalid'。

    [lo, hi] 为当前自定义段（可手动调整），用于判定归属。
    """
    if not isinstance(card_id, int) or card_id <= 0:
        return ("invalid", "非法（非正整数）")
    if lo <= card_id <= hi:
        return ("custom", "自定义段 [{}, {}]".format(lo, hi))
    if 1 <= card_id <= OFFICIAL_MAX:
        return ("official", "官方 8 位密码段")
    return ("invalid", "超出范围")


def generate(known, count, dedup, lo=CUSTOM_MIN, hi=CUSTOM_MAX):
    """
    在 [lo, hi] 内生成 count 个卡密。
    known : 已存在的卡密集合（用于查重）
    dedup : 是否避开 known 与本次已生成的卡密
    返回 (list, skipped) —— skipped 为因池子/查重不足而未生成的数量。
    """
    lo = int(lo)
    hi = int(hi)
    if hi < lo:
        lo, hi = hi, lo
    if not dedup:
        return [draw_card_id(lo, hi) for _ in range(count)], 0

    # 只关心落在当前范围内的已知卡密
    blocked = {k for k in known if lo <= k <= hi}
    avail = (hi - lo + 1) - len(blocked)
    if avail <= 0:
        return [], count
    want = min(count, avail)

    # 池子规模可控：收集空闲号再洗牌，保证不重复且不会死循环
    if (hi - lo + 1) <= 2_000_000:
        free = [i for i in range(lo, hi + 1) if i not in blocked]
        random.shuffle(free)
        picked = free[:want]
        return picked, count - len(picked)

    # 超大池：逐个随机抽取（冲突率极低，几乎不会重试）
    picked = []
    seen = set()
    guard = 0
    limit = want * 200 + 1000
    while len(picked) < want and guard < limit:
        gid = random.randint(lo, hi)
        guard += 1
        if gid in blocked or gid in seen:
            continue
        seen.add(gid)
        picked.append(gid)
    return picked, count - len(picked)


def load_cdb_ids(path):
    """读取单个 .cdb 文件 datas 表的 id 集合（只读）。"""
    s = set()
    uri = "file:{}?mode=ro".format(path.replace("'", "''"))
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM datas")
        for (i,) in cur.fetchall():
            s.add(int(i))
    finally:
        con.close()
    return s


def load_folder_ids(folder, recursive=True):
    """扫描文件夹内所有 .cdb，合并 id 集合。"""
    s = set()
    files = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            for n in names:
                if n.lower().endswith(".cdb"):
                    files.append(os.path.join(root, n))
    else:
        for n in os.listdir(folder):
            if n.lower().endswith(".cdb"):
                files.append(os.path.join(folder, n))
    for f in files:
        try:
            s |= load_cdb_ids(f)
        except Exception:
            # 单个文件读不了就跳过（与 app.js 行为一致）
            pass
    return s, files


def load_cdb_setcodes(path):
    """读取卡库 datas.setcode 中实际使用的四个 16 位系列码。"""
    found = set()
    uri = "file:{}?mode=ro".format(path.replace("'", "''"))
    con = sqlite3.connect(uri, uri=True)
    try:
        for (value,) in con.execute("SELECT setcode FROM datas"):
            packed = int(value or 0) & ((1 << 64) - 1)
            for shift in (0, 16, 32, 48):
                code = (packed >> shift) & 0xFFFF
                if code:
                    found.add(code)
    finally:
        con.close()
    return found


def load_bundled_setnames():
    """读取本地系列码名称表；未生成时从空表启动。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "setnames.json")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return {int(code): name for code, name in json.load(fh).items()}


def search_setnames(names, query):
    """按中文名、十进制或 0x 十六进制代码筛选系列码。"""
    query = query.strip().casefold()
    return [(code, name) for code, name in sorted(names.items())
            if query in name.casefold() or query in str(code)
            or query in "0x{:X}".format(code).casefold()]


def parse_setname_code(raw):
    """接受十进制或 0x 十六进制，限制在 CDB 的 16 位非零范围。"""
    value = str(raw).strip()
    if value.lower().startswith("0x"):
        digits = value[2:]
        if not digits or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise ValueError("系列码须为十进制或 0x 十六进制整数")
        code = int(digits, 16)
    elif value.isascii() and value.isdecimal():
        code = int(value, 10)
    else:
        raise ValueError("系列码须为十进制或 0x 十六进制整数")
    if not 1 <= code <= 0xFFFF:
        raise ValueError("系列码须在 1–65535（0x1–0xFFFF）之间")
    return code


def draw_setname_code(used):
    """在 1–65535 内等概率选出一个未登记、未被已载入卡库使用的代码。"""
    available = [code for code in range(1, 0x10000) if code not in used]
    if not available:
        raise ValueError("16 位系列码已全部占用")
    return random.choice(available)


def save_setname_to_conf(path, code, name, blocked=()):
    """复核占用情况，保留原编码和换行格式，备份后写入一条 !setname。"""
    code = parse_setname_code(code)
    name = str(name).strip()
    if not name or any(c in name for c in "\r\n\t"):
        raise ValueError("中文名称不能为空，也不能包含换行或制表符")
    if code in blocked:
        raise ValueError("0x{:X} 已被载入的卡库使用".format(code))

    before = os.stat(path)
    with open(path, "rb") as fh:
        original = fh.read()
    names = parse_setnames(path)
    if code in names:
        raise ValueError("0x{:X} 已在 strings.conf 中登记为「{}」".format(code, names[code]))
    if os.stat(path).st_mtime_ns != before.st_mtime_ns or os.stat(path).st_size != before.st_size:
        raise RuntimeError("strings.conf 在读取时发生变化，请重新载入后重试")

    newline = "\r\n" if b"\r\n" in original else "\n"
    lines = original.decode("utf-8-sig").splitlines(keepends=True)
    insert_at = next((i + 1 for i in range(len(lines) - 1, -1, -1)
                      if lines[i].startswith("!setname ")), len(lines))
    if insert_at and lines[insert_at - 1] and not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += newline
    lines.insert(insert_at, "!setname 0x{:X} {}{}".format(code, name, newline))
    output = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")

    directory = os.path.dirname(os.path.abspath(path))
    handle, temp_path = tempfile.mkstemp(prefix=".strings-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(output)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, temp_path)
        current = os.stat(path)
        if current.st_mtime_ns != before.st_mtime_ns or current.st_size != before.st_size:
            raise RuntimeError("strings.conf 已被其他程序修改，请重新载入后重试")
        backup = path + ".bak"
        index = 1
        while os.path.exists(backup):
            backup = path + ".bak{}".format(index)
            index += 1
        shutil.copy2(path, backup)
        os.replace(temp_path, path)
        return backup
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _selftest():
    print("[selftest] draw_card_id 范围检查 ...")
    for _ in range(200000):
        v = draw_card_id()
        assert CUSTOM_MIN <= v <= CUSTOM_MAX, v
    print("  OK: 全部落在 [{}, {}]".format(CUSTOM_MIN, CUSTOM_MAX))

    print("[selftest] classify ...")
    assert classify(900000000)[0] == "custom"
    assert classify(900099998)[0] == "custom"
    assert classify(899999999)[0] == "invalid"
    assert classify(900099999)[0] == "invalid"
    assert classify(46986414)[0] == "official"
    assert classify(0)[0] == "invalid"
    assert classify(-5)[0] == "invalid"
    assert classify("abc")[0] == "invalid"
    print("  OK")

    print("[selftest] generate（带查重）...")
    known = {900000000, 900000001}
    out, skipped = generate(known, 50, dedup=True)
    assert len(out) == 50 and skipped == 0
    assert not (set(out) & known)
    assert len(set(out)) == 50
    print("  OK: 生成 {} 个，无重复".format(len(out)))

    print("[selftest] generate（自定义范围）...")
    k2 = {500, 501}
    o2, s2 = generate(k2, 10, dedup=True, lo=500, hi=520)
    assert len(o2) == 10 and s2 == 0, (len(o2), s2)
    assert all(500 <= x <= 520 for x in o2)
    assert not (set(o2) & k2)
    # 池子被占满时应返回 skipped 而不是死循环
    o3, s3 = generate(set(range(100, 200)), 50, dedup=True, lo=100, hi=199)
    assert o3 == [] and s3 == 50, (o3, s3)
    # 部分可用
    o4, s4 = generate(set(range(100, 195)), 50, dedup=True, lo=100, hi=199)
    assert len(o4) == 5 and s4 == 45, (len(o4), s4)
    # 不查重时允许重复，但仍在范围内
    o5, _ = generate(set(), 200, dedup=False, lo=900000000, hi=900000005)
    assert all(900000000 <= x <= 900000005 for x in o5)
    print("  OK: 自定义范围 / 池子不足 / 不查重 均正确")

    print("[selftest] classify（自定义范围）...")
    assert classify(300, lo=200, hi=400)[0] == "custom"
    assert classify(199, lo=200, hi=400)[0] == "official"
    assert classify(401, lo=200, hi=400)[0] == "official"
    print("  OK")

    print("[selftest] 读库 ...")
    db = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "游戏王卡片数据", "cards.cdb"
    )
    if os.path.exists(db):
        ids = load_cdb_ids(db)
        print("  OK: cards.cdb 载入 {} 个 id，范围 [{}-{}]".format(
            len(ids), min(ids), max(ids)))
        # 卡库里不应出现自定义 9 亿段（应保持分离）
        overlap = ids & set(range(CUSTOM_MIN, CUSTOM_MAX + 1))
        assert not overlap, "官方库与 9 亿段不应重叠: {}".format(overlap)
        print("  OK: 官方库与 9 亿段无重叠")
    else:
        print("  (跳过) 未找到 cards.cdb：{}".format(db))

    print("\n全部自检通过。")


# ----------------------------------------------------------------------
# 图形界面
# ----------------------------------------------------------------------
def main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    DEFAULT_LIB = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "游戏王卡片数据"
    )

    root = tk.Tk()
    root.title("卡密工具 — 随机生成 / 检测 / 系列码检索")
    root.geometry("780x670")
    try:
        from tkinter import font
        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(size=10)
    except Exception:
        pass

    known_ids = set()          # 已载入卡库的卡密集合（去重用）
    known_setcodes = set()     # 已载入卡库实际使用的系列码
    loaded_files = []          # 实际扫描到的 .cdb 路径（用于显示）
    lib_files = []             # 手动选取的单个 .cdb 文件
    lib_folders = []           # 手动选取的文件夹
    setnames = load_bundled_setnames()
    setname_source = None      # 手动选取的游戏 strings.conf

    # 生成范围（可手动调整，默认 9 亿段）
    range_lo_var = tk.StringVar(value=str(CUSTOM_MIN))
    range_hi_var = tk.StringVar(value=str(CUSTOM_MAX))

    def get_range():
        """读取界面上的生成范围，返回 (lo, hi)；非法返回 None。"""
        try:
            lo = int(str(range_lo_var.get()).strip())
            hi = int(str(range_hi_var.get()).strip())
        except Exception:
            return None
        if lo < 1 or hi < lo:
            return None
        return (lo, hi)

    def reset_range():
        range_lo_var.set(str(CUSTOM_MIN))
        range_hi_var.set(str(CUSTOM_MAX))
        set_status("已恢复默认生成范围 [{}, {}]。".format(CUSTOM_MIN, CUSTOM_MAX))

    def config_path():
        """记忆文件位置：打包后随 exe，未打包时随脚本。"""
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "cardid_config.json")

    def reload_library():
        """根据 lib_files / lib_folders 重新扫描并刷新界面。"""
        known_ids.clear()
        known_setcodes.clear()
        loaded_files.clear()
        lib_list.delete(0, "end")
        for f in lib_files:
            try:
                ids = load_cdb_ids(f)
                known_ids.update(ids)
                try:
                    known_setcodes.update(load_cdb_setcodes(f))
                except (OSError, sqlite3.Error, ValueError):
                    pass
                loaded_files.append(f)
                lib_list.insert("end", "{}  ({} 个卡密)".format(f, len(ids)))
            except Exception as e:
                lib_list.insert("end", "{}  [读取失败: {}]".format(f, e))
        for d in lib_folders:
            ids, files = load_folder_ids(d, recursive=True)
            known_ids.update(ids)
            loaded_files.extend(files)
            for f in files:
                lib_list.insert("end", f)
                try:
                    known_setcodes.update(load_cdb_setcodes(f))
                except (OSError, sqlite3.Error, ValueError):
                    pass
        refresh_lib_status()

    def save_memory():
        """把当前选取的卡库（文件 + 文件夹）写入记忆文件。"""
        try:
            with open(config_path(), "w", encoding="utf-8") as fh:
                json.dump({
                    "files": lib_files,
                    "folders": lib_folders,
                    "range": [range_lo_var.get(), range_hi_var.get()],
                    "setname_source": setname_source,
                }, fh, ensure_ascii=False, indent=2)
            set_status("已记住卡库选择（{} 个文件 / {} 个文件夹）、生成范围和系列码名称来源。下次打开自动载入。".format(
                len(lib_files), len(lib_folders)))
        except Exception as e:
            messagebox.showerror("保存失败", "无法写入记忆文件：\n{}".format(e))

    def load_memory():
        """启动时读取记忆文件并自动载入（路径失效则跳过）。"""
        p = config_path()
        if not os.path.exists(p):
            if os.path.isfile(DEFAULT_GAME_STRINGS):
                try:
                    apply_setnames(parse_setnames(DEFAULT_GAME_STRINGS), DEFAULT_GAME_STRINGS)
                except (OSError, UnicodeError, ValueError):
                    pass
            return False
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            files = [f for f in data.get("files", []) if os.path.isfile(f)]
            folders = [d for d in data.get("folders", []) if os.path.isdir(d)]
            rng = data.get("range")
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                try:
                    if int(rng[0]) >= 1 and int(rng[1]) >= int(rng[0]):
                        range_lo_var.set(str(int(rng[0])))
                        range_hi_var.set(str(int(rng[1])))
                except Exception:
                    pass
            source = data.get("setname_source", DEFAULT_GAME_STRINGS)
            if isinstance(source, str) and os.path.isfile(source):
                try:
                    apply_setnames(parse_setnames(source), source)
                except (OSError, UnicodeError, ValueError):
                    pass
            if not (files or folders):
                return False
            lib_files.extend(files)
            lib_folders.extend(folders)
            reload_library()
            return True
        except Exception:
            return False

    # ---- 状态栏 ----
    status_var = tk.StringVar(value="未载入卡库。可点击「卡库」页载入 .cdb。")
    status_bar = ttk.Label(root, textvariable=status_var, relief="sunken",
                           anchor="w", padding=(6, 3))
    status_bar.pack(side="bottom", fill="x")

    nb = ttk.Notebook(root)

    def set_status(msg):
        status_var.set(msg)

    def refresh_lib_status():
        if lib_files or lib_folders:
            set_status("已载入卡库：{} 个文件 + {} 个文件夹，共 {} 个已知卡密。".format(
                len(lib_files), len(lib_folders), len(known_ids)))
        else:
            set_status("未载入卡库（检测「重复」时会退化为仅格式判断）。可点击「保存记忆」记住选择。")

    # ===== 卡库页 =====
    lib_frame = ttk.Frame(nb, padding=12)
    ttk.Label(lib_frame,
              text="载入卡库 (.cdb) 作为「检测 / 生成」时的已知卡密来源。\n"
                   "手动选取后点「保存记忆」，下次打开会自动载入，无需重新选择。",
              anchor="w").pack(anchor="w")

    lib_list = tk.Listbox(lib_frame, height=8)
    lib_list.pack(fill="both", expand=True, pady=8)

    lib_btn_row = ttk.Frame(lib_frame)
    lib_btn_row.pack(fill="x")

    def add_files():
        paths = filedialog.askopenfilenames(
            title="选择 .cdb 文件",
            filetypes=[("YGOPro 卡库", "*.cdb"), ("所有文件", "*.*")])
        for p in paths:
            if p not in lib_files:
                lib_files.append(p)
        reload_library()

    def add_folder():
        folder = filedialog.askdirectory(title="选择卡库文件夹")
        if folder and folder not in lib_folders:
            lib_folders.append(folder)
        reload_library()

    def add_default():
        if not os.path.isdir(DEFAULT_LIB):
            messagebox.showinfo("提示", "未找到默认卡库文件夹：\n{}".format(DEFAULT_LIB))
            return
        if DEFAULT_LIB not in lib_folders:
            lib_folders.append(DEFAULT_LIB)
        reload_library()

    def clear_lib():
        lib_files.clear()
        lib_folders.clear()
        reload_library()

    def refresh_lib():
        if not (lib_files or lib_folders):
            messagebox.showinfo("提示", "还没有选取任何卡库，先「选择 .cdb 文件」或「选择文件夹」。")
            return
        reload_library()
        set_status("已刷新卡库：{} 个文件 + {} 个文件夹，共 {} 个已知卡密。".format(
            len(lib_files), len(lib_folders), len(known_ids)))

    ttk.Button(lib_btn_row, text="选择 .cdb 文件", command=add_files).pack(side="left", padx=4)
    ttk.Button(lib_btn_row, text="选择文件夹", command=add_folder).pack(side="left", padx=4)
    ttk.Button(lib_btn_row, text="载入默认卡库", command=add_default).pack(side="left", padx=4)
    ttk.Button(lib_btn_row, text="刷新", command=refresh_lib).pack(side="left", padx=4)
    ttk.Button(lib_btn_row, text="保存记忆", command=save_memory).pack(side="left", padx=4)
    ttk.Button(lib_btn_row, text="清空", command=clear_lib).pack(side="left", padx=4)

    # ===== 生成页 =====
    gen_frame = ttk.Frame(nb, padding=12)
    # ---- 生成范围（可手动调整）----
    row0 = ttk.LabelFrame(gen_frame, text="生成范围", padding=8)
    row0.pack(fill="x", pady=(0, 8))
    ttk.Label(row0, text="起始：").pack(side="left")
    ttk.Entry(row0, textvariable=range_lo_var, width=14).pack(side="left", padx=(0, 8))
    ttk.Label(row0, text="结束：").pack(side="left")
    ttk.Entry(row0, textvariable=range_hi_var, width=14).pack(side="left", padx=(0, 8))
    ttk.Button(row0, text="恢复默认", command=reset_range).pack(side="left", padx=4)
    ttk.Label(row0, text="（默认 9 亿段，闭区间，含两端）",
              foreground="#666666").pack(side="left", padx=8)

    row1 = ttk.Frame(gen_frame)
    row1.pack(fill="x", pady=(0, 8))
    ttk.Label(row1, text="生成数量：").pack(side="left")
    count_var = tk.IntVar(value=10)
    ttk.Spinbox(row1, from_=1, to=100000, textvariable=count_var,
                width=10).pack(side="left", padx=4)
    dedup_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(row1, text="避开已存在卡密（查重）", variable=dedup_var).pack(side="left", padx=8)

    gen_btn_row = ttk.Frame(gen_frame)
    gen_btn_row.pack(fill="x", pady=(0, 8))
    gen_out = scrolledtext.ScrolledText(gen_frame, height=18, wrap="none")
    gen_out.pack(fill="both", expand=True)

    def do_generate():
        try:
            count = int(count_var.get())
        except Exception:
            messagebox.showerror("输入错误", "生成数量必须是整数。")
            return
        if count < 1:
            messagebox.showerror("输入错误", "生成数量必须 >= 1。")
            return
        rng = get_range()
        if rng is None:
            messagebox.showerror("输入错误",
                                 "生成范围必须是整数，且满足 1 <= 起始 <= 结束。")
            return
        lo, hi = rng
        out, skipped = generate(known_ids, count, dedup_var.get(), lo, hi)
        gen_out.delete("1.0", "end")
        gen_out.insert("end", "\n".join(str(x) for x in out))
        msg = "已生成 {} 个卡密（范围 [{}, {}]）。".format(len(out), lo, hi)
        if skipped:
            msg += " 有 {} 个因查重池不足未生成。".format(skipped)
        if dedup_var.get() and known_ids:
            msg += " 已避开 {} 个已知卡密。".format(len(known_ids))
        set_status(msg)

    def export_gen():
        text = gen_out.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("提示", "先生成卡密再导出。")
            return
        path = filedialog.asksaveasfilename(
            title="导出卡密", defaultextension=".txt",
            filetypes=[("文本", "*.txt"), ("CSV", "*.csv")])
        if not path:
            return
        lines = text.splitlines()
        lo, hi = get_range() or (CUSTOM_MIN, CUSTOM_MAX)
        if path.lower().endswith(".csv"):
            content = "card_id,type\n" + "\n".join(
                "{},{}".format(l, classify(int(l), lo, hi)[1])
                for l in lines if l.strip()) + "\n"
        else:
            content = text + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        messagebox.showinfo("已导出", "已保存到：\n{}".format(path))

    def copy_gen():
        text = gen_out.get("1.0", "end").strip()
        if not text:
            return
        root.clipboard_clear()
        root.clipboard_append(text)
        set_status("已复制 {} 个卡密到剪贴板。".format(len(text.splitlines())))

    ttk.Button(gen_btn_row, text="生成", command=do_generate).pack(side="left", padx=4)
    ttk.Button(gen_btn_row, text="导出", command=export_gen).pack(side="left", padx=4)
    ttk.Button(gen_btn_row, text="复制", command=copy_gen).pack(side="left", padx=4)

    # ===== 检测页 =====
    det_frame = ttk.Frame(nb, padding=12)
    ttk.Label(det_frame, text="每行一个卡密（或用逗号 / 空格分隔），点击「检测」。",
              anchor="w").pack(anchor="w")
    det_in = scrolledtext.ScrolledText(det_frame, height=8, wrap="word")
    det_in.pack(fill="both", expand=False, pady=6)

    det_btn_row = ttk.Frame(det_frame)
    det_btn_row.pack(fill="x", pady=(0, 6))
    det_tree = ttk.Treeview(det_frame, columns=("id", "type", "status", "note"),
                            show="headings", height=12)
    det_tree.heading("id", text="卡密")
    det_tree.heading("type", text="类型")
    det_tree.heading("status", text="状态")
    det_tree.heading("note", text="备注")
    det_tree.column("id", width=120)
    det_tree.column("type", width=140)
    det_tree.column("status", width=100)
    det_tree.column("note", width=200)
    det_tree.pack(fill="both", expand=True)

    def parse_inputs(text):
        ids = []
        for raw in text.replace(",", " ").split():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ids.append(int(raw))
            except ValueError:
                ids.append(raw)   # 保留原样，标记为非法
        return ids

    def do_detect():
        for item in det_tree.get_children():
            det_tree.delete(item)
        text = det_in.get("1.0", "end")
        ids = parse_inputs(text)
        if not ids:
            messagebox.showinfo("提示", "请输入待检测的卡密。")
            return
        rng = get_range()
        if rng is None:
            messagebox.showerror("输入错误",
                                 "生成范围必须是整数，且满足 1 <= 起始 <= 结束。\n"
                                 "请到「随机生成」页修正。")
            return
        lo, hi = rng
        dup_count = 0
        for x in ids:
            if not isinstance(x, int):
                det_tree.insert("", "end", values=(x, "—", "非法输入", "无法解析为整数"))
                continue
            kind, label = classify(x, lo, hi)
            if kind == "invalid":
                det_tree.insert("", "end", values=(x, label, "非法", "不在有效范围"))
                continue
            if x in known_ids:
                det_tree.insert("", "end", values=(x, label, "重复", "已存在于卡库"))
                dup_count += 1
            else:
                det_tree.insert("", "end", values=(x, label, "可用", "未冲突"))
        set_status("检测 {} 个：{} 个可用，{} 个重复/非法。".format(
            len(ids), len(ids) - dup_count, dup_count))

    def export_det():
        rows = det_tree.get_children()
        if not rows:
            messagebox.showinfo("提示", "先检测再导出。")
            return
        path = filedialog.asksaveasfilename(
            title="导出检测结果", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("文本", "*.txt")])
        if not path:
            return
        lines = ["卡密,类型,状态,备注"]
        for r in rows:
            lines.append(",".join(str(v) for v in det_tree.item(r, "values")))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        messagebox.showinfo("已导出", "已保存到：\n{}".format(path))

    ttk.Button(det_btn_row, text="检测", command=do_detect).pack(side="left", padx=4)
    ttk.Button(det_btn_row, text="导出结果", command=export_det).pack(side="left", padx=4)

    # ===== 系列码检索页 =====
    series_frame = ttk.Frame(nb, padding=12)
    ttk.Label(series_frame,
              text="检索 CDB 的 16 位系列码：支持中文名、十进制和 0x 十六进制；单击结果可复制代码。",
              anchor="w").pack(anchor="w", pady=(0, 8))
    series_search_row = ttk.Frame(series_frame)
    series_search_row.pack(fill="x", pady=(0, 8))
    ttk.Label(series_search_row, text="系列名 / 代码：").pack(side="left")
    series_query_var = tk.StringVar()
    series_query = ttk.Entry(series_search_row, textvariable=series_query_var)
    series_query.pack(side="left", fill="x", expand=True, padx=(0, 8))
    series_count_var = tk.StringVar()
    ttk.Label(series_search_row, textvariable=series_count_var, width=12).pack(side="left")

    series_result_frame = ttk.Frame(series_frame)
    series_result_frame.pack(fill="both", expand=True)
    series_tree = ttk.Treeview(series_result_frame, columns=("code", "hex", "name"),
                               show="headings", selectmode="browse")
    for col, label, width in (("code", "十进制", 110),
                              ("hex", "十六进制", 110),
                              ("name", "中文系列名", 470)):
        series_tree.heading(col, text=label)
        series_tree.column(col, width=width, anchor="w")
    series_scroll = ttk.Scrollbar(series_result_frame, orient="vertical", command=series_tree.yview)
    series_tree.configure(yscrollcommand=series_scroll.set)
    series_tree.pack(side="left", fill="both", expand=True)
    series_scroll.pack(side="right", fill="y")

    series_source_var = tk.StringVar(value="名称来源：内置游戏中文配置")
    ttk.Label(series_frame, textvariable=series_source_var, wraplength=740).pack(anchor="w", pady=(8, 4))
    series_btn_row = ttk.Frame(series_frame)
    series_btn_row.pack(fill="x")

    def refresh_series_results(*_args):
        for item in series_tree.get_children():
            series_tree.delete(item)
        matches = search_setnames(setnames, series_query_var.get())
        for code, name in matches:
            series_tree.insert("", "end", values=(code, "0x{:X}".format(code), name))
        series_count_var.set("{} / {} 条".format(len(matches), len(setnames)))

    def apply_setnames(names, source=None):
        nonlocal setname_source
        setnames.clear()
        setnames.update(names)
        setname_source = source
        series_source_var.set("名称来源：{}".format(source or "内置游戏中文配置"))
        refresh_series_results()

    def choose_setnames():
        path = filedialog.askopenfilename(
            title="选择游戏 zh-CN/strings.conf",
            filetypes=[("游戏中文配置", "strings.conf"), ("所有文件", "*.*")])
        if not path:
            return None
        try:
            names = parse_setnames(path)
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("读取失败", "无法读取系列码名称：\n{}".format(exc))
            return None
        apply_setnames(names, path)
        save_memory()
        set_status("已载入 {} 条系列码名称，已记住文件路径。".format(len(names)))
        return path

    def restore_bundled_setnames():
        apply_setnames(load_bundled_setnames())
        save_memory()
        set_status("已恢复内置的 {} 条系列码名称。".format(len(setnames)))

    def copy_series_code(hexadecimal=False):
        selection = series_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一个系列码。")
            return
        code, hex_code, name = series_tree.item(selection[0], "values")
        value = hex_code if hexadecimal else code
        root.clipboard_clear()
        root.clipboard_append(value)
        set_status("已复制 {}（{}，{}）。".format(value, hex_code, name))

    ttk.Button(series_btn_row, text="复制十进制", command=copy_series_code).pack(side="left", padx=4)
    ttk.Button(series_btn_row, text="复制 0x 代码",
               command=lambda: copy_series_code(True)).pack(side="left", padx=4)
    ttk.Button(series_btn_row, text="读取 strings.conf",
               command=choose_setnames).pack(side="left", padx=(20, 4))
    ttk.Button(series_btn_row, text="恢复内置名称",
               command=restore_bundled_setnames).pack(side="left", padx=4)

    series_manage = ttk.LabelFrame(series_frame, text="随机生成 / 检测重复 / 写入游戏配置", padding=8)
    series_manage.pack(fill="x", pady=(10, 0))
    series_code_row = ttk.Frame(series_manage)
    series_code_row.pack(fill="x", pady=(0, 6))
    ttk.Label(series_code_row, text="系列码：").pack(side="left")
    series_code_var = tk.StringVar()
    ttk.Entry(series_code_row, textvariable=series_code_var, width=14).pack(side="left", padx=(0, 8))
    series_check_var = tk.StringVar(value="范围 1–65535；可输入十进制或 0x 代码")

    def current_game_setnames():
        if setname_source:
            fresh = parse_setnames(setname_source)
            if fresh != setnames:
                apply_setnames(fresh, setname_source)
            return fresh
        return setnames

    def generate_series_code():
        try:
            names = current_game_setnames()
            code = draw_setname_code(set(names) | known_setcodes)
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("生成失败", str(exc))
            return
        series_code_var.set("0x{:X}".format(code))
        series_check_var.set("可用：{} / 0x{:X}".format(code, code))
        set_status("已生成未登记、未被载入卡库使用的系列码 {} / 0x{:X}。".format(code, code))

    def check_series_code():
        try:
            code = parse_setname_code(series_code_var.get())
            names = current_game_setnames()
        except (OSError, UnicodeError, ValueError) as exc:
            series_check_var.set(str(exc))
            return False
        if code in names:
            series_check_var.set("重复：配置中是「{}」".format(names[code]))
            return False
        if code in known_setcodes:
            series_check_var.set("重复：已被载入卡库使用")
            return False
        series_check_var.set("可用：{} / 0x{:X}".format(code, code))
        return True

    ttk.Button(series_code_row, text="随机生成未用码",
               command=generate_series_code).pack(side="left", padx=4)
    ttk.Button(series_code_row, text="检测重复",
               command=check_series_code).pack(side="left", padx=4)
    ttk.Label(series_code_row, textvariable=series_check_var,
              wraplength=325).pack(side="left", padx=(12, 0))

    series_name_row = ttk.Frame(series_manage)
    series_name_row.pack(fill="x")
    ttk.Label(series_name_row, text="中文名称：").pack(side="left")
    series_name_var = tk.StringVar()
    ttk.Entry(series_name_row, textvariable=series_name_var).pack(
        side="left", fill="x", expand=True, padx=(0, 8))

    def write_series_code():
        if not check_series_code():
            messagebox.showerror("无法写入", series_check_var.get())
            return
        path = setname_source or choose_setnames()
        if not path:
            return
        try:
            code = parse_setname_code(series_code_var.get())
            backup = save_setname_to_conf(path, code, series_name_var.get(), known_setcodes)
            apply_setnames(parse_setnames(path), path)
            save_memory()
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            messagebox.showerror("写入失败", str(exc))
            return
        series_query_var.set(series_name_var.get().strip())
        series_check_var.set("已写入：{} / 0x{:X}".format(code, code))
        set_status("已将 0x{:X} 写入 {}；备份：{}".format(code, path, backup))
        messagebox.showinfo("写入完成", "已写入：!setname 0x{:X} {}\n备份文件：{}".format(
            code, series_name_var.get().strip(), backup))

    ttk.Button(series_name_row, text="写入 strings.conf",
               command=write_series_code).pack(side="left", padx=4)
    series_query_var.trace_add("write", refresh_series_results)
    refresh_series_results()

    # ---- Notebook tabs ----
    nb.add(gen_frame, text="随机生成")
    nb.add(det_frame, text="检测卡密")
    nb.add(lib_frame, text="卡库")
    nb.add(series_frame, text="系列码检索")
    nb.pack(fill="both", expand=True, padx=8, pady=8)

    if not load_memory():
        refresh_lib_status()
    root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
