#!/system/bin/sh
# Copyright (c) 2012-2014, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

# start wpa_supplicant
/system/bin/log -t "wpa_supplicant.sh" -p i "Executing wpa_supplicant for BRCM chips (forced)"
/system/bin/wpa_supplicant \
    -iwlan0 \
    -Dnl80211 \
    -c/data/misc/wifi/wpa_supplicant.conf \
    -O/data/misc/wifi/sockets \
    -e/data/misc/wifi/entropy.bin \
    -puse_p2p_group_interface=1,p2p_device=1 \
    -g@android:wpa_wlan0
