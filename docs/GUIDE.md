# Lumia 1520 Android installer: user guide

[Italiano](GUIDA.md) · [README](../README.md)

This guide takes you from a Lumia 1520 running Windows 10 Mobile (or Windows Phone 8.1) to the
unofficial **LineageOS 18.1** (Android 11) port, and shows how to reinstall or update it later.

> **Lumia 930 (martini, RM-1045):** the installer already knows it, but only the backup, the firmware
> extraction and the boot chain are available, with an lk1st that has never been booted. There is no
> ROM or TWRP for the 930 yet, so Full, Clean and Update stay locked. Pick the phone on the install page
> (or `--device martini`); the installer checks the model on the phone before writing anything. The rest
> of this guide describes the Lumia 1520.

> **Unofficial and experimental.** The **Full** install **erases Windows 10 Mobile and everything on
> the phone** (apps, photos, files). Going back to Windows needs a Windows FFU image flashed with
> WPinternals or Windows Device Recovery Tool. Make the backups the installer offers and keep them.

## Before you start: unlock the bootloader (Windows only)

**WPinternals is a separate tool**, by René Lergner
([github.com/ReneLergner/WPinternals](https://github.com/ReneLergner/WPinternals)). It is **not part of
this installer** and it **runs only on Windows** (Windows 10 or 11). You need it only for the **Full**
install, and before the installer touches the phone:

1. **Unlock the bootloader** with WPinternals on a Windows PC. This is always the first step, done on
   Windows, even if you will run the installer on Linux. Without the unlock BootShim cannot start and
   the installer stops ("IS_UNLOCKED missing").
2. **Switch to mass storage mode** with WPinternals (*Switch to Mass Storage Mode*). Mass storage is
   entered only from WPinternals, so only from Windows. The phone shows a USB symbol and its partitions
   appear on the PC.

| You run the installer on | Workflow |
|---|---|
| Windows | WPinternals: unlock, then mass storage; then start the installer on the same PC. |
| Linux | On the Windows PC, WPinternals: unlock, then mass storage. Move the USB cable to the Linux PC (the phone stays in mass storage until it restarts) and start the installer there. |

If the phone restarts before the boot chain step is done (for example you stopped during the backup
and restarted it), it leaves mass storage: switch it again from WPinternals on Windows and move the
cable back. From the lk1st check onwards the installer only uses fastboot and adb, on Windows or Linux.
**Clean** and **Update** never need WPinternals. WPinternals is needed again only to go back to Windows
(flashing an FFU from flash mode, [section 13](#13-going-back-to-windows)).

## Contents

1. [Which mode?](#1-which-mode)
2. [What you need](#2-what-you-need)
3. [Download](#3-download)
4. [Prepare the PC](#4-prepare-the-pc)
5. [Start the installer](#5-start-the-installer)
6. [Full install, step by step](#6-full-install-step-by-step)
7. [Clean install](#7-clean-install)
8. [Update](#8-update)
9. [Getting the phone to lk1st fastboot](#9-getting-the-phone-to-lk1st-fastboot)
10. [After the install](#10-after-the-install)
11. [Known limitations](#11-known-limitations)
12. [Troubleshooting and boot loops](#12-troubleshooting-and-boot-loops)
13. [Going back to Windows](#13-going-back-to-windows)
14. [Advanced tools](#14-advanced-tools)
15. [Files and options](#15-files-and-options)

## 1. Which mode?

| Your phone now | Mode | Your data |
|---|---|---|
| Windows 10 Mobile or 8.1, never had Android | **Full** | Windows and everything on the phone is erased |
| Has this Android port, you want a fresh start (or it does not start any more) | **Clean** | all Android data erased, internal storage included |
| Has this Android port, you want a newer build | **Update** | kept |

## 2. What you need

- A **Nokia Lumia 1520** (RM-937, RM-938, RM-939 or RM-940).
- For **Full**: a **Windows 10/11 PC** for the unlock with
  [WPinternals](https://github.com/ReneLergner/WPinternals). The installer itself runs on that PC or
  on a **Linux** PC.
- A good USB cable, plugged directly into the PC (no hub). A bad one shows up as "two different
  reads" or as the phone disappearing from adb.
- The phone charged **above 50%**: in TWRP the PC's USB port charges it slowly.
- Free space on the PC: about 1 GB for the basic backup, plus the compressed copy of the whole phone
  if you keep the full image (strongly recommended before Full: a few GB to tens of GB), plus 600 MB
  for the ROM.
- Time: Full 45–120 minutes (most of it for the full backup), Clean and Update 10–15 minutes.

## 3. Download

| What | From | Put it in |
|---|---|---|
| The installer: `bandit-installer-<version>-windows.zip` (with its own Python) or `bandit-installer-<version>-linux.tar.gz` | [installer releases](https://github.com/lumia-msm8974/bandit-installer/releases) | a folder with enough free space |
| The ROM: `lineage-18.1-YYYYMMDD-UNOFFICIAL-bandit.zip` **and** `SHA256SUMS` | [android_device_nokia_bandit releases](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases) | the `rom` folder of the installer |
| TWRP: `twrp-3.7.0_9-1-bandit.img` (optional: the installer downloads it when missing) | [release `twrp-bandit-20261102`](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/tag/twrp-bandit-20261102) | the `twrp` folder of the installer |

On Windows extract the whole installer zip first (right click → *Extract all*); do not start it from
inside the zip. Keep **one** ROM zip in `rom` (with more than one the installer asks which).

The installer checks the ROM zip against `SHA256SUMS` and the TWRP image against its own
`checksums.sha256` **before touching the phone**: a damaged download stops it with a clear message.

## 4. Prepare the PC

### Windows

1. Install the [Google USB driver](https://developer.android.com/studio/run/win-usb), so that Windows
   recognises the phone in fastboot and adb. In Device Manager the phone must show as *Android
   Bootloader Interface* in fastboot and *Android ADB Interface* in TWRP; if it shows with a yellow mark,
   right click → *Update driver* → *Browse* → the folder of the Google USB driver.
2. Have **Edge** or **Chrome** (Edge is part of Windows): the installer opens in an app window.
3. `fastboot` and `adb` are not needed in advance: the installer offers to download Google's Platform
   Tools into its `tools\` folder.

### Linux

Install Python 3 (3.8 or newer), the hivex Python binding (to edit the BCD), util-linux and NTFS
support (for the firmware step):

```bash
sudo pacman -S python hivex util-linux ntfs-3g android-tools              # Arch
sudo apt install python3 python3-hivex util-linux ntfs-3g adb fastboot      # Debian, Ubuntu
sudo dnf install python3 python3-hivex util-linux ntfs-3g android-tools   # Fedora
```

For the app window you need Chromium, Chrome, Brave or Edge; without them the default browser opens
a tab, and `--text` gives a terminal interface. If `adb`/`fastboot` are missing the installer can
download Google's copy.

## 5. Start the installer

| System | How |
|---|---|
| Windows | double-click `installer.bat` and accept the administrator prompt |
| Linux | `./installer.sh` in a terminal; it asks for your password through `sudo` |

Administrator (root) rights are needed to read and write the phone's disk in mass storage mode (Full).
Clean and Update only use adb and fastboot: `--no-elevate` is enough for them.

The window shows:

- on the left, the **phone status** (not connected, mass storage, fastboot, recovery…); click it for a
  diagnostic;
- **Install**, with the three modes, the progress of the full install and the requirements (adb,
  fastboot, TWRP, ROM);
- **Advanced tools**, with each step on its own;
- **Settings**, for the language (English or Italian), the accent colour and the data folder.

The window talks to the installer through a local address (`127.0.0.1`) shown in the terminal. If no
window opens, copy that address into a browser. Closing the window does not stop the installer: use
**Settings → Quit**, or Ctrl+C in the terminal. In the terminal interface (`--text`) the installer asks
the same questions as text.

Whenever the installer asks you to **type a word** (`INSTALL`, `ERASE WINDOWS`, `ERASE`, `UPDATE`), it
is about to write to the phone. Anything else stops it with nothing written.

## 6. Full install, step by step

**Before you start:** Windows and everything on the phone will be erased. Copy your photos and files
off the phone first.

### 6.1 Preparation: unlock the phone and switch it to mass storage

This is the **preparation**, done with **WPinternals on Windows**, outside this installer: every
mode starts from a phone that is already unlocked (see
[Before you start](#before-you-start-unlock-the-bootloader-windows-only)).

1. Follow the WPinternals instructions to **unlock the bootloader**.
2. In WPinternals choose **Switch to Mass Storage Mode**. The phone shows a USB symbol and its
   partitions appear on the PC.
3. Installer on Linux: now move the USB cable to the Linux PC. The phone stays in mass storage mode
   until it restarts.

The installer never runs WPinternals. The Full mode checks the unlock (the `IS_UNLOCKED` partition
WPinternals adds) before anything else and stops at once, without writing, if it is missing.

### 6.2 Start

Put the ROM zip and `SHA256SUMS` in `rom`, start the installer and click **Start** on **Full install**
(or run `--mode full`). The installer:

1. offers Platform Tools if `adb` or `fastboot` are missing;
2. checks the TWRP image (it offers to download it if it is missing) and the ROM zip.

### 6.3 Backup

It finds the phone's disk by its partition table and asks whether to make the **full compressed
image** too. **Say yes**: it is the only exact copy of Windows. Then it saves the partition table,
every partition under 512 MiB (read **twice** and compared), the full image and a `SHA256SUMS` list.
This takes 20–60 minutes with the full image.

The backup contains personal and modem data (DPP, MODEM_FS*): keep it, and never publish it.

### 6.4 Firmware from Windows

It copies from the Windows partitions the firmware, the audio and Wi-Fi calibration, the ACPI tables
and the registry, for reference and development. These are Nokia/Microsoft files: they stay on your PC.
(The ROM brings its own firmware.)

### 6.5 Boot chain

1. It shows the current boot configuration (BCD) and the **boot options**: in the Full mode Android is
   the default and only system, because Windows is about to be erased. You may set a **menu timeout**
   (0 = no menu).
2. It backs up EFIESP (files and raw image), writes the new BCD next to the backup, reads it back and
   checks it.
3. You type **INSTALL**; it writes BootShim, lk1st and the BCD to EFIESP and compares each file.

**Volume up at power-on always opens flash mode**: that is the way back with WPinternals, and the
installer refuses any configuration that would change it.

### 6.6 Restart and lk1st check

Restart the phone: leave mass storage mode in WPinternals, or hold **power + volume down** for about
10 seconds until it vibrates. The BootShim text appears, then the screen stays still: lk1st does not
drive the display, so a still screen is expected. The installer waits for fastboot and saves
`fastboot getvar all` and the lk1st log.

### 6.7 Android partitions (phase 2)

1. TWRP is started **in RAM** with `fastboot boot` (it is not written to the phone). Wait for the TWRP
   screen; the installer waits for adb.
2. It checks that adb is root, that the tools it needs are there and that nothing of the phone's
   storage is mounted.
3. It computes the new partition table and shows it: MMOS, MainOS and Data go away; aboot, boot,
   recovery, misc, modem, cache, persist, persistent, fsc, fsg, metadata, ssd, modemst1, modemst2,
   vendor, system and userdata take their place, right after EFIESP. Every other partition stays as it
   is.
4. It checks that lk1st is on EFIESP and is the default boot entry.
5. It copies the **11 provisioning partitions** (DPP, MODEM_FS1, MODEM_FS2, MODEM_FSC, MODEM_FSG, SSD,
   DDR, DBI, UEFI_BS_NV, UEFI_RT_NV, BACKUP_BS_NV) to `work/backups/phase2-<date>/provisioned/` and
   compares each one with the phone.
6. It shows a summary and asks you to type **ERASE WINDOWS**. This is the point of no return.
7. It writes the new table (backup copy at the end of the disk first, then the primary), reads it back
   and checks it with an independent parser.
8. It formats the new partitions, copies lk1st into `aboot` (it stays on EFIESP too), copies the modem
   data into modemst1/modemst2/fsc/fsg/ssd and writes the Wi-Fi and Bluetooth MAC addresses and the
   Wi-Fi calibration from DPP into `persist`.

If the kernel does not see the new partitions at once, the installer restarts the phone into lk1st and
TWRP by itself.

### 6.8 ROM

1. TWRP is restarted (so that it sees the new partitions).
2. `format data`, `wipe cache`, `wipe dalvik`.
3. **adb sideload** of the ROM. The transfer stops around **47%** with `Total xfer: 1.00x`: normal.
   TWRP installs while it receives, a few minutes.
4. The installer reads the TWRP log (it must say `script succeeded: result was [1.000000]`), checks
   that the boot partition matches `boot.img` of the zip, saves the log in `work/logs/rom-<date>/` and
   restarts the phone.

The **first start takes several minutes**: the LineageOS boot animation stays on screen for a while.
Leave the phone alone until the setup wizard appears.

### 6.9 If it stops

Fix the cause shown in red and run **Full** again: it continues where it stopped. Steps already done
are offered as *Skip*. If the new partition table was already written when it stopped, the next run
formats and provisions again without asking to erase Windows a second time.

## 7. Clean install

For a phone that already has lk1st and the Android partitions (for example after a Full install).
**All Android data is erased**, internal storage included.

1. Put the ROM zip and `SHA256SUMS` in `rom`.
2. Connect the phone: in Android with **USB debugging** on (Settings → System → Developer options;
   allow the PC when the phone asks), or already in lk1st fastboot ([section 9](#9-getting-the-phone-to-lk1st-fastboot)).
3. Start **Clean install** (or `--mode clean`). The installer checks TWRP and the ROM, restarts the
   phone into lk1st, starts TWRP in RAM and checks that the Android partitions are there.
4. Type **ERASE**. TWRP formats data and wipes cache and Dalvik.
5. The ROM is sideloaded and checked, the phone restarts. The first start takes several minutes.

## 8. Update

For a phone that already runs this LineageOS 18.1 port. **Data is kept.**

1. Put the new ROM zip and its `SHA256SUMS` in `rom` (remove the old zip).
2. In Android enable **USB debugging** (Settings → System → Developer options), connect the phone and
   allow the PC when the phone asks.
3. Start **Update** (or `--mode update`). The installer checks TWRP and the ROM, restarts the phone
   into lk1st, starts TWRP in RAM and reads the installed system. If it is not LineageOS 18.1 for
   bandit it warns you: updating another system without formatting usually ends in a boot loop, so
   type **UPDATE** only if you know why; otherwise use Clean.
4. The ROM is sideloaded and checked, the phone restarts.

## 9. Getting the phone to lk1st fastboot

The installer needs lk1st in fastboot to start TWRP. In most cases it gets there by itself:

| Phone is… | What to do |
|---|---|
| in Android, USB debugging on | nothing: the installer runs `adb reboot bootloader` (about 12 s) |
| in TWRP | nothing: the installer restarts it into lk1st; by hand: *Reboot → Bootloader* |
| on with no Android installed yet (after the boot chain) | power it on: after the BootShim text lk1st stays in fastboot with a still screen |
| showing the Windows boot menu (if you set a menu timeout) | pick **Android (lk1st)** |
| in a boot loop | see [section 12](#12-troubleshooting-and-boot-loops) |

**Volume up held at power-on is not fastboot**: it opens the Windows Phone **flash mode** (a large
lightning bolt), used by WPinternals and Windows Device Recovery Tool. It is the way back when nothing
else works; the installer never changes it.

## 10. After the install

| You do | The phone starts |
|---|---|
| power on | Android, through UEFI → bootmgr → BootShim → lk1st |
| power on with volume up | flash mode, for WPinternals |
| `adb reboot bootloader` | lk1st in fastboot |

To **update later**, see [section 8](#8-update). Keep `work/` (backups, provisioning copy, logs) in a
safe place.

## 11. Known limitations

- `adb sideload` stops around **47%** with `Total xfer: 1.00x`: normal for this block-based ROM.
- The `twrp` command line tool prints **`Segmentation fault`** after each command: the command has
  run; the installer reads the result from the TWRP log.
- **Do not open the phone in a file manager or MTP tool** on the PC while the installer runs: an MTP
  client makes adb lose the phone in TWRP. Copy files with `adb push`/`adb pull`, or once Android runs.
- TWRP is only started in RAM; it is not installed on the phone.
- The first start takes several minutes, more after Clean or Full.
- Phase 2 with TWRP has been tested only on a simulated phone; the same sequence ran on a real RM-937
  with the ROM's recovery. If TWRP lacks a tool the installer stops before writing anything; the
  advanced option `--phase2-recovery <recovery.img>` then uses another recovery (it needs adb as root).
- The Windows-specific parts of the installer (BCD with `bcdedit`, disk and USB detection) have not
  been run on Windows yet.

## 12. Troubleshooting and boot loops

| Problem | What to do |
|---|---|
| "The ROM zip does not match its checksum" | Download the zip and `SHA256SUMS` again, from the same release. |
| "is not the expected file" (TWRP) | Delete the image in `twrp` and let the installer download it again. |
| "No SHA-256 found" | Put the `SHA256SUMS` from the ROM's release page next to the zip. |
| "More than one device" | Disconnect other phones (also adb over Wi-Fi), or use `--serial <serial>`. |
| fastboot or adb does not appear (Windows) | Check the Google USB driver in Device Manager. |
| TWRP shows on the phone but the installer keeps waiting | Close any MTP or file manager window on the PC; unplug and plug the cable. |
| "TWRP reports that the ROM installation failed" | Look at `work/logs/rom-<date>/recovery.log`; run the installer again. |
| Phone ends in **Qualcomm 9008** | Hold power + volume down for 10 s, then volume up at power-on (flash mode) and ask for help with the logs. |
| **Two different reads** during the backup | Unstable cable or port: change them and run the backup again. |
| **Partitions still mounted** (mass storage) | Close Explorer/file manager windows showing the phone's drives. |
| Linux: **hivex** missing | Install the package from [section 4](#linux). |

### Boot loop

1. If `adb devices` shows the phone while it loops (USB debugging on and this PC authorised), run
   **Update** (keeps data) or **Clean**: the installer stops the loop with `adb reboot bootloader`.
2. If adb never appears: put the phone in flash mode (volume up at power-on), switch it to mass storage
   with WPinternals and, on Linux, spoil the boot image header so that lk1st stays in fastboot (not yet
   tried on the phone):
   ```bash
   lsblk -o NAME,PARTLABEL          # find the partition labelled "boot", e.g. sdb27
   sudo dd if=/dev/zero of=/dev/sdb27 bs=4096 count=1 conv=fsync   # check the name twice!
   ```
   Restart the phone (power + volume down for 10 s): lk1st finds no valid boot image and stays in
   fastboot. Run **Update** or **Clean**.
3. Last resort: [go back to Windows](#13-going-back-to-windows) and start again with Full.

The logs of each session are in `work/logs/`: attach them to any report, after checking they contain
no data you want to keep private.

## 13. Going back to Windows

After a Full install Windows is gone: you need a **Windows 10 Mobile or 8.1 FFU** for your model.

1. Power the phone on holding **volume up**: flash mode.
2. Flash the FFU with **WPinternals** (its FFU flashing function) or **Windows Device Recovery Tool**.

Before the Full install (only the boot chain installed), **Restore EFIESP** in the advanced tools is
enough: it puts back the original BCD and files.

## 14. Advanced tools

| Tool | Phone mode | Writes to the phone |
|---|---|---|
| Backup, Firmware | mass storage | no |
| Boot chain | mass storage | EFIESP only, after a backup |
| Check lk1st | restart → fastboot | no |
| Android partitions | fastboot → TWRP | yes: phase 2 alone (erases Windows), no ROM |
| Boot in RAM | fastboot | no: `fastboot boot` of K1 or a `boot.img` |
| Previous boot log | fastboot | no: ram console and lk1st log after a hang |
| Write partition | fastboot | yes, Android partitions only (`boot`, `recovery`, `system`…) |
| Restore EFIESP | mass storage | yes, from a backup |
| Diagnostics | any | no |
| Platform Tools | — | no |

## 15. Files and options

| Folder | Contents |
|---|---|
| `rom/` | the ROM zip and its `SHA256SUMS` |
| `twrp/` | the TWRP image |
| `work/backups/` | phone, EFIESP and phase 2 backups (provisioning partitions, GPT before and after) |
| `work/private/` | firmware extracted from Windows |
| `work/logs/` | session logs, lk1st logs, TWRP `recovery.log` of every ROM install |
| `work/state.json` | progress, language and colour |

Backups and firmware contain personal data and Nokia/Microsoft files: **do not publish them**.

| Option | Meaning |
|---|---|
| `--mode full\|clean\|update` | start that mode without the menu |
| `--rom <zip>`, `--rom-sha256 <hash>` | ROM zip and its expected SHA-256 (default: `rom/` and `SHA256SUMS`) |
| `--twrp <img>` | TWRP image; it must be listed in `checksums.sha256` |
| `--phase2-recovery <img>` | advanced: another recovery for phase 2 |
| `--serial <serial>` | only this phone (also `ANDROID_SERIAL`) |
| `--adb <path>`, `--fastboot <path>` | use these executables |
| `--lang en\|it` | interface language (default English, or the last one chosen) |
| `--text` | terminal interface, for SSH or machines without a browser |
| `--text --action <task>` | run one task and exit (`backup`, `chain`, `verify`, `phase2`, `boot`, `diag`…) |
| `--work <folder>` | data folder (default `work/` next to the installer) |
| `--no-browser` | only print the address of the window |
| `--no-elevate` | do not ask for administrator rights (enough for Clean and Update) |

Examples: `installer.bat --mode update`, `./installer.sh --text --mode clean --lang it`.
