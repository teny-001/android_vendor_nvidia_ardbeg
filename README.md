# android_vendor_nvidia_ardbeg — 分支 `cm-14.1`

NVIDIA Tegra K1 T124 / TN8 P1761（ardbeg / G100）的原厂预编译 blob 树，
对应 **LineageOS 14.1 / Android 7.1.2** ROM。落地路径固定为
`vendor/nvidia/ardbeg`，由 `device/nvidia/ardbeg/device.mk` 通过
`ardbeg-vendor.mk` → `ardbeg-vendor-blobs.mk` 继承。

配套：`teny-001/android_device_nvidia_ardbeg` @ `cm-14.1`，
其 `DEPENDENCIES.md` 是完整依赖说明。

---

## 分支

| 分支 | 内容 |
| :--- | :--- |
| `main` | 初始骨架 |
| `cm-11` | 原厂 4.4.4 / CM11 时代的 vendor 树 |
| **`cm-14.1`（本分支）** | 修复后的 7.1.2 可用版本 |

---

## 内容

```
android_vendor_nvidia_ardbeg @ cm-14.1
├── ardbeg-vendor.mk                    # 产品继承入口
├── BoardConfigVendor.mk                # 板级 vendor 变量（当前基本为空）
├── ardbeg-vendor-blobs.mk              # 为 7.1 重写的 PRODUCT_COPY_FILES 清单
├── ardbeg-vendor-blobs.mk.orig-4.4.4  # 4.4.4 原始清单，仅 diff 留档，不参与构建
├── blobs-blacklist.txt                 # 排除记录（含历史误判说明）
├── blobs-recovered.txt                 # 从原厂包找回 / 补齐的 blob 记录
├── tools/                              # 提取与核对辅助脚本
└── proprietary/                        # 原厂 blob 本体
    ├── lib/          # libnvomx.so、libavcodec.so、libnvdemo.so ...
    ├── vendor/lib/   # libEGL_tegra_impl.so、libglcore.so、libcuda.so ...
    ├── etc/, firmware/, bin/, ...
```

规模：324 个文件 / 约 114 MiB；最大单文件
`proprietary/vendor/lib/libglcore.so` 约 20 MiB，低于 GitHub 单文件 100 MiB
上限，可直接 git push，无需 Git LFS。

---

## 来源与修复

blob 全部来自原厂 Android 4.4.4 线刷包（`/home/dy/k1flash`）与官方
`prebuilts-device-nvidia`，早期由用户手工提取，存在缺文件、路径错位、
`PRODUCT_COPY_FILES` 目标分区仍是 4.4 布局等问题，本分支已逐项修复并
在真机上验证。

### 一处需要留意的历史误判

`blobs-blacklist.txt` 与 device 树旧注释里曾写「Android 7.1 要求
`createOMXPlugin(bool secure)`，所以排除 4.4 的 `libstagefrighthw.so`」。
这是**错的**：当前 CM14.1
`frameworks/av/media/libstagefrighthw/omx/OMXMaster.cpp` 用的是无参

```cpp
typedef OMXPluginBase *(*CreateOMXPluginFunc)();
```

先 `dlsym("createOMXPlugin")`，失败再 fallback 到
`dlsym("_ZN7android15createOMXPluginEv")`，而原厂 4.4 blob 正好导出该无参
符号；NVIDIA 官方 14.1 桥 `hardware/nvidia/libstagefrighthw/omxplugin.cpp`
同样是无参 `createOMXPlugin()`。

这个误判曾导致最终 ROM 里**完全没有** `system/lib/libstagefrighthw.so`，
`OMXMaster::addVendorPlugin()` 拿不到 `OMX.Nvidia.*`。修复后的桥文件已固化在
**device 树**的 `prebuilt/lib/libstagefrighthw.so`（md5
`7221cb3656b33b54846e34b5e406163c`），不在本 vendor 仓库；本文件保留
历史记录以免再次误判。

---

## 硬件解码三条依赖（跨仓库）

| 文件 | 所在 | 作用 |
| :--- | :--- | :--- |
| `prebuilt/lib/libstagefrighthw.so` | device 仓库 | OMXMaster vendor plugin 桥 |
| `media/media_codecs.xml` | device 仓库 | 7.1 兼容 codec 表，避免重复组件名清空 `mCodecInfos` |
| `initfiles/mediacodec.rc` | device 仓库 | `group ... media`，让 `mediacodec` 能开 `/dev/tegra_avpchannel` |

vendor 仓库提供 `libnvomx.so` 及 NvMM / AVP 相关库。三者 + vendor 缺一不可，
真机判据是播放 H.264 出现 `NvMMLiteOpen` 且最终 `EOS detected`。

---

## 依赖与许可

* 必须与 `android_device_nvidia_ardbeg` @ `cm-14.1` 配套使用；单独 clone
  本仓库没有意义。
* **内核源码不需要**：本次使用 device 树内的 `prebuilt/kernel`，
  `android_kernel_nvidia_ardbeg` 保持 4.4 分支不动。
* 编译前仍需执行 device 仓库的 `patches/install.sh` 打 AOSP 补丁。
* blob 版权归 NVIDIA 及原厂所有，仅用于本机移植与调试。
