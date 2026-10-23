#!/bin/sh
# Test CLUD on Raspberry Pi OS, from a Linux x86-64 PC: boots the real Raspberry Pi OS
# (64-bit, desktop) in QEMU with Debian's 16K-page arm64 kernel, so memory pages are the size
# a Raspberry Pi 5 uses. The desktop (labwc, Wayland) runs as it does on a Pi.
#
#   tests/pi-vm.sh up             create/boot the VM (first run downloads ~1.5 GB, needs sudo)
#   tests/pi-vm.sh test [fs]      build a stick (vfat, default, or exfat) on this PC with the
#                                 installer, plug it into the VM, and check CLUD runs there
#   tests/pi-vm.sh stick [fs]     only build the stick and plug it in (mounts at /media/pi/CLUD)
#   tests/pi-vm.sh shot [file]    screenshot of the VM's desktop (default /tmp/pi-vm.png)
#   tests/pi-vm.sh ssh [cmd] | vnc | down | destroy
#
# The desktop is also on VNC at 127.0.0.1:5901 (no password). It's emulated (no KVM for ARM
# on x86), so give it a minute to boot. To sign in to Claude in the VM, run CLUD in tmux over
# ssh: tests/pi-vm.sh ssh -t 'tmux new -A -s clud sh /media/pi/CLUD/CLUD.sh'
#
# Needs: sudo apt install qemu-system-arm qemu-user binfmt-support qemu-utils parted
#        dosfstools exfatprogs xz-utils cloud-guest-utils
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="$ROOT/.vm-pi"
PORT="${CLUD_PI_PORT:-2223}"
PI_IMAGE_URL="https://downloads.raspberrypi.com/raspios_arm64_latest"
DEBIAN="http://deb.debian.org/debian"
STICK_SIZE=4G

ssh_vm() {
    ssh -q -i "$VM/id_ed25519" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 pi@127.0.0.1 "$@"
}

running() { [ -f "$VM/qemu.pid" ] && kill -0 "$(cat "$VM/qemu.pid")" 2>/dev/null; }

monitor() {
    # Send QEMU monitor commands, one per argument
    python3 - "$VM/monitor.sock" "$@" <<'EOF'
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
time.sleep(0.3); s.recv(65536)
for cmd in sys.argv[2:]:
    s.send((cmd + "\n").encode()); time.sleep(1)
    out = s.recv(65536).decode(errors="replace").split("\n")[1:-1]
    print("\n".join(l.rstrip("\r") for l in out if l.strip() and not l.startswith("(qemu)")))
EOF
}

create() {
    for tool in qemu-system-aarch64 qemu-img parted growpart; do
        command -v "$tool" >/dev/null || { echo "Missing $tool, see the top of $0"; exit 1; }
    done
    [ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || { echo "Install qemu-user and binfmt-support"; exit 1; }
    mkdir -p "$VM/dl"
    if [ ! -f "$VM/dl/pios.img.xz" ]; then
        echo "Downloading Raspberry Pi OS (64-bit, desktop)..."
        curl -fL --progress-bar -o "$VM/dl/pios.img.xz.part" "$PI_IMAGE_URL"
        mv "$VM/dl/pios.img.xz.part" "$VM/dl/pios.img.xz"
    fi
    # Debian trixie's 16K-page arm64 kernel: a Pi kernel won't boot on QEMU's "virt" board
    deb=$(curl -fsSL "$DEBIAN/dists/trixie/main/binary-arm64/Packages.xz" | xz -d \
          | awk '/^Package: linux-image-.*-arm64-16k-unsigned$/ {p=1} p && /^Filename:/ {print $2; p=0}' \
          | sort -V | tail -1)
    [ -n "$deb" ] || { echo "Couldn't find Debian's arm64-16k kernel"; exit 1; }
    [ -f "$VM/dl/kernel.deb" ] || curl -fL --progress-bar -o "$VM/dl/kernel.deb" "$DEBIAN/$deb"
    [ -f "$VM/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C clud-pi -f "$VM/id_ed25519"

    echo "Unpacking and preparing the image (needs sudo)..."
    xz -dcT0 "$VM/dl/pios.img.xz" > "$VM/pi.img"
    truncate -s 24G "$VM/pi.img"
    loop=$(sudo losetup -fP --show "$VM/pi.img")
    mnt=$(mktemp -d)
    trap 'sudo umount -R "$mnt" 2>/dev/null || true; sudo losetup -d "$loop" 2>/dev/null || true' EXIT
    sudo growpart "$loop" 2 >/dev/null
    sudo e2fsck -pf "${loop}p2" >/dev/null || true
    sudo resize2fs "${loop}p2" >/dev/null 2>&1
    sudo mount "${loop}p2" "$mnt"
    for d in dev proc sys; do sudo mount --bind "/$d" "$mnt/$d"; done
    sudo cp "$VM/dl/kernel.deb" "$mnt/tmp/kernel.deb"
    sudo cp "$VM/id_ed25519.pub" "$mnt/tmp/key.pub"
    # Pi OS ships a disabled "pi" account and a first-boot wizard; set up what Raspberry Pi
    # Imager would: user pi (password clud), ssh, auto-login to the desktop
    sudo chroot "$mnt" sh -e <<'EOF'
echo MODULES=most > /etc/initramfs-tools/conf.d/vm.conf
dpkg -i /tmp/kernel.deb >/dev/null 2>&1 || dpkg -i /tmp/kernel.deb
id pi >/dev/null 2>&1 || useradd -m pi
usermod -s /bin/bash -aG sudo,video,audio,input,render,plugdev,netdev,users pi
echo pi:clud | chpasswd
echo 'pi ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/010_pi-nopasswd
sed -i 's/^autologin-user=.*/autologin-user=pi/' /etc/lightdm/lightdm.conf
rm -f /etc/xdg/autostart/piwiz.desktop
systemctl enable ssh >/dev/null 2>&1
systemctl disable userconfig.service rpi-resize.service >/dev/null 2>&1 || true
touch /etc/cloud/cloud-init.disabled
echo clud-pi > /etc/hostname
sed -i 's/raspberrypi/clud-pi/' /etc/hosts
install -d -o pi -g pi -m 700 /home/pi/.ssh
install -o pi -g pi -m 600 /tmp/key.pub /home/pi/.ssh/authorized_keys
rm /tmp/kernel.deb /tmp/key.pub
EOF
    sudo sh -c "cp '$mnt'/boot/vmlinuz-*-16k '$VM/vmlinuz' && cp '$mnt'/boot/initrd.img-*-16k '$VM/initrd.img'"
    sudo chown "$(id -u):$(id -g)" "$VM/vmlinuz" "$VM/initrd.img"
    sudo umount -R "$mnt"
    sudo losetup -d "$loop"
    trap - EXIT
    qemu-img convert -O qcow2 "$VM/pi.img" "$VM/disk.qcow2"
    rm "$VM/pi.img"
}

up() {
    running && { echo "VM already running (ssh: tests/pi-vm.sh ssh)"; return; }
    [ -f "$VM/disk.qcow2" ] || create
    echo "Booting Raspberry Pi OS..."
    qemu-system-aarch64 -machine virt -cpu max,pauth-impdef=on -smp 4 -m 4096 -accel tcg,thread=multi \
        -kernel "$VM/vmlinuz" -initrd "$VM/initrd.img" \
        -append "root=/dev/vda2 rootfstype=ext4 rw console=ttyAMA0" \
        -drive if=virtio,format=qcow2,file="$VM/disk.qcow2" \
        -device virtio-gpu-pci,xres=1600,yres=900 -device qemu-xhci -device usb-kbd -device usb-tablet \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$PORT"-:22 -device virtio-net-pci,netdev=n0 \
        -display none -vnc 127.0.0.1:1 -monitor unix:"$VM/monitor.sock",server,nowait \
        -serial file:"$VM/console.log" -daemonize -pidfile "$VM/qemu.pid"
    printf "Waiting for SSH"
    i=0
    until ssh_vm true 2>/dev/null; do
        i=$((i + 1)); [ $i -gt 100 ] && { echo; echo "VM didn't come up, see .vm-pi/console.log"; exit 1; }
        printf "."; sleep 3
    done
    echo
    printf "Waiting for the desktop"
    i=0
    until ssh_vm 'test -S /run/user/1000/wayland-0' 2>/dev/null; do
        i=$((i + 1)); [ $i -gt 60 ] && { echo; echo "No desktop session"; exit 1; }
        printf "."; sleep 3
    done
    echo
    ssh_vm 'command -v tmux >/dev/null || sudo apt-get install -y -qq tmux >/dev/null 2>&1' || true
    echo "VM ready: page size $(ssh_vm getconf PAGESIZE), kernel $(ssh_vm uname -r)."
}

down() {
    running || { echo "VM not running"; return; }
    ssh_vm "sudo poweroff" 2>/dev/null || true
    i=0
    while running && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
    running && kill "$(cat "$VM/qemu.pid")"
    rm -f "$VM/qemu.pid"
    echo "VM stopped."
}

unplug() {
    if monitor "info usb" | grep -q "ID: clud"; then
        ssh_vm 'udisksctl unmount -b "$(findmnt -nro SOURCE /media/pi/CLUD)" >/dev/null 2>&1' || true
        monitor "device_del clud" "drive_del cludstick" >/dev/null
        sleep 2
    fi
}

# Build a stick image on this PC with the installer, like a user would with a real stick,
# then hot-plug it into the VM as a USB drive. Pi OS automounts it at /media/pi/CLUD.
stick() {
    fs="${1:-vfat}"
    unplug
    img="$VM/stick.img"
    rm -f "$img" && truncate -s "$STICK_SIZE" "$img"
    parted -s "$img" mklabel msdos mkpart primary fat32 1MiB 100%
    loop=$(sudo losetup -fP --show "$img")
    mnt=$(mktemp -d)
    trap 'sudo umount "$mnt" 2>/dev/null || true; sudo losetup -d "$loop" 2>/dev/null || true' EXIT
    case "$fs" in
        vfat)  sudo mkfs.vfat -F 32 -n CLUD "${loop}p1" >/dev/null
               opts="uid=$(id -u),gid=$(id -g),shortname=mixed,utf8=1,showexec,flush" ;;
        exfat) sudo mkfs.exfat -q -L CLUD "${loop}p1" >/dev/null
               opts="uid=$(id -u),gid=$(id -g)" ;;
        *) echo "Unknown filesystem: $fs (use vfat or exfat)"; exit 1 ;;
    esac
    # Mounted the way udisks mounts sticks on a desktop
    sudo mount -o "$opts" "${loop}p1" "$mnt"
    "$ROOT/CLUD-Installer.sh" --target "$mnt" --yes --platforms "${CLUD_PLATFORMS:-linux-arm64,windows}" \
        | grep -v '%'
    sudo umount "$mnt"
    sudo losetup -d "$loop"
    trap - EXIT
    monitor "drive_add 0 if=none,id=cludstick,format=raw,file=$img" \
            "device_add usb-storage,drive=cludstick,id=clud,removable=on" >/dev/null
    printf "Waiting for Pi OS to mount the stick"
    i=0
    until ssh_vm 'test -f /media/pi/CLUD/CLUD.sh' 2>/dev/null; do
        i=$((i + 1)); [ $i -gt 40 ] && { echo; echo "Stick didn't mount"; exit 1; }
        printf "."; sleep 2
    done
    echo
}

shot() {
    out="${1:-/tmp/pi-vm.png}"
    scp -q -i "$VM/id_ed25519" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$ROOT/tests/mcp_probe.py" pi@127.0.0.1:/tmp/mcp_probe.py
    ssh_vm 'export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0; rm -rf /tmp/shot
        python3 /tmp/mcp_probe.py /tmp/shot /media/pi/CLUD/linux-arm64/py/bin/python3.exe \
            /media/pi/CLUD/mcp/clud_desktop.py Screenshot {} >/dev/null 2>&1' \
        && ssh_vm 'cat /tmp/shot/00-Screenshot.png' > "$out" && echo "Saved $out"
}

e2e() {
    stick "${1:-vfat}"
    scp -q -i "$VM/id_ed25519" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$ROOT/tests/mcp_probe.py" pi@127.0.0.1:/tmp/mcp_probe.py
    ssh_vm sh -s <<'EOF'
set -e
S=/media/pi/CLUD
fail() { echo "FAIL: $*"; exit 1; }
echo "== Page size $(getconf PAGESIZE), $(findmnt -nro FSTYPE,OPTIONS /media/pi/CLUD | cut -c1-60)..."
for f in CLUD.sh CLUD.desktop clud-version.json linux-arm64/claude.exe linux-arm64/py/bin/python3.exe \
         mcp/clud_desktop.py config/CLAUDE.md; do
    [ -e "$S/$f" ] || fail "$f missing on the stick"
done
[ -x "$S/linux-arm64/claude.exe" ] || fail "claude.exe isn't executable from the stick"
echo "== Claude Code: $("$S/linux-arm64/claude.exe" --version)"
echo "== Computer use (Status, Screenshot, Move, Shortcut)"
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
python3 /tmp/mcp_probe.py /tmp/probe "$S/linux-arm64/py/bin/python3.exe" "$S/mcp/clud_desktop.py" 2>&1 \
    | grep -v '^tools' || fail "computer-use server"
echo "== The launcher starts Claude Code (in tmux, as a terminal would)"
tmux kill-session -t e2e 2>/dev/null || true
tmux new-session -d -s e2e -x 160 -y 40 "sh $S/CLUD.sh /tmp; sleep 600"
i=0
until pgrep -f "linux-arm64/claude.exe --dangerously-skip-permissions --mcp-config" >/dev/null; do
    i=$((i + 1)); [ $i -gt 30 ] && fail "CLUD.sh didn't start claude.exe"; sleep 2
done
sleep 5
pgrep -f "linux-arm64/claude.exe" >/dev/null || fail "claude.exe exited right away"
tmux kill-session -t e2e
echo "PASS"
EOF
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    destroy) down || true; rm -rf "$VM"; echo "VM deleted." ;;
    stick) stick "${2:-vfat}" ;;
    shot) shot "${2:-}" ;;
    ssh) shift; if [ $# -eq 0 ]; then ssh_vm -t; else ssh_vm "$@"; fi ;;
    vnc) echo "VNC: 127.0.0.1:5901" ;;
    test) up; e2e "${2:-vfat}" ;;
    *) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
