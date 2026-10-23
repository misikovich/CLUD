#!/bin/sh
# Test CLUD-Installer.sh on Linux from a Mac: boots an Ubuntu 24.04 VM in QEMU with a USB
# stick attached (an emulated usb-storage device, so Linux sees it as removable, same as a
# real stick) and runs the installer against it.
#
#   tests/linux-vm.sh up            create/boot the VM (first run downloads ~600 MB)
#   tests/linux-vm.sh test          sync the repo, format the stick, install, check, update
#   tests/linux-vm.sh tui           sync the repo and open the installer TUI in the VM
#   tests/linux-vm.sh stick [fs]    wipe the stick and format it (exfat, default, or vfat)
#   tests/linux-vm.sh sync | ssh [cmd] | down | destroy
#
# Needs: brew install qemu. VM files live in .vm/ (gitignored). Apple Silicon only for now.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="$ROOT/.vm"
PORT="${CLUD_VM_PORT:-2222}"
IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
QEMU_SHARE="$(brew --prefix qemu 2>/dev/null)/share/qemu"
STICK_SIZE=4G

ssh_vm() {
    ssh -q -i "$VM/id_ed25519" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 clud@127.0.0.1 "$@"
}

running() { [ -f "$VM/qemu.pid" ] && kill -0 "$(cat "$VM/qemu.pid")" 2>/dev/null; }

create() {
    command -v qemu-system-aarch64 >/dev/null || { echo "Install QEMU first: brew install qemu"; exit 1; }
    [ "$(uname -m)" = arm64 ] || { echo "This script expects an Apple Silicon Mac"; exit 1; }
    mkdir -p "$VM"
    if [ ! -f "$VM/base.img" ]; then
        echo "Downloading Ubuntu 24.04 cloud image..."
        curl -fL --progress-bar -o "$VM/base.img.part" "$IMAGE_URL"
        mv "$VM/base.img.part" "$VM/base.img"
    fi
    [ -f "$VM/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C clud-vm -f "$VM/id_ed25519"
    qemu-img create -q -f qcow2 -b "$VM/base.img" -F qcow2 "$VM/disk.qcow2" 20G
    cp "$QEMU_SHARE/edk2-arm-vars.fd" "$VM/vars.fd"
    qemu-img create -q -f raw "$VM/stick.img" "$STICK_SIZE"

    # cloud-init: a "clud" user with our key, the filesystem tools, and a polkit rule so
    # udisksctl can mount the stick over SSH like a desktop session would (/media/clud/...)
    seed="$VM/seed"
    rm -rf "$seed" && mkdir -p "$seed"
    echo "instance-id: clud-vm" > "$seed/meta-data"
    cat > "$seed/user-data" <<EOF
#cloud-config
hostname: clud-vm
users:
  - name: clud
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat "$VM/id_ed25519.pub")
packages: [exfatprogs, dosfstools, parted, udisks2]
runcmd:
  # The cloud kernel leaves exFAT/vfat drivers out; they're in modules-extra
  - apt-get install -y linux-modules-extra-\$(uname -r)
write_files:
  - path: /etc/polkit-1/rules.d/50-clud-udisks.rules
    content: |
      polkit.addRule(function(action, subject) {
        if (subject.user == "clud" && action.id.indexOf("org.freedesktop.udisks2.") == 0)
          return polkit.Result.YES;
      });
EOF
    rm -f "$VM/seed.iso"
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o "$VM/seed.iso" "$seed"
}

up() {
    running && { echo "VM already running (ssh: tests/linux-vm.sh ssh)"; return; }
    [ -f "$VM/disk.qcow2" ] || create
    echo "Booting VM..."
    qemu-system-aarch64 -machine virt -accel hvf -cpu host -smp 4 -m 4096 \
        -drive if=pflash,format=raw,readonly=on,file="$QEMU_SHARE/edk2-aarch64-code.fd" \
        -drive if=pflash,format=raw,file="$VM/vars.fd" \
        -drive if=virtio,format=qcow2,file="$VM/disk.qcow2" \
        -drive if=virtio,format=raw,readonly=on,file="$VM/seed.iso" \
        -device qemu-xhci \
        -drive if=none,id=stick,format=raw,file="$VM/stick.img" \
        -device usb-storage,drive=stick,removable=on \
        -netdev user,id=net0,hostfwd=tcp:127.0.0.1:"$PORT"-:22 -device virtio-net-pci,netdev=net0 \
        -display none -serial file:"$VM/console.log" -daemonize -pidfile "$VM/qemu.pid"
    printf "Waiting for SSH"
    i=0
    until ssh_vm true 2>/dev/null; do
        i=$((i + 1)); [ $i -gt 120 ] && { echo; echo "VM didn't come up, see .vm/console.log"; exit 1; }
        printf "."; sleep 3
    done
    echo
    echo "Waiting for cloud-init (first boot installs packages)..."
    ssh_vm "cloud-init status --wait >/dev/null; sudo modprobe exfat" \
        || { echo "cloud-init failed, see: tests/linux-vm.sh ssh sudo cat /var/log/cloud-init-output.log"; exit 1; }
    echo "VM ready."
}

down() {
    running || { echo "VM not running"; return; }
    ssh_vm "sudo poweroff" 2>/dev/null || true
    i=0
    while running && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
    running && kill "$(cat "$VM/qemu.pid")"
    rm -f "$VM/qemu.pid"
    echo "VM stopped."
}

# Copy the working tree (tracked + untracked, minus ignored) to ~/CLUD in the VM.
# .tools/ survives between syncs so uv and its Python are only fetched once.
sync_repo() {
    cd "$ROOT"
    ssh_vm "mkdir -p CLUD && find CLUD -mindepth 1 -maxdepth 1 ! -name .tools -exec rm -rf {} +"
    git ls-files -co --exclude-standard -z | grep -zv '^tests/' \
        | COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --null -T - -cf - | ssh_vm "tar -xf - -C CLUD"
    echo "Repo synced to ~/CLUD in the VM."
}

# Wipe the stick, give it an MBR partition like a store-bought stick, format it and mount
# it through udisks, which puts it under /media/clud/STICK like a desktop automount would
stick() {
    fs="${1:-exfat}"
    ssh_vm FS="$fs" sh -s <<'EOF'
set -e
dev=$(lsblk -dnpo NAME,TRAN | awk '$2 == "usb" {print $1; exit}')
[ -n "$dev" ] || { echo "No USB stick found in the VM"; exit 1; }
for p in $(lsblk -lnpo NAME "$dev" | tail -n +2); do udisksctl unmount -b "$p" >/dev/null 2>&1 || true; done
sudo wipefs -qa "$dev"
sudo parted -s "$dev" mklabel msdos mkpart primary 1MiB 100%
sudo partprobe "$dev" 2>/dev/null; sleep 1
case "$FS" in
    exfat) sudo mkfs.exfat -q -L STICK "${dev}1" >/dev/null ;;
    vfat)  sudo mkfs.vfat -F 32 -n STICK "${dev}1" >/dev/null ;;
    *) echo "Unknown filesystem: $FS (use exfat or vfat)"; exit 1 ;;
esac
sudo udevadm trigger --action=change "${dev}1"; sudo udevadm settle
udisksctl mount -b "${dev}1"
EOF
}

e2e() {
    sync_repo
    stick "${1:-exfat}"
    ssh_vm sh -s <<'EOF'
set -e
cd ~/CLUD
mnt=$(findmnt -nro TARGET -S "$(lsblk -lnpo NAME,TRAN | awk '$2 == "usb" {print $1; exit}')1")
fail() { echo "FAIL: $*"; exit 1; }

echo "== Drive list"
./CLUD-Installer.sh --list | tee /tmp/list.txt
grep -q "$mnt" /tmp/list.txt || fail "the stick ($mnt) isn't in --list"

echo "== Fresh install to $mnt"
./CLUD-Installer.sh --target "$mnt" --yes
for f in CLUD.exe CLUD.cmd autorun.inf clud.ico clud-version.json app/claude.exe \
         git/bin/bash.exe py/python.exe config/CLAUDE.md mcp/windows-mcp.cmd mcp/windows-mcp.json \
         CLUD.sh CLUD.desktop mcp/clud_desktop.py linux-arm64/claude.exe linux-arm64/py/bin/python3.exe \
         linux-x64/claude.exe linux-x64/py/bin/python3.exe; do
    [ -e "$mnt/$f" ] || fail "$f missing on the stick"
done
[ -z "$(find "$mnt" -name '._*' | head -1)" ] || fail "AppleDouble ._ files on the stick"
[ ! -e "$mnt/.clud-new" ] && [ ! -e "$mnt/.clud-old" ] || fail "staging folders left behind"
echo "Stick contents: $(du -sh "$mnt" | cut -f1), $(find "$mnt" -type f | wc -l) files"
"$mnt/linux-arm64/claude.exe" --version || fail "the Linux ARM64 build doesn't run"

echo "== Update over the existing install (keeps config)"
echo "test-marker" > "$mnt/config/marker.txt"
echo "# edited" >> "$mnt/config/CLAUDE.md"
./CLUD-Installer.sh --target "$mnt" --yes
[ -f "$mnt/config/marker.txt" ] || fail "update wiped config/"
[ -f "$mnt/config/CLAUDE.md.bak" ] || fail "edited CLAUDE.md wasn't backed up"
./CLUD-Installer.sh --list | grep -q "$mnt" || fail "stick gone from --list after install"
echo "PASS"
EOF
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    destroy) down; rm -rf "$VM"; echo "VM deleted." ;;
    sync) sync_repo ;;
    stick) stick "${2:-exfat}" ;;
    ssh) shift; if [ $# -eq 0 ]; then ssh_vm -t; else ssh_vm "$@"; fi ;;
    tui) sync_repo; ssh_vm -t "cd CLUD && TERM=xterm-256color ./CLUD-Installer.sh" ;;
    test) up; e2e "${2:-exfat}" ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
