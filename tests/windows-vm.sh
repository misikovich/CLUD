#!/bin/sh
# Test a CLUD stick on Windows: boots Windows 11 in QEMU with the stick attached as a USB
# mass-storage device, so Windows sees a removable drive like a real stick. On an Apple
# Silicon Mac it's Windows 11 on Arm (x64 apps like claude.exe run under Windows' built-in
# emulation); on an x86-64 Linux PC it's x64 Windows 11 under KVM.
#
#   tests/windows-vm.sh up             create/boot the VM (first run: unattended install, ~20 min)
#   tests/windows-vm.sh stick [src]    rebuild the stick and plug it in: src is "install" (default,
#                                      runs the installer from this repo) or a folder to copy, e.g.
#                                      /Volumes/CLUD to test your real stick's contents
#   tests/windows-vm.sh unplug | plug  pull the stick out / put it back in
#   tests/windows-vm.sh view           open the VM's screen in Screen Sharing (password: clud)
#   tests/windows-vm.sh shot [file]    save a screenshot of the VM's screen (PNG)
#   tests/windows-vm.sh ssh [cmd] | down | destroy
#
# Mac: the Windows ISO has to be downloaded by hand (Microsoft blocks scripted downloads):
# https://www.microsoft.com/en-us/software-download/windows11arm64 -> "Windows 11
# (multi-edition ISO for Arm64)". It's picked up from ~/Downloads, or set WIN_ISO=/path.
# Needs: brew install qemu.
# Linux: the Windows 11 Enterprise evaluation ISO (90 days, ~5.4 GB) is downloaded by itself;
# or set WIN_ISO=/path. Needs access to /dev/kvm, sudo (to format the stick image), and:
# sudo apt install qemu-system-x86 qemu-utils ovmf genisoimage exfatprogs parted netcat-openbsd
# VM files live in .vm-win/ (gitignored).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="$ROOT/.vm-win"
PORT="${CLUD_WINVM_PORT:-2224}"   # tests/pi-vm.sh has 2223
VNC_DISPLAY="${CLUD_WINVM_VNC:-2}"           # 127.0.0.1:5902 (the Pi VM has :5901)
MEM="${CLUD_WINVM_MEM:-6144}"
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) HOST=mac ;;
    Linux-x86_64) HOST=linux ;;
    *) echo "Runs on an Apple Silicon Mac or an x86-64 Linux PC"; exit 1 ;;
esac
QEMU_SHARE="$(brew --prefix qemu 2>/dev/null || true)/share/qemu"
OVMF=/usr/share/OVMF
EVAL_ISO_URL="https://go.microsoft.com/fwlink/p/?LinkID=2289031&clcid=0x409&culture=en-us&country=US"
VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
OPENSSH_ARCH=ARM64; [ "$HOST" = linux ] && OPENSSH_ARCH=Win64
OPENSSH_URL="https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-$OPENSSH_ARCH-v10.0.0.0.msi"
STICK_SIZE=4g
STICK_LABEL=CLUDVM

ssh_vm() {
    ssh -q -i "$VM/id_ed25519" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 clud@127.0.0.1 "$@"
}

running() { [ -f "$VM/qemu.pid" ] && kill -0 "$(cat "$VM/qemu.pid")" 2>/dev/null; }

# Human monitor command; prints its output
mon() { printf '%s\n' "$*" | nc -U -w 1 "$VM/monitor.sock" 2>/dev/null | tr -d '\r' | grep -v '^QEMU\|^(qemu)' || true; }

find_iso() {
    if [ -n "${WIN_ISO:-}" ]; then echo "$WIN_ISO"; return; fi
    if [ "$HOST" = linux ]; then
        [ -f "$VM/dl/win11-eval.iso" ] || { echo "Downloading the Windows 11 Enterprise evaluation (~5.4 GB)..." >&2
            curl -fL --progress-bar -o "$VM/dl/win11-eval.iso.part" "$EVAL_ISO_URL" >&2
            mv "$VM/dl/win11-eval.iso.part" "$VM/dl/win11-eval.iso"; }
        echo "$VM/dl/win11-eval.iso"
        return
    fi
    ls -t "$HOME"/Downloads/Win*11*[Aa]rm64*.iso "$VM"/dl/Win*11*[Aa]rm64*.iso 2>/dev/null | head -1
}

create() {
    if [ "$HOST" = mac ]; then
        command -v qemu-system-aarch64 >/dev/null || { echo "Install QEMU first: brew install qemu"; exit 1; }
    else
        command -v qemu-system-x86_64 >/dev/null && [ -f "$OVMF/OVMF_CODE_4M.fd" ] \
            || { sed -n '/^# Linux:/,/^# VM files/p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
        [ -r /dev/kvm ] && [ -w /dev/kvm ] || { echo "No access to /dev/kvm: sudo usermod -aG kvm $USER, then log in again"; exit 1; }
    fi
    mkdir -p "$VM/dl"
    iso="$(find_iso)"
    [ -n "$iso" ] && [ -f "$iso" ] || { sed -n '/^# Mac:/,/^# Needs: brew/p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
    echo "Windows ISO: $iso"
    # x64 Windows has drivers for the emulated disk, network card and display built in
    if [ "$HOST" = mac ] && [ ! -f "$VM/dl/virtio-win.iso" ]; then
        echo "Downloading virtio drivers..."
        curl -fL --progress-bar -o "$VM/dl/virtio-win.iso.part" "$VIRTIO_URL"
        mv "$VM/dl/virtio-win.iso.part" "$VM/dl/virtio-win.iso"
    fi
    [ -f "$VM/dl/openssh-$OPENSSH_ARCH.msi" ] || { echo "Downloading OpenSSH..."; curl -fL --progress-bar -o "$VM/dl/openssh-$OPENSSH_ARCH.msi" "$OPENSSH_URL"; }
    [ -f "$VM/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C clud-winvm -f "$VM/id_ed25519"

    # Setup media: autounattend.xml (Windows Setup finds it on any removable drive), the Arm64
    # virtio drivers (network, storage, input, display) and a first-logon script
    seed="$VM/seed"
    rm -rf "$seed" && mkdir -p "$seed/drivers"
    if [ "$HOST" = mac ]; then
        m="$(hdiutil attach -nobrowse -readonly "$VM/dl/virtio-win.iso" 2>/dev/null | tail -1 | awk -F'\t' '{print $NF}')"
        for d in NetKVM viostor vioscsi vioinput viogpudo Balloon vioserial viorng; do
            [ -d "$m/$d/w11/ARM64" ] && cp -R "$m/$d/w11/ARM64" "$seed/drivers/$d"
        done
        hdiutil detach -quiet "$m"
    fi
    cp "$VM/dl/openssh-$OPENSSH_ARCH.msi" "$seed/openssh.msi"
    cp "$VM/id_ed25519.pub" "$seed/authorized_keys"
    write_unattend "$seed/autounattend.xml"
    write_firstlogon "$seed/clud-setup.ps1"
    rm -f "$VM/seed.iso"
    if [ "$HOST" = mac ]; then
        hdiutil makehybrid -quiet -iso -joliet -default-volume-name CLUDSETUP -o "$VM/seed.iso" "$seed"
    else
        genisoimage -quiet -J -r -V CLUDSETUP -o "$VM/seed.iso" "$seed"
    fi

    qemu-img create -q -f qcow2 "$VM/disk.qcow2" 64G
    if [ "$HOST" = mac ]; then cp "$QEMU_SHARE/edk2-arm-vars.fd" "$VM/vars.fd"; else cp "$OVMF/OVMF_VARS_4M.fd" "$VM/vars.fd"; fi
    [ -f "$VM/stick.img" ] || truncate -s "$STICK_SIZE" "$VM/stick.img"
    ln -sf "$iso" "$VM/install.iso"
    touch "$VM/.installing"
}

boot() {
    stick_args=""
    [ -f "$VM/unplugged" ] || stick_args="-drive if=none,id=stick,format=raw,file=$VM/stick.img -device usb-storage,id=stickdev,drive=stick,removable=on"
    setup_args=""
    [ -f "$VM/.installing" ] && setup_args="-drive if=none,id=cd0,media=cdrom,readonly=on,file=$VM/install.iso -device usb-storage,drive=cd0,bootindex=1
        -drive if=none,id=cd1,media=cdrom,readonly=on,file=$VM/seed.iso -device usb-storage,drive=cd1"
    if [ "$HOST" = mac ]; then
        machine="qemu-system-aarch64 -machine virt -accel hvf -cpu host
            -drive if=pflash,format=raw,readonly=on,file=$QEMU_SHARE/edk2-aarch64-code.fd
            -device ramfb -device virtio-net-pci,netdev=net0"
    else
        machine="qemu-system-x86_64 -machine q35 -accel kvm -cpu host
            -drive if=pflash,format=raw,readonly=on,file=$OVMF/OVMF_CODE_4M.fd
            -vga std -device e1000e,netdev=net0"
    fi
    # shellcheck disable=SC2086
    $machine -smp 4 -m "$MEM" \
        -drive if=pflash,format=raw,file="$VM/vars.fd" \
        -drive if=none,id=sys,format=qcow2,file="$VM/disk.qcow2" -device nvme,drive=sys,serial=clud,bootindex=0 \
        -device qemu-xhci,id=xhci -device usb-kbd -device usb-tablet \
        -netdev user,id=net0,hostfwd=tcp:127.0.0.1:"$PORT"-:22 \
        -rtc base=localtime \
        $setup_args $stick_args \
        -vnc 127.0.0.1:"$VNC_DISPLAY",password=on -monitor unix:"$VM/monitor.sock",server,nowait \
        -display none -daemonize -pidfile "$VM/qemu.pid"
    sleep 1
    mon "set_password vnc clud" >/dev/null
}

up() {
    running && { echo "VM already running (view: tests/windows-vm.sh view, ssh: tests/windows-vm.sh ssh)"; return; }
    [ -f "$VM/disk.qcow2" ] || create
    echo "Booting VM... (watch it: tests/windows-vm.sh view, password clud)"
    boot
    if [ -f "$VM/.installing" ]; then
        # The Windows ISO waits for "Press any key to boot from CD". Keep pressing Enter while the
        # screen is still dark (firmware, that prompt, boot logo), but stop once Setup shows up:
        # there Enter would hit its Cancel button
        i=0
        while [ $i -lt 90 ] && screen_dark; do mon "sendkey ret" >/dev/null; sleep 1; i=$((i + 1)); done
        echo "Installing Windows unattended. This takes a while."
        limit=900                # x 4s = 1 hour
    else
        limit=150
    fi
    printf "Waiting for SSH"
    i=0
    until ssh_vm "exit 0" 2>/dev/null; do
        i=$((i + 1)); [ $i -gt $limit ] && { echo; echo "VM didn't come up. Look at it: tests/windows-vm.sh view"; exit 1; }
        printf "."; sleep 4
    done
    echo
    if [ -f "$VM/.installing" ]; then
        printf "Waiting for first-logon setup"
        until ssh_vm "if (Test-Path C:\\clud-ready.txt) { exit 0 } else { exit 1 }" 2>/dev/null; do printf "."; sleep 5; done
        echo
        # Setup media are no longer needed; boot without them from now on
        rm -f "$VM/.installing"
        echo "Windows installed. Restarting without the setup media..."
        down >/dev/null
        boot
        until ssh_vm "exit 0" 2>/dev/null; do sleep 3; done
    fi
    echo "VM ready. Screen: tests/windows-vm.sh view (password clud). Login: clud / clud."
}

down() {
    running || { echo "VM not running"; return; }
    ssh_vm "Stop-Computer -Force" 2>/dev/null || mon "system_powerdown" >/dev/null
    i=0
    while running && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
    running && kill "$(cat "$VM/qemu.pid")"
    rm -f "$VM/qemu.pid" "$VM/monitor.sock"
    echo "VM stopped."
}

unplug() {
    touch "$VM/unplugged"
    running || return 0
    ssh_vm "\$v = Get-Volume -FileSystemLabel $STICK_LABEL -ErrorAction SilentlyContinue; if (\$v) { (New-Object -ComObject Shell.Application).Namespace(17).ParseName(\"\$(\$v.DriveLetter):\").InvokeVerb('Eject') }" 2>/dev/null || true
    sleep 2
    mon "device_del stickdev" >/dev/null; sleep 1; mon "drive_del stick" >/dev/null
    echo "Stick unplugged."
}

plug() {
    rm -f "$VM/unplugged"
    running || { echo "Stick will be plugged in at next boot."; return; }
    mon "drive_add 0 if=none,id=stick,format=raw,file=$VM/stick.img" >/dev/null
    out="$(mon "device_add usb-storage,id=stickdev,drive=stick,removable=on,bus=xhci.0")"
    [ -z "$out" ] || { echo "$out"; exit 1; }
    echo "Stick plugged in."
}

# Format stick.img like a store-bought stick (MBR, exFAT) and fill it, then plug it in
stick() {
    src="${1:-install}"
    [ -f "$VM/stick.img" ] || { mkdir -p "$VM"; truncate -s "$STICK_SIZE" "$VM/stick.img"; }
    unplug >/dev/null
    if [ "$HOST" = mac ]; then
        dev="$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$VM/stick.img" 2>/dev/null | head -1 | awk '{print $1}')"
        trap 'hdiutil detach -quiet -force "$dev" 2>/dev/null || true' EXIT
        diskutil quiet eraseDisk ExFAT "$STICK_LABEL" MBR "$dev"
        mnt="$(diskutil info "${dev}s1" | awk -F': *' '/Mount Point/ {print $2}')"
    else
        parted -s "$VM/stick.img" mklabel msdos mkpart primary 1MiB 100%
        dev="$(sudo losetup -fP --show "$VM/stick.img")"
        mnt="$(mktemp -d)"
        trap 'sudo umount "$mnt" 2>/dev/null || true; sudo losetup -d "$dev" 2>/dev/null || true' EXIT
        sudo mkfs.exfat -q -L "$STICK_LABEL" "${dev}p1" >/dev/null
        sudo mount -o "uid=$(id -u),gid=$(id -g)" "${dev}p1" "$mnt"
    fi
    [ -d "$mnt" ] || { echo "Couldn't mount the stick image"; exit 1; }
    if [ "$src" = install ]; then
        "$ROOT/CLUD-Installer.sh" --target "$mnt" --yes --no-rename
    else
        [ -f "$src/CLUD.cmd" ] || { echo "$src doesn't look like a CLUD stick (no CLUD.cmd)"; exit 1; }
        echo "Copying CLUD from $src..."
        for f in CLUD.exe CLUD.cmd README.txt autorun.inf clud-version.json clud.ico app config mcp py work; do
            [ -e "$src/$f" ] && rsync -a --exclude '._*' --exclude .DS_Store "$src/$f" "$mnt/"
        done
    fi
    echo "Stick: $(du -sh "$mnt" | cut -f1)"
    if [ "$HOST" = mac ]; then
        dot_clean -m "$mnt" 2>/dev/null || true
        find "$mnt" -name '._*' -delete 2>/dev/null || true
        rm -rf "$mnt/.fseventsd" "$mnt/.Spotlight-V100" "$mnt/.Trashes"
        hdiutil detach -quiet "$dev"
    else
        sudo umount "$mnt" && sudo losetup -d "$dev" && rmdir "$mnt"
    fi
    trap - EXIT
    plug
}

shot() {
    out="${1:-$VM/screen.png}"
    case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
    rm -f "$out"
    mon "screendump $out -f png" >/dev/null
    i=0; while [ ! -s "$out" ] && [ $i -lt 10 ]; do sleep 0.3; i=$((i + 1)); done
    echo "$out"
}

# True while the VM's screen is almost all black (text-mode prompts, boot logo)
screen_dark() {
    rm -f "$VM/screen.ppm"
    mon "screendump $VM/screen.ppm" >/dev/null
    j=0; while [ ! -s "$VM/screen.ppm" ] && [ $j -lt 10 ]; do sleep 0.2; j=$((j + 1)); done
    python3 -I - "$VM/screen.ppm" <<'EOF'
import sys
data = open(sys.argv[1], "rb").read()
# P6 header: magic, width, height, maxval, then RGB bytes
parts = data.split(maxsplit=4)
pixels = parts[4][::7]   # a stride coprime to 3 samples all of R, G and B
sys.exit(0 if sum(b > 40 for b in pixels) < len(pixels) * 0.05 else 1)
EOF
}

write_unattend() {
    arch=arm64; [ "$HOST" = linux ] && arch=amd64
    if [ "$HOST" = mac ]; then
        image='<Key>/IMAGE/NAME</Key><Value>Windows 11 Pro</Value>'
        key='<ProductKey><Key>VK7JG-NPHTM-C97JM-9MPGT-3V66T</Key><WillShowUI>Never</WillShowUI></ProductKey>'
    else
        image='<Key>/IMAGE/INDEX</Key><Value>1</Value>'   # the evaluation ISO has one edition, no key
        key=''
    fi
    a='processorArchitecture="'$arch'" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"'
    cat > "$1" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" $a>
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" $a>
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"><Path>D:\\drivers</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2"><Path>E:\\drivers</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3"><Path>F:\\drivers</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="4"><Path>G:\\drivers</Path></PathAndCredentials>
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-Setup" $a>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>5</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassStorageCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom><MetaData wcm:action="add">$image</MetaData></InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        $key
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $a>
      <ComputerName>CLUD-WINVM</ComputerName>
    </component>
    <component name="Microsoft-Windows-Deployment" $a>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" $a>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" $a>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>clud</Name><Group>Administrators</Group>
            <Password><Value>clud</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled><Username>clud</Username><LogonCount>9999999</LogonCount>
        <Password><Value>clud</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-PSDrive -PSProvider FileSystem | ForEach-Object { \$s = Join-Path \$_.Root 'clud-setup.ps1'; if (Test-Path \$s) { &amp; \$s } }"</CommandLine>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
EOF
}

write_firstlogon() {
    cat > "$1" <<'EOF'
# Runs once at first logon from the CLUDSETUP media: drivers, SSH, no sleep
$ErrorActionPreference = 'Continue'
$media = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Transcript C:\clud-setup.log
pnputil /add-driver "$media\drivers\*.inf" /subdirs /install

Start-Process msiexec -Wait -ArgumentList '/i', "$media\openssh.msi", '/qn'
$keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
Copy-Item "$media\authorized_keys" $keys -Force
icacls $keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -PropertyType String -Force `
    -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
Set-Service sshd -StartupType Automatic
Restart-Service sshd
if (-not (Get-NetFirewallRule -Name clud-sshd -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name clud-sshd -DisplayName 'OpenSSH (CLUD test VM)' -Protocol TCP -LocalPort 22 -Action Allow
}

# A test PC shouldn't sleep, lock or nag
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
reg add 'HKCU\Control Panel\Desktop' /v ScreenSaveActive /t REG_SZ /d 0 /f
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization' /v NoLockScreen /t REG_DWORD /d 1 /f

Stop-Transcript
Set-Content C:\clud-ready.txt (Get-Date)
EOF
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    destroy) running && kill "$(cat "$VM/qemu.pid")"; rm -rf "$VM/disk.qcow2" "$VM/vars.fd" "$VM/seed" "$VM/seed.iso" \
        "$VM/stick.img" "$VM/qemu.pid" "$VM/monitor.sock" "$VM/.installing" "$VM/unplugged"; echo "VM deleted (downloads in .vm-win/dl kept)." ;;
    stick) stick "${2:-install}" ;;
    plug) plug ;;
    unplug) unplug ;;
    view) if [ "$HOST" = mac ]; then open "vnc://127.0.0.1:$((5900 + VNC_DISPLAY))"
          else echo "VNC: 127.0.0.1:$((5900 + VNC_DISPLAY)), password clud"; fi ;;
    shot) shot "${2:-}" ;;
    ssh) shift; if [ $# -eq 0 ]; then ssh_vm -t; else ssh_vm "$@"; fi ;;
    *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
