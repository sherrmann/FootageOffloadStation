#!/bin/bash
# CI stage 3 (release tags): boot a real Raspberry Pi OS Lite image in QEMU
# system emulation, install the .deb on first boot and wait for the web UI.
#
#   sudo ci/stage3-boot.sh prepare IMAGE_URL DEB WORKDIR
#   ci/stage3-boot.sh boot WORKDIR          (starts QEMU in the background)
#   ci/stage3-boot.sh wait URL SECONDS      (polls until URL answers)
#   ci/stage3-boot.sh logs WORKDIR          (stops the guest, prints its journal)
#   ci/stage3-boot.sh stop WORKDIR
#
# Trade-off: QEMU cannot emulate a Pi Zero W usefully (its raspi0 machine has
# no USB networking), so the image boots on the "raspi3b" machine with the
# image's own 64-bit kernel (kernel8.img, armhf userland in compat mode) and a
# usb-net NIC; it falls back to "raspi2b" with kernel7.img. Stage 3 therefore
# proves the real image boots, systemd starts both units without a login,
# the postinst works with systemd running and the web UI answers. It does not
# prove armv6 kernel behaviour; the armv6 userland is covered by stage 2.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CI_USER=ci
CI_PASSWORD=${CI_PASSWORD:-footage-offload-ci}

prepare() {
    local url=$1 deb=$2 work=$3 loop2 mnt
    mkdir -p "$work"
    work=$(cd "$work" && pwd)
    "$HERE/rpios-image.sh" fetch "$url" "$work/rpios.img"

    # SD card images must have a power-of-two size; grow the root partition
    # and file system into the new space (the image's own first-boot resize
    # is bypassed because QEMU gets its own kernel command line).
    qemu-img resize -q -f raw "$work/rpios.img" 8G
    echo ', +' | sfdisk -q --no-reread -N 2 "$work/rpios.img"

    # Boot partition (FAT) through mtools: no vfat kernel module needed.
    local start boot
    read -r start < <(partx -g -o START -n 1 "$work/rpios.img")
    boot="$work/rpios.img@@$((start * 512))"
    export MTOOLS_SKIP_CHECK=1

    # Login for debugging: user "ci" (Bookworm's userconfig.service reads
    # userconf.txt) and SSH (sshswitch.service reads the "ssh" file).
    printf '%s:%s\n' "$CI_USER" "$(openssl passwd -6 "$CI_PASSWORD")" > "$work/userconf.txt"
    : > "$work/ssh"
    mcopy -o -i "$boot" "$work/userconf.txt" "$work/ssh" ::

    # Kernel and device tree for QEMU.
    if mcopy -n -i "$boot" ::kernel8.img "$work/kernel.img" 2>/dev/null; then
        mcopy -n -i "$boot" ::bcm2710-rpi-3-b.dtb "$work/board.dtb"
        echo raspi3b > "$work/machine"
    else
        mcopy -n -i "$boot" ::kernel7.img "$work/kernel.img"
        mcopy -n -i "$boot" ::bcm2709-rpi-2-b.dtb "$work/board.dtb"
        echo raspi2b > "$work/machine"
    fi

    loop2=$("$HERE/rpios-image.sh" part-loop "$work/rpios.img" 2)
    mnt="$work/mnt"
    mkdir -p "$mnt"
    # shellcheck disable=SC2064 # expand now: the values are fixed
    trap "cleanup_mounts '$mnt' '$loop2'" EXIT
    e2fsck -fy "$loop2" >/dev/null || true
    resize2fs "$loop2" >/dev/null
    mount "$loop2" "$mnt"
    # Persistent journal, so `logs` can read it from the image afterwards.
    mkdir -p "$mnt/var/log/journal" "$mnt/etc/systemd/journald.conf.d"
    printf '[Journal]\nStorage=persistent\nSyncIntervalSec=10s\n' \
        > "$mnt/etc/systemd/journald.conf.d/fos-ci.conf"

    # Dependencies are installed now, in a chroot under QEMU user emulation
    # (much faster than inside the emulated machine and independent of its
    # network). The package itself is installed on first boot, with systemd
    # running, by a one-shot unit.
    chroot_deps "$mnt" "$deb"

    mkdir -p "$mnt/opt/fos-ci"
    cp "$deb" "$mnt/opt/fos-ci/"
    cat > "$mnt/etc/systemd/system/fos-ci-install.service" <<'EOF'
[Unit]
Description=CI: install the footage-offload package
ConditionPathExists=!/opt/fos-ci/installed
After=local-fs.target

[Service]
Type=oneshot
Environment=DEBIAN_FRONTEND=noninteractive
ExecStart=/bin/sh -c 'apt-get install -y /opt/fos-ci/footage-offload_*_all.deb > /opt/fos-ci/install.log 2>&1; echo "exit $?" >> /opt/fos-ci/install.log; touch /opt/fos-ci/installed'
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
EOF
    ln -sf /etc/systemd/system/fos-ci-install.service \
        "$mnt/etc/systemd/system/multi-user.target.wants/fos-ci-install.service"

    sync
    cleanup_mounts "$mnt" "$loop2"
    trap - EXIT
}

chroot_deps() {
    local mnt=$1 deb=$2 depends recommends preload_saved=0
    depends=$(dpkg-deb -f "$deb" Depends)
    recommends=$(dpkg-deb -f "$deb" Recommends)
    mount --bind /proc "$mnt/proc"
    mount --bind /sys "$mnt/sys"
    mount --bind /dev "$mnt/dev"
    mount --bind /dev/pts "$mnt/dev/pts"
    if [ -e "$mnt/etc/resolv.conf" ] || [ -L "$mnt/etc/resolv.conf" ]; then
        mv "$mnt/etc/resolv.conf" "$mnt/etc/resolv.conf.ci"
    fi
    cp /etc/resolv.conf "$mnt/etc/resolv.conf"
    if [ -s "$mnt/etc/ld.so.preload" ]; then
        mv "$mnt/etc/ld.so.preload" "$mnt/etc/ld.so.preload.ci"
        preload_saved=1
    fi
    printf '#!/bin/sh\nexit 101\n' > "$mnt/usr/sbin/policy-rc.d"
    chmod 0755 "$mnt/usr/sbin/policy-rc.d"
    chroot "$mnt" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get update -q
    chroot "$mnt" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
        apt-get satisfy -y -q --no-install-recommends "$depends, $recommends"
    chroot "$mnt" apt-get clean
    rm -f "$mnt/usr/sbin/policy-rc.d" "$mnt/etc/resolv.conf"
    if [ -e "$mnt/etc/resolv.conf.ci" ] || [ -L "$mnt/etc/resolv.conf.ci" ]; then
        mv "$mnt/etc/resolv.conf.ci" "$mnt/etc/resolv.conf"
    fi
    if [ "$preload_saved" = 1 ]; then
        mv "$mnt/etc/ld.so.preload.ci" "$mnt/etc/ld.so.preload"
    fi
    umount "$mnt/dev/pts" "$mnt/dev" "$mnt/sys" "$mnt/proc"
}

cleanup_mounts() {
    local mnt=$1 m
    shift
    for m in dev/pts dev sys proc; do
        if mountpoint -q "$mnt/$m"; then umount "$mnt/$m" || true; fi
    done
    if mountpoint -q "$mnt"; then umount "$mnt" || true; fi
    for m in "$@"; do
        losetup -d "$m" 2>/dev/null || true
    done
}

boot() {
    # earlycon: kernel messages from the start. module_blacklist: on a Pi 3
    # device tree the Bluetooth driver would take over the PL011 UART that
    # carries the console (QEMU's first -serial).
    local work=$1 machine qemu
    machine=$(cat "$work/machine")
    if [ "$machine" = raspi3b ]; then qemu="qemu-system-aarch64"; else qemu="qemu-system-arm"; fi
    "$qemu" -M "$machine" -m 1G -smp 4 \
        -kernel "$work/kernel.img" -dtb "$work/board.dtb" \
        -drive "file=$work/rpios.img,if=sd,format=raw" \
        -append "earlycon=pl011,0x3f201000 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait rw fsck.repair=yes dwc_otg.lpm_enable=0 module_blacklist=hci_uart,btbcm" \
        -device usb-net,netdev=net0 \
        -netdev user,id=net0,hostfwd=tcp:127.0.0.1:8080-:8080,hostfwd=tcp:127.0.0.1:2222-:22 \
        -display none -monitor none -serial "file:$work/console.log" \
        -pidfile "$work/qemu.pid" -daemonize
    echo "stage3: $qemu -M $machine started; console log in $work/console.log"
}

wait_url() {
    local url=$1 seconds=$2 start
    start=$(date +%s)
    while true; do
        if curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
            echo "stage3: $url answers after $(( $(date +%s) - start )) s"
            return 0
        fi
        if [ $(( $(date +%s) - start )) -ge "$seconds" ]; then
            echo "stage3: $url did not answer within $seconds s" >&2
            return 1
        fi
        sleep 10
    done
}

logs() {
    # Stops the guest, then reads its journal and the install log from the
    # image (no SSH needed).
    local work=$1 loop mnt
    stop "$work"
    sleep 5
    echo "--- last console lines ---"
    tail -n 100 "$work/console.log" 2>/dev/null || true
    mnt="$work/logs-mnt"
    mkdir -p "$mnt"
    loop=$("$HERE/rpios-image.sh" part-loop "$work/rpios.img" 2 ro)
    if mount -o ro,noload "$loop" "$mnt"; then
        echo "--- /opt/fos-ci/install.log ---"
        cat "$mnt/opt/fos-ci/install.log" 2>/dev/null || echo "(none)"
        echo "--- journal: footage-offload units and first-boot install ---"
        journalctl -D "$mnt/var/log/journal" --no-pager -o short-monotonic \
            -u footage-offload.service -u footage-offload-helper.service \
            -u fos-ci-install.service 2>&1 | tail -n 300
        echo "--- journal: errors ---"
        journalctl -D "$mnt/var/log/journal" --no-pager -p err 2>&1 | tail -n 100
        umount "$mnt"
    fi
    losetup -d "$loop" || true
}

stop() {
    local work=$1
    if [ -f "$work/qemu.pid" ]; then
        kill "$(cat "$work/qemu.pid")" 2>/dev/null || true
    fi
}

case "${1:-}" in
    prepare) [ $# -eq 4 ] || { echo "usage: $0 prepare IMAGE_URL DEB WORKDIR" >&2; exit 2; }
        prepare "$2" "$3" "$4" ;;
    boot) boot "$2" ;;
    wait) wait_url "$2" "$3" ;;
    logs) logs "$2" ;;
    stop) stop "$2" ;;
    *) echo "usage: $0 prepare|boot|wait|logs|stop ..." >&2; exit 2 ;;
esac
