#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_vendor.py — 修复 vendor/nvidia/ardbeg 手写提取原厂 4.4.4 blob 时的三类系统性错误

背景
====
vendor 树是从原厂 4.4.4 线刷包（system.img）里手工抠出来的。抠的过程丢了两类
信息，还有一类判断失误：

  【错误 1】全部 313 个 blob 都是 0644，没有一个带可执行位
      ── 这是最致命的。原厂镜像里的权限记录在解包时没有一起保存，抠出来的
         文件统一变成了 0644。而 7.1 打 system.img 时**不注入 fs_config**
         （build/core/Makefile 的 build-systemimage-target 走 build_image.py，
         只有 prop_dict 里有 "fs_config" 才会给 mkuserimg.sh 传 -C；
         本树的 system_image_info.txt 里没有这一项），
         所以镜像里文件的权限 = out/target/product/ardbeg/system 里的权限
         = PRODUCT_COPY_FILES 里 `cp -f` 之后的权限 = **源文件的权限**。
         实测 `cp -f` 不套 umask，源文件 755 拷完还是 755（见 tools/ 里的验证）。
      ── 后果：system/bin/dcc、system/bin/hotplug、system/bin/mac、
         system/vendor/bin/tegrastats、system/vendor/bin/touch_fusion …
         装进镜像全是 0644，init 拉起时报
             init: cannot execute: Permission denied
         而且这个错误只在开机 log 里出现，编译期完全静默。
      ── 修复：按原厂 meta.txt 里记录的真实 mode 还原；meta.txt 没记录的
         按 AOSP 惯例推断（bin/ 下 0755，其余 0644）。

  【错误 2】黑名单按「消费者没了」删库，没反查「谁还依赖它」
      ── blobs-blacklist.txt 里两条判断是错的：
         * vendor/lib/libussrd.so
             理由写的是「7.1 无对应 init 服务」——但那只说明 ussrd 这个
             **守护进程**不用了（vendor/bin/ussrd 继续黑名单，正确），
             库本身仍被 10 个 libgov_*.so 用 DT_NEEDED 硬依赖：
                 libgov_boot / libgov_camera / libgov_combinator / libgov_force
                 libgov_generic / libgov_gpucompute / libgov_graphics
                 libgov_il / libgov_spincircle / libgov_tbc
             这些是 Tegra 的 GPU DVFS governor，libnvgr/libglcore 会加载。
             删了库 → dlopen 失败 → GPU 调频失效（表现为卡顿/发热/掉帧），
             但**不会崩**，所以极难发现。
         * system/lib/libpowerservice_client.so
             理由写的是「7.1 的 power HAL 用 hardware/nvidia/power」——
             这说的是 power **HAL**，但 vendor/lib/libpowerservice.so
             仍然 DT_NEEDED libpowerservice_client.so。删了 → powerservice
             加载失败。
      ── 修复：把这两个库从黑名单移除（守护进程/服务本体继续排除）。

  【错误 3】三个 4.4 的通用库根本没被提取
      ── 原厂 system.img 里有、但 proprietary/ 里没有：
         * system/lib/libstlport.so            被 glgps_nvidiaTegra2android、
                                               gpslogd、mtk_agpsd、libnvcameratools、
                                               libnvcapaudioservice、libwvdrmengine 依赖
         * system/lib/libpowerservice_client.so 见错误 2
         * system/lib/libasan_preload.so        被 system/bin/asan/app_process 依赖
      ── 修复：从原厂解包目录拷进 proprietary/，并在 mk 里补上条目。

    （libussrd.so 在 proprietary/ 里是有的，只是被黑名单挡掉了，不用拷。）

幂等性
======
重复运行结果不变：已经 0755 的不再动；已存在的库不覆盖（除非 --force-copy）；
黑名单已移除的条目不会再匹配。

用法
====
    python3 fix_vendor.py --dry-run     # 只看会改什么
    python3 fix_vendor.py               # 实际修
    python3 fix_vendor.py --force-copy  # 连库文件也强制重新拷
"""

import argparse
import os
import shutil
import sys

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
VENDOR = "/home/dy/aosp14/vendor/nvidia/ardbeg"
PROPRIETARY = os.path.join(VENDOR, "proprietary")
BLACKLIST = os.path.join(VENDOR, "blobs-blacklist.txt")
MK = os.path.join(VENDOR, "ardbeg-vendor-blobs.mk")
RECOVERED = os.path.join(VENDOR, "blobs-recovered.txt")

# 原厂 4.4.4 解包目录（由 ardbeg-work 里的解包流程产出）
STOCK = "/home/dy/WorkBuddy AI/ardbeg-work/ext4build-work/rom/system"
# 原厂镜像的权限清单：每行 "path uid gid mode caps selinux_context"
STOCK_META = "/home/dy/WorkBuddy AI/ardbeg-work/ext4build-work/meta.txt"

# ---------------------------------------------------------------------------
# 需要从原厂补进来的库
#   (原厂相对路径, proprietary 内相对路径)
#   proprietary 内的路径要和 mk 里的 src 对得上，也就是
#   vendor/nvidia/ardbeg/proprietary/<第二个元素>
# ---------------------------------------------------------------------------
COPY_FROM_STOCK = [
    # 4.4 的 STL。7.1 用 libc++，但 4.4 编译的 blob 仍然按 SONAME 找 libstlport.so，
    # 缺了它们就 dlopen 失败。libstlport 自身只依赖 libc/libstdc++/libm，7.1 都有。
    ("lib/libstlport.so", "lib/libstlport.so"),

    # asan 预加载库，system/bin/asan/app_process 依赖。
    # 只在 ASAN 调试时用，但缺了会让 asan 包装器直接起不来。
    ("lib/libasan_preload.so", "lib/libasan_preload.so"),
]

# ---------------------------------------------------------------------------
# 需要从黑名单移除的条目（dest 形态，和 blobs-blacklist.txt 第一列一致）
# ---------------------------------------------------------------------------
UNBLACKLIST = [
    "vendor/lib/libussrd.so",
    "system/lib/libpowerservice_client.so",
]

# 这些文件即便在 bin/ 下也不需要可执行位（纯数据/配置）
NOT_EXEC_OVERRIDE = set()


# ---------------------------------------------------------------------------
# 一、读原厂 meta.txt
# ---------------------------------------------------------------------------
def load_stock_meta(path):
    """返回 {'system/bin/dcc': 0o755, ...}。path 是镜像内路径（无前导 /）。"""
    modes = {}
    if not os.path.exists(path):
        return modes
    with open(path, "r", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            p, uid, gid, mode = parts[0], parts[1], parts[2], parts[3]
            try:
                modes[p] = int(mode, 8)
            except ValueError:
                continue
    return modes


# ---------------------------------------------------------------------------
# 二、dest → proprietary 源文件路径
# ---------------------------------------------------------------------------
def dest_to_src(dest):
    """
    把 mk 里的 dest 还原成 proprietary/ 内的相对路径。

    mk 里 dest 有两种写法：
        system/lib/foo.so                          （装到 system/lib）
        $(TARGET_COPY_OUT_VENDOR)/lib/foo.so       （装到 system/vendor/lib）

    而 proprietary/ 里的目录结构是：
        proprietary/lib/foo.so
        proprietary/vendor/lib/foo.so

    所以：
        system/xxx            -> proprietary/xxx
        vendor/xxx (展开后)    -> proprietary/vendor/xxx
    """
    d = dest.replace("$(TARGET_COPY_OUT_VENDOR)", "vendor")
    d = d.replace("$(TARGET_COPY_OUT_SYSTEM)", "system")
    if d.startswith("system/"):
        return d[len("system/"):]
    return d


def image_path_of(dest):
    """dest → 镜像内路径（用于查 meta.txt），带前导 system/。"""
    d = dest.replace("$(TARGET_COPY_OUT_VENDOR)", "vendor")
    d = d.replace("$(TARGET_COPY_OUT_SYSTEM)", "system")
    if not d.startswith("system/"):
        d = "system/" + d
    return d


# ---------------------------------------------------------------------------
# 三、推断权限
# ---------------------------------------------------------------------------
def guess_mode(rel_path, stock_modes, dest):
    """
    优先级：
      1) 原厂 meta.txt 有记录 → 用原厂值（最权威）
      2) 路径在 bin/ 下，或者是 .sh 脚本 → 0755
      3) 其余 → 0644

    为什么 .sh 也要 0755：init 的 exec 动作、以及 .rc 里的 service 都可能
    直接 exec 这些脚本（比如 wifi_loader.sh、set_hwui_params.sh）。
    """
    img = image_path_of(dest)
    if img in stock_modes:
        return stock_modes[img], "原厂 meta.txt"
    if "/bin/" in "/" + rel_path or rel_path.endswith(".sh"):
        return 0o755, "bin/ 或 .sh 惯例"
    return 0o644, "普通数据/库"


# ---------------------------------------------------------------------------
# 四、解析 mk 拿到全部条目
# ---------------------------------------------------------------------------
def parse_mk_entries(path):
    """返回 [(src, dest)]。只认 PRODUCT_COPY_FILES 续行里的 src:dest。"""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", errors="replace") as f:
        for line in f:
            s = line.strip().rstrip("\\").strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("PRODUCT_COPY_FILES"):
                continue
            if ":" not in s:
                continue
            # dest 里可能带 $(...) 宏，不能用简单 split(":")
            m = s.split(":", 1)
            if len(m) != 2:
                continue
            src, dest = m[0].strip(), m[1].strip()
            if not src.startswith("vendor/nvidia/ardbeg/proprietary/"):
                continue
            out.append((src, dest))
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-copy", action="store_true")
    args = ap.parse_args()

    stock_modes = load_stock_meta(STOCK_META)
    entries = parse_mk_entries(MK)
    print("解析到 %d 条 PRODUCT_COPY_FILES 条目" % len(entries))
    if not entries:
        print("！mk 里没解析到条目，先确认文件格式")
        return 1

    # =====================================================================
    # 步骤 1：补拷缺失的库
    # =====================================================================
    print("\n=== 步骤 1：从原厂镜像补拷缺失的库 ===")
    copied = 0
    for stock_rel, prop_rel in COPY_FROM_STOCK:
        src = os.path.join(STOCK, stock_rel)
        dst = os.path.join(PROPRIETARY, prop_rel)
        if not os.path.exists(src):
            print("  ！原厂里也没有：%s —— 跳过" % stock_rel)
            continue
        if os.path.exists(dst) and not args.force_copy:
            print("  已存在，跳过：%s" % prop_rel)
            continue
        print("  拷贝 %s -> proprietary/%s" % (stock_rel, prop_rel))
        if not args.dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        copied += 1
    print("  补拷 %d 个文件" % copied)

    # =====================================================================
    # 步骤 2：修黑名单
    # =====================================================================
    print("\n=== 步骤 2：修正黑名单 ===")
    if os.path.exists(BLACKLIST):
        with open(BLACKLIST, "r", errors="replace") as f:
            lines = f.readlines()
    else:
        lines = []

    removed = []
    kept_lines = []
    for line in lines:
        s = line.strip()
        hit = None
        if s and not s.startswith("#"):
            first = s.split()[0]
            if first in UNBLACKLIST:
                hit = first
        if hit:
            removed.append(hit)
            # 保留一行注释说明为什么解禁，方便以后 review
            kept_lines.append(
                "# [已解禁] %s\n"
                "#   %s\n"
                % (hit,
                   "该库被其他**保留的** blob 以 DT_NEEDED 依赖，删掉会导致 dlopen 失败；"
                   "被排除的只是使用它的守护进程/服务本体，不是库本身"))
        else:
            kept_lines.append(line)

    for u in UNBLACKLIST:
        if u not in removed:
            print("  黑名单里本来就没有 %s" % u)
        else:
            print("  解禁 %s" % u)

    if not args.dry_run and removed:
        # 把解禁说明追加到文件头部的统计区之后
        with open(BLACKLIST, "w", errors="replace") as f:
            f.writelines(kept_lines)

    # =====================================================================
    # 步骤 3：还原可执行位
    # =====================================================================
    print("\n=== 步骤 3：还原文件权限 ===")
    changed = []
    touched = 0
    for src, dest in entries:
        rel = dest_to_src(dest)
        full = os.path.join(PROPRIETARY, rel)
        if not os.path.exists(full) or os.path.islink(full):
            continue
        want, why = guess_mode(rel, stock_modes, dest)
        cur = os.stat(full).st_mode & 0o7777
        if cur == want:
            # 【关键】权限没变也要 touch 一下 mtime。
            #   原因：PRODUCT_COPY_FILES 的规则是
            #       $(PRODUCT_OUT)/system/lib/foo.so: <源文件>
            #       cp -f <源文件> $@
            #   ninja 判断"要不要重跑"靠的是**源文件 mtime**。
            #   chmod 只改 ctime 不改 mtime，所以第一次修完权限后，
            #   ninja 认为拷贝结果还是最新的，**不会重新拷**，
            #   镜像里仍然是 0644 —— 修复看起来"没生效"。
            #   这里统一 touch 一遍，强制下游重拷。
            if not args.dry_run:
                os.utime(full, None)
                touched += 1
            continue
        changed.append((rel, cur, want, why))
        if not args.dry_run:
            os.chmod(full, want)
            os.utime(full, None)

    for rel, cur, want, why in changed:
        print("  %-46s %04o -> %04o  (%s)" % (rel, cur, want, why))
    print("  共调整 %d 个文件权限，touch %d 个" % (len(changed), touched))

    # =====================================================================
    # 步骤 4：把补拷的库写进 blobs-recovered.txt
    #   为什么单独一个文件而不直接改 mk：
    #     mk 是 gen_vendor_mk.py 生成的，直接改会被下次重新生成覆盖掉。
    #     而 .orig-4.4.4 是「原厂清单」的存档，不该往里塞原厂清单里没有的东西。
    #     所以新增一个「从原厂镜像补回来」的清单，由 gen_vendor_mk.py 合并。
    # =====================================================================
    print("\n=== 步骤 4：登记补拷的库到 blobs-recovered.txt ===")
    rec_entries = []
    for stock_rel, prop_rel in COPY_FROM_STOCK:
        # 判断装到 system/lib 还是 system/vendor/lib
        if prop_rel.startswith("vendor/"):
            dest = "$(TARGET_COPY_OUT_VENDOR)/" + prop_rel[len("vendor/"):]
        else:
            dest = "system/" + prop_rel
        rec_entries.append(
            ("vendor/nvidia/ardbeg/proprietary/" + prop_rel, dest, stock_rel))

    header = """\
# =============================================================================
# blobs-recovered.txt — 从原厂 4.4.4 system.img 补回来的闭源文件
#
# 【为什么需要这个文件】
#   ardbeg-vendor-blobs.mk.orig-4.4.4 是「原厂 setup-makefiles.sh 生成的清单」
#   的存档，它列了 345 条，其中 35 条指向包里根本没有的文件。
#   手工提取 blob 时还有一批**原厂清单里有、但提取时漏了**的文件，
#   以及一批**原厂清单里也没有、但原厂镜像里确实存在**的库
#   （典型：libstlport.so，它是 NDK 带进来的，原厂 setup-makefiles 没列）。
#
#   这些文件必须补进来，否则保留的 blob 会 DT_NEEDED 解析失败，
#   开机 dlopen 报 "library ... not found"。
#
# 【格式】每行  源路径:安装目标路径
#   由 tools/gen_vendor_mk.py 合并进 ardbeg-vendor-blobs.mk。
#   本文件由 tools/fix_vendor.py 维护，也可以手工追加。
# =============================================================================

"""
    body = []
    for src, dest, why in rec_entries:
        body.append("# 来自原厂 %s\n%s:%s\n" % (why, src, dest))

    text = header + "\n".join(body)
    if args.dry_run:
        print("  （dry-run）将写入 %d 条到 %s" % (len(rec_entries), RECOVERED))
    else:
        with open(RECOVERED, "w", errors="replace") as f:
            f.write(text)
        print("  已写入 %d 条到 %s" % (len(rec_entries), RECOVERED))

    # =====================================================================
    print("\n" + "=" * 70)
    print("完成：补拷 %d，解禁 %d，权限调整 %d"
          % (copied, len(removed), len(changed)))
    if args.dry_run:
        print("（dry-run，未落盘）")
    else:
        print("下一步：python3 tools/gen_vendor_mk.py --write  重新生成 mk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
