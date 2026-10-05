# -*- coding: utf-8 -*-
"""从游戏的 zh-CN/strings.conf 生成卡密工具内置的系列码名称表。"""

import json
import os
import sys


def parse_setnames(path):
    names = {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.startswith("!setname"):
                continue
            parts = line.strip().split(maxsplit=2)
            if len(parts) != 3 or not parts[1].lower().startswith("0x"):
                raise ValueError("第 {} 行的 !setname 格式无效".format(line_no))
            try:
                code = int(parts[1], 16)
            except ValueError as exc:
                raise ValueError("第 {} 行的系列码无效".format(line_no)) from exc
            name = parts[2].split("\t", 1)[0].strip()
            if not 1 <= code <= 65535 or not name:
                raise ValueError("第 {} 行的系列码超出 16 位范围或缺少名称".format(line_no))
            if code in names:
                raise ValueError("第 {} 行的系列码 0x{:X} 重复".format(line_no, code))
            names[code] = name
    if not names:
        raise ValueError("没有找到 !setname 条目")
    return dict(sorted(names.items()))


def main():
    if len(sys.argv) != 2:
        raise SystemExit("用法：python gen_setnames.py <zh-CN/strings.conf 路径>")
    source = sys.argv[1]
    names = parse_setnames(source)
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setnames.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(names, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("已生成 {} 条系列码：{}".format(len(names), target))


if __name__ == "__main__":
    main()
