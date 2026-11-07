#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_vendor_mk.py — 从 4.4.4 原厂 blob 目录重新生成 ardbeg-vendor-blobs.mk

为什么需要重新生成而不是手改
--------------------------
原始 ardbeg-vendor-blobs.mk 是 setup-makefiles.sh 在 4.4 时代生成的，里面：

  * 引用了 35 个**包里根本没有**的文件（设备树仓库只提交了 313 个 blob，
    而 makefile 列了 345 条）。7.1 的 PRODUCT_COPY_FILES 是硬依赖，
    少一个就 "No rule to make target"，编译直接中断。
  * 有 3 个文件会覆盖 AOSP 7.1 自己的 libEGL.so / libGLESv2.so /
    libGLESv1_CM.so，编译期不一定报错，装上去就是黑屏。
  * 还有一批 4.4 专用 HAL / 依赖缺失的库，见 blobs-blacklist.txt。

本脚本做三件事：
  1. 逐条校验源文件是否存在，不存在就丢弃（并在生成文件里记一行"已丢弃"）；
  2. 应用 blobs-blacklist.txt；
  3. 按目标目录分组输出，每组带注释，方便以后人工核对。

生成结果可重复：同样的 blob 目录 + 同一份黑名单，输出逐字节相同。

用法：
    python3 gen_vendor_mk.py [--root /home/dy/aosp14] [--write]
不带 --write 时只打印统计，不落盘。
"""

import argparse
import os
import re
import sys
from collections import OrderedDict

MK_HEADER = """\
# =============================================================================
# ardbeg-vendor-blobs.mk — 原厂 4.4.4 闭源文件清单（适配 LineageOS 14.1）
#
# 【本文件由 tools/gen_vendor_mk.py 生成，不要手改】
#   原始版本保留在 ardbeg-vendor-blobs.mk.orig-4.4.4，供对照。
#
# 生成规则：
#   1) 只保留**源文件真实存在**的条目（原始清单有 35 条指向不存在的文件，
#      7.1 的 PRODUCT_COPY_FILES 是硬依赖，缺一个就编译中断）；
#   2) 排除 blobs-blacklist.txt 里列出的条目（4.4 专用 HAL、会覆盖 AOSP
#      同名库的文件、依赖 7.1 已删除库的文件）；
#   3) 其余条目原样保留，按安装目录分组，组内按路径排序。
#
# 统计：共 %(kept)d 条；丢弃 %(missing)d 条（源文件缺失）、
#       %(blacklisted)d 条（黑名单）。
# =============================================================================

# -----------------------------------------------------------------------------
# 【为什么 blob 装在 system/vendor 而不是 /vendor】
# 本机没有独立 vendor 分区（原厂分区表只有 APP/CAC/UDA，见 BoardConfig），
# 所以 7.1 会把 TARGET_COPY_OUT_VENDOR 解析成 system/vendor，
# 由 /system 分区承载，链接 /vendor -> /system/vendor 由 init 建立。
# -----------------------------------------------------------------------------
"""


def parse_mk(path):
    """解析原始 mk，返回 [(src, dest)]，保持原始顺序。"""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.split("#", 1)[0].strip().rstrip("\\").strip()
            if not line or line.startswith("PRODUCT_COPY_FILES"):
                continue
            if ":" not in line:
                continue
            src, dest = line.split(":", 1)
            entries.append((src.strip(), dest.strip()))
    return entries


def load_recovered(path):
    """
    读取 blobs-recovered.txt —— 从原厂 4.4.4 system.img 补回来的条目。

    【为什么需要单独一份清单】
      ardbeg-vendor-blobs.mk.orig-4.4.4 是原厂 setup-makefiles.sh 生成的清单的
      **存档**，它的价值就在于「原厂当年列了什么」。往里塞新条目就毁了存档。
      而手工提取 blob 时确实漏了一批文件，其中最要命的是 libstlport.so ——
      原厂 setup-makefiles 根本没列它（它是 NDK 带进来的），但 glgps/gpslogd/
      mtk_agpsd/libnvcameratools/libnvcapaudioservice/libwvdrmengine 都以
      DT_NEEDED 依赖它。少了它这些 blob 全部 dlopen 失败。
      所以补回来的条目单独放一个文件，由本脚本合并。

    格式：每行 `源路径:安装目标路径`，# 开头是注释。
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            src, dest = line.split(":", 1)
            out.append((src.strip(), dest.strip()))
    return out


def load_blacklist(path):
    """返回 {dest: reason}。dest 里允许写 vendor/lib/... 简写。"""
    bl = {}
    if not os.path.exists(path):
        return bl
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 1)
            dest = parts[0]
            reason = parts[1] if len(parts) > 1 else ""
            bl[dest] = reason
    return bl


def norm_dest(dest):
    """把 $(TARGET_COPY_OUT_VENDOR)/lib/x.so 归一化成 vendor/lib/x.so，便于匹配黑名单。"""
    d = dest.replace("$(TARGET_COPY_OUT_VENDOR)", "vendor")
    d = d.replace("$(TARGET_COPY_OUT_SYSTEM)", "system")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/dy/aosp14")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    root = args.root
    vdir = os.path.join(root, "vendor/nvidia/ardbeg")
    mk_path = os.path.join(vdir, "ardbeg-vendor-blobs.mk")
    orig_path = mk_path + ".orig-4.4.4"

    # 第一次运行时把当前文件备份成 .orig
    if not os.path.exists(orig_path):
        with open(mk_path, encoding="utf-8", errors="replace") as f:
            data = f.read()
        with open(orig_path, "w", encoding="utf-8") as f:
            f.write(data)
        print("已备份原始清单 -> %s" % orig_path)

    entries = parse_mk(orig_path)

    # 合并「从原厂镜像补回来」的条目。放在 orig 之后，去重时以 orig 为准，
    # 避免同一 dest 出现两条（PRODUCT_COPY_FILES 里同一 dest 两条会互相覆盖）。
    rec_path = os.path.join(vdir, "blobs-recovered.txt")
    recovered = load_recovered(rec_path)
    seen_dest = set(norm_dest(d) for s, d in entries)
    n_rec_added = 0
    for src, dest in recovered:
        if norm_dest(dest) in seen_dest:
            continue
        entries.append((src, dest))
        seen_dest.add(norm_dest(dest))
        n_rec_added += 1

    blacklist = load_blacklist(os.path.join(vdir, "blobs-blacklist.txt"))

    kept, dropped_missing, dropped_black = [], [], []
    for src, dest in entries:
        if not os.path.exists(os.path.join(root, src)):
            dropped_missing.append((src, dest))
            continue
        nd = norm_dest(dest)
        reason = None
        if nd in blacklist:
            reason = blacklist[nd]
        else:
            # 支持 "system/usr/keylayout/*" 这种整目录排除
            for pat, r in blacklist.items():
                if pat.endswith("/*") and nd.startswith(pat[:-1]):
                    reason = r
                    break
        if reason is not None:
            dropped_black.append((src, dest, reason))
            continue
        kept.append((src, dest))

    # 按安装目录分组
    groups = OrderedDict()
    for src, dest in kept:
        nd = norm_dest(dest)
        gdir = os.path.dirname(nd)
        groups.setdefault(gdir, []).append((src, dest))
    for g in groups.values():
        g.sort(key=lambda x: x[1])

    out = [MK_HEADER % {"kept": len(kept), "missing": len(dropped_missing),
                        "blacklisted": len(dropped_black)}]

    if dropped_black:
        out.append("# -----------------------------------------------------------------------------\n")
        out.append("# 【黑名单：已排除的条目及原因】（详见 blobs-blacklist.txt）\n")
        out.append("# -----------------------------------------------------------------------------\n")
        for src, dest, reason in sorted(dropped_black, key=lambda x: x[1]):
            out.append("#   %-52s %s\n" % (norm_dest(dest), reason))
        out.append("\n")

    if dropped_missing:
        out.append("# -----------------------------------------------------------------------------\n")
        out.append("# 【源文件缺失：已排除的条目】原厂包里没有这些文件（仓库只提交了 %d 个 blob）\n"
                   % (len(entries) - len(dropped_missing)))
        out.append("# -----------------------------------------------------------------------------\n")
        for src, dest in sorted(dropped_missing, key=lambda x: x[1]):
            out.append("#   %s\n" % norm_dest(dest))
        out.append("\n")

    # 分组概览（只写注释，不打断下面的续行语句）
    out.append("# -----------------------------------------------------------------------------\n")
    out.append("# 【保留条目的安装目录分布】\n")
    out.append("# -----------------------------------------------------------------------------\n")
    for gdir, items in groups.items():
        out.append("#   %-34s %3d\n" % (gdir, len(items)))
    out.append("\n")

    out.append("PRODUCT_COPY_FILES += \\\n")
    # 【重要】这一段必须是**连续的续行**，中间不能插空行或注释。
    # make 的续行规则：反斜杠+换行 表示"下一行仍是本条语句"，
    # 如果中间插入空行，语句就结束了，后面的 "src:dest \" 会被当成
    # 规则行解析，报 "multiple target patterns"。
    # 所以分组信息只写在文件开头的注释里，不插在条目中间。
    flat = []
    for gdir, items in groups.items():
        flat.extend(items)
    for idx, (src, dest) in enumerate(flat):
        cont = " \\" if idx != len(flat) - 1 else ""
        out.append("    %s:%s%s\n" % (src, dest, cont))

    text = "".join(out).rstrip()
    if text.endswith("\\"):
        text = text[:-1].rstrip()

    print("原始条目: %d" % len(entries))
    print("  其中从原厂镜像补回: %d" % n_rec_added)
    print("保留:     %d" % len(kept))
    print("缺失丢弃: %d" % len(dropped_missing))
    print("黑名单:   %d" % len(dropped_black))
    print("分组:     %d" % len(groups))
    for gdir, items in groups.items():
        print("   %-30s %d" % (gdir, len(items)))

    if args.write:
        with open(mk_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("\n已写入 %s" % mk_path)
    else:
        print("\n（未写入，加 --write 生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
