# Lumia MSM8974 Android installer (bandit, martini)

An installer for the unofficial **LineageOS 18.1 (Android 11)** ports to the Nokia Lumias built on the
Qualcomm MSM8974, for **Windows and Linux**, with a desktop app window in the style of Windows 10 and a
terminal mode (`--text`). The interface is in English, with Italian available in the settings or with
`--lang it`.

| Phone | Codename | Models | State |
|---|---|---|---|
| Nokia Lumia 1520 | `bandit` | RM-937, RM-938, RM-939, RM-940 | all modes; ROM and TWRP released |
| Nokia Lumia 930 | `martini` | RM-1045 (RM-1087 unconfirmed) | **test builds**: every step has run on a real RM-1045 (backup, firmware, boot chain, phase 2 from the advanced tools, Clean, Update); Full locked until it has run end to end on a 930 |

The Lumia Icon / 929 (RM-927) shares the `martini` codename but has its own Windows firmware set:
it is refused. Pick the phone on the install page (or `--device bandit|martini`); the installer also
reads the model from the phone itself (`Nokia/product.dat` in DPP) before it writes anything and stops
if it does not match.

Android starts from the phone's own Windows Phone boot manager:

```text
UEFI Nokia → bootmgr → BCD entry "Android (lk1st)" → BootShim → lk1st → Android
```

The installer sets up that chain, replaces Windows with the Android partitions and installs the ROM
from TWRP, which it only starts in RAM. **Unofficial and experimental**: you do this at your own risk.

**Download** the package for Windows or Linux from the
[releases page](https://github.com/lumia-msm8974/bandit-installer/releases) and follow the
**[user guide](docs/GUIDE.md)** ([guida in italiano](docs/GUIDA.md)).

![Install page](docs/screenshots/01-install.png)

| | |
|---|---|
| ![Advanced tools](docs/screenshots/02-advanced-tools.png) | ![Diagnostics](docs/screenshots/03-diagnostics.png) |
| ![Boot in RAM](docs/screenshots/04-boot-in-ram.png) | |

<sub>Screenshots taken on Linux with the phone disconnected.</sub>

## Before you start: unlock the bootloader (Windows only)

**WPinternals is a separate tool**, by René Lergner
([github.com/ReneLergner/WPinternals](https://github.com/ReneLergner/WPinternals)). It is **not part of
this installer** and it **runs only on Windows**. The Full install needs it twice, before the installer
touches the phone:

1. **Unlock the bootloader** with WPinternals, on a Windows PC. This is always done first, on Windows,
   even if you then run the installer on Linux.
2. **Switch to mass storage mode** with WPinternals (*Switch to Mass Storage Mode*). Mass storage is
   entered only from WPinternals, so only from Windows.

On **Linux**: unlock and switch to mass storage on the Windows PC, then move the USB cable to the Linux
PC and start the installer there. The phone stays in mass storage until it restarts. If it restarts
before the boot chain step is done (for example the backup stopped and you restarted the phone), switch
it to mass storage again from WPinternals on Windows and move the cable back.

After the boot chain is installed the installer works only with fastboot and adb (on Windows or Linux):
Clean and Update never need WPinternals. WPinternals is needed again only to go back to Windows
(flashing an FFU from flash mode).

## Modes

As in the [Lumia 950 XL installer](https://github.com/giannoccarol/installer-cityman-a16), you pick one
of three modes and the installer goes through the steps in order.

| Mode | Phone before | What happens | Data |
|---|---|---|---|
| **Full** | Windows 10 Mobile (or 8.1), **already unlocked and in mass storage mode** (the preparation, done with WPinternals outside the installer; checked first) | backup, firmware from Windows, BootShim + lk1st on EFIESP, lk1st check, **Android partitions (Windows erased)**, TWRP in RAM, format data, ROM | **Windows and all its data erased** (after a backup on the PC) |
| **Clean** | lk1st and the Android partitions already in place | TWRP in RAM, format data, wipe cache and Dalvik, ROM | all Android data erased, internal storage included |
| **Update** | this ROM already installed | TWRP in RAM, ROM | kept |

### Full, step by step

1. **Backup** (mass storage): GPT, every partition under 512 MiB read twice and compared, and,
   recommended, a compressed image of the whole phone.
2. **Firmware from Windows**: ADSP, Wi-Fi, modem, Venus, calibration, ACPI tables and registry, kept on
   the PC.
3. **Boot chain**: BootShim, lk1st and a new BCD entry on EFIESP, after a backup of EFIESP. In the Full
   mode Android is the default and only entry (Windows is about to be erased). You type `INSTALL`.
4. **lk1st check**: you restart the phone; the installer waits for lk1st in fastboot.
5. **Android partitions (phase 2)**: TWRP is started in RAM (`fastboot boot`). The 11 provisioning
   partitions (DPP, MODEM_FS1/FS2/FSC/FSG, SSD, DDR, DBI, UEFI_BS_NV, UEFI_RT_NV, BACKUP_BS_NV) are
   copied to the PC and checked. You type **`ERASE WINDOWS`**. The new GPT (same layout as the
   Lumia 950 XL installer) is written, read back and checked; MainOS, Data and MMOS disappear. The
   Android partitions are formatted, lk1st is copied to `aboot` (it also stays on EFIESP, where BootShim
   loads it), the modem data goes to `modemst1/2`, `fsc`, `fsg`, `ssd`, and the Wi-Fi/Bluetooth MAC
   addresses and Wi-Fi calibration from DPP go to `persist`.
6. **ROM**: TWRP is started again (so it sees the new partitions), `twrp format data`, `wipe cache`,
   `wipe dalvik`, then `adb sideload` of the ROM zip. The TWRP log must say
   `script succeeded: result was [1.000000]`, the boot partition must match `boot.img` of the zip, then
   the phone restarts into Android.

Every step is saved in `work/state.json`: run **Full** again and it continues where it stopped
(also after phase 2 was interrupted: with the Android table already written it formats and provisions
again, and never asks to erase Windows twice).

### One installer, several phones

Everything that differs between the phones is in
[`bandit_installer/devices.py`](bandit_installer/devices.py) and in the per-phone sections of
[`payload/payload.json`](payload/payload.json): model codes, ROM and TWRP names, lk1st builds and test
kernels, which modes are ready. The rest is shared, because the phones have the same Windows Phone 8.1
boot manager and the same Spec B partition set: BCD entry, phase 2 table, provisioning, TWRP over adb.

- A TWRP image is accepted only when it is pinned in `checksums.sha256` **and** its name carries the
  codename (`twrp-…-bandit.img`); the TWRP of another phone is refused by name.
- A ROM is accepted only when its OTA metadata lists the phone (`pre-device`); zips without metadata
  are refused.
- The model is read from DPP in mass storage (backup, firmware, boot chain, restore) and again in TWRP
  before phase 2, Clean or Update.
- Phase 2 refuses an EFIESP that holds the lk1st of another phone, and **Restore EFIESP** refuses a
  backup taken from another phone (different disk GUID).

### Clean and Update

1. The ROM zip and the TWRP image are checked (SHA-256) before anything touches the phone.
2. The phone is brought to lk1st fastboot: from Android with `adb reboot bootloader`, or it is already
   in fastboot.
3. `fastboot boot` of TWRP (in RAM, never flashed), wait for adb in recovery (root).
4. The Android partitions must be there (otherwise: use Full).
5. **Clean**: you type `ERASE`, then `twrp format data`, `wipe cache`, `wipe dalvik`.
   **Update**: the installed system is read from the system partition; if it is not LineageOS 18.1
   for this phone you must type `UPDATE` to go on (otherwise use Clean).
6. `adb sideload` of the ROM, TWRP log check, boot partition check, restart.

## Files you need

| File | Where | Where from |
|---|---|---|
| The installer | anywhere with a few GB free | [releases](https://github.com/lumia-msm8974/bandit-installer/releases): `bandit-installer-<version>-windows.zip` (with its own Python) or `-linux.tar.gz` |
| ROM: `lineage-18.1-YYYYMMDD-UNOFFICIAL-<codename>.zip` **and** `SHA256SUMS` | `rom/` in the installer folder | Lumia 1520: [android_device_nokia_bandit releases](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases); Lumia 930: [android_device_nokia_martini releases](https://github.com/lumia-msm8974/android_device_nokia_martini/releases) |
| TWRP: `twrp-3.7.0_9-1-bandit.img` (1520), `twrp-3.7.0_9-0-martini.img` (930) | `twrp/` in the installer folder (downloaded by the installer if missing) | [release `twrp-bandit-20261102`](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/tag/twrp-bandit-20261102); Lumia 930: [release `twrp-martini-20261113`](https://github.com/lumia-msm8974/android_device_nokia_martini/releases/tag/twrp-martini-20261113) |
| Platform Tools (`adb`, `fastboot`) | `tools/` (downloaded by the installer if missing) | Google |

The ROM zip is checked against `SHA256SUMS` next to it (or `--rom-sha256`); the TWRP image against
[`checksums.sha256`](checksums.sha256) (SHA-256 `c53687b5…ddabd2d` for bandit). The ROM zip must declare
the phone in its OTA metadata; it contains the phone's firmware.

## Requirements

- For **Full**: the phone **unlocked with [WPinternals](https://github.com/ReneLergner/WPinternals)**
  (needs a Windows PC), which also switches it to mass storage mode. After unlocking you may move the
  cable to a Linux PC: the phone stays in mass storage mode until it restarts.
- Battery above 50%, a good USB cable, no USB hub.
- **Python 3.8 or newer**, standard library only. The Windows package includes its own Python.
- **Android Platform Tools** (`fastboot`, `adb`): the installer can download Google's copy into `tools/`.
- Windows: the [Google USB driver](https://developer.android.com/studio/run/win-usb) for fastboot and
  adb. The BCD is changed with Windows' own `bcdedit`.
- Linux: `util-linux`, `ntfs3` or `ntfs-3g` for the firmware step. The BCD is written by the
  installer itself (`regfw.py`), so no hivex package is needed; `LUMIA_BCD_WRITER=hivex` uses
  `python3-hivex` instead, if installed.
- For the app window: Edge or Chrome on Windows, Chromium/Chrome/Brave/Edge on Linux (otherwise the
  default browser opens a tab).

## Usage

```bash
./installer.sh            # Linux (asks for sudo)
```

```bat
installer.bat             :: Windows (asks for administrator rights)
```

| Option | Meaning |
|---|---|
| `--device bandit\|martini` | the phone (default: the one chosen last, else `bandit`) |
| `--mode full\|clean\|update` | start that mode without the menu |
| `--rom <zip>` | ROM zip (default: the `lineage-18.1-*-<codename>.zip` in `rom/`) |
| `--rom-sha256 <hash>` | expected SHA-256 of the ROM (default: `SHA256SUMS` next to the zip) |
| `--twrp <img>` | TWRP image (default: the phone's one in `twrp/`); it must be listed in `checksums.sha256` with the phone's codename in its name |
| `--phase2-recovery <img>` | advanced: recovery for phase 2 instead of TWRP (it needs adb root, `dd`, `mke2fs`, `blockdev`) |
| `--serial <serial>` | use only this phone when more than one device is on adb/fastboot (also `ANDROID_SERIAL`) |
| `--adb <path>`, `--fastboot <path>` | use these executables |
| `--lang en\|it` | interface language (default: English, or the one last chosen in the settings) |
| `--text` | terminal interface, for SSH or machines without a browser |
| `--text --action <task>` | run one task and exit (`backup`, `chain`, `verify`, `phase2`, `boot`, `diag`…) |
| `--work <folder>` | backups, logs and progress (default `work/` next to the installer) |
| `--no-browser` | only print the address of the app window |
| `--no-elevate` | do not ask for administrator/root rights (enough for Clean and Update) |

Examples: `./installer.sh --text --mode update`, `installer.bat --mode clean --lang it`.

Everything the installer saves goes to `work/`: `backups/` (phone, EFIESP and phase 2 provisioning
copies), `private/` (firmware), `logs/` (session logs, TWRP `recovery.log` of every install) and
`state.json`. Backups and firmware contain personal and modem data and Nokia/Microsoft files:
**do not publish them**.

## Updating later

Put the new `lineage-18.1-…-<codename>.zip` and its `SHA256SUMS` in `rom/` (remove the old zip), enable
USB debugging in Android (Settings → System → Developer options) and run **Update**. Data is kept.

## Safety

- **Volume up at power-on always opens flash mode**, the way back with WPinternals. The installer
  refuses any BCD that would change that action.
- Windows is erased only in the **Full** mode, only after the backup of the provisioning partitions
  and only after you type `ERASE WINDOWS`. Before that it checks that lk1st is the default boot entry
  (otherwise the phone would have nothing to start).
- Every other destructive step asks for a word: `INSTALL` (EFIESP), `ERASE` (Clean), `UPDATE` (Update
  over another system), `WRITE`, `RESTORE`.
- The ROM and TWRP are checked with SHA-256 before the phone is touched. TWRP is only started in RAM,
  never flashed. TWRP's MTP is never used.
- The new partition table is computed on the PC, checked with an independent parser before and after
  writing, and every partition outside Data, MainOS and MMOS stays byte for byte as it was.
- The BCD is never copied from another phone: the new entry takes the EFIESP device element from an
  entry already on the same partition, and the new BCD is read back and checked before it is written.
- The kit files have pinned SHA-256 hashes ([`payload/payload.json`](payload/payload.json)), and the
  entry point of each lk1st ELF must match the one written in the kit. An lk1st never tried on a phone
  is marked *untested* and the boot chain step says how to go back before writing it.
- The phone model is read from the phone (DPP) and must match the chosen phone: see
  [One installer, several phones](#one-installer-several-phones).
- The local app window is served on `127.0.0.1` only, on a random port, with a random token.
- With more than one Android device connected the installer stops: disconnect the others or use
  `--serial`.

## Known limitations

- `adb sideload` stops around **47%** with `Total xfer: 1.00x`: normal for this block-based ROM. The
  installer decides from the TWRP log, not from that number.
- The `twrp` command line client prints **`Segmentation fault`** after each command: the command has
  run; the installer checks the TWRP log instead of the exit code.
- Do not open the phone in a file manager or MTP tool on the PC while the installer runs: an MTP client
  makes adb lose the phone in TWRP.
- The **first start** after an install takes several minutes (longer after Clean or Full).
- Phase 2 was run on an RM-937 on 2026-11-01 with the ROM's own recovery (`kit/phase2/phase2.py`). The
  installer runs the same sequence with **TWRP**, which has adb as root and the same USB properties,
  plus `dd`, `mke2fs` and `blockdev`: this combination has been tested only with a simulated phone so
  far. If TWRP lacks a tool, the installer stops before writing anything; `--phase2-recovery` then
  takes the ROM's recovery image.
- The Windows-specific parts (BCD with `bcdedit`, disk and USB detection) have not been run on Windows
  yet.

## Getting out of a boot loop

1. **If adb still answers** during the loop (`adb devices` shows the phone; USB debugging must have
   been enabled and this PC authorised): run **Update** (keeps data) or **Clean**. The installer itself
   sends `adb reboot bootloader`.
2. **If adb never comes up**, make lk1st stop in fastboot by spoiling the boot image header (Linux,
   phone in mass storage through flash mode and WPinternals; not yet tried on the phone): find the
   `boot` partition with `lsblk -o NAME,PARTLABEL`, check the name twice, then
   `sudo dd if=/dev/zero of=/dev/sdX<n> bs=4096 count=1 conv=fsync`. lk1st, finding no valid boot
   image, stays in fastboot: run **Update** or **Clean**.
3. **Last resort**: volume up at power-on (flash mode), then WPinternals or Windows Device Recovery
   Tool to flash a Windows FFU. Then start again with **Full**.

## Advanced tools

The single steps are also available on their own, for development and recovery:

| Task | Phone mode | Writes to the phone |
|---|---|---|
| **Backup** | mass storage | no: GPT, every partition under 512 MiB (read twice and compared), optional full compressed image, SHA-256 list |
| **Firmware** | mass storage | no: ADSP/WCNSS/modem/Venus firmware, audio and Wi-Fi calibration, ACPI tables and registry from Windows; `.mbn` split into `.mdt` + `.bNN` |
| **Boot chain** | mass storage | EFIESP only: BootShim, lk1st and a BCD entry, after a backup of EFIESP (files and raw image) |
| **Check lk1st** | restart → fastboot | no: waits for lk1st, explains what it sees on the way, saves `getvar all` and the lk1st log |
| **Android partitions** | fastboot → TWRP | yes: phase 2 alone (erases Windows), no ROM |
| **Boot in RAM** | fastboot | no: `fastboot boot` of the K1 test kernel or of a `boot.img` from a build |
| **Previous boot log** | fastboot | no: ram console (ramoops) and lk1st log after a hang |
| **Write partition** | fastboot | yes: `fastboot flash` of Android partitions only (`boot`, `recovery`, `system`…) |
| **Restore EFIESP** | mass storage | yes: back to a backup, by files (Windows and Linux) or whole image (Linux) |
| **Diagnostics** | any | no |

## Status

| Part | State |
|---|---|
| BootShim `elf-loader-pre3` + lk1st `base-0ff` (1520) | fastboot verified on an RM-937 (2026-11-01) |
| lk1st `base-7cc` (930) | built from [lumia-msm8974/lk2nd](https://github.com/lumia-msm8974/lk2nd) branch `martini`; boots and runs fastboot on an RM-1045 (2026-11-13) |
| Lumia 930 install | backup, firmware, boot chain, phase 2 and Clean on an RM-1045 on Windows Phone 8.1 (2026-11-13), Update with TWRP in RAM and `adb sideload` (2026-11-14); Full never run end to end |
| Phase 2 sequence (GPT, format, provisioning) | run on an RM-937 with the ROM's recovery (2026-11-01); in the installer with TWRP: simulated phone only |
| TWRP 3.7.0_9-1 in RAM, `adb sideload` of the ROM | verified on an RM-937 (2026-11-02) |
| Clean/Update flow of the installer | simulated phone only (`tests/test_flows.py`) |
| Backup, firmware, boot chain, EFIESP restore | simulated phone only (`tests/test_mass.py`, disk image through mtools) |
| Lumia 930 partition table | the GPT read from a real RM-1045 unlocked with WPinternals (W10M layout: `BACKUP_BS_NV` present, MainOS and Data differ from the 8.1 FFU); phase 2 layout and checks pass on it |
| Linux BCD writer (hivex) | output byte-identical to the kit script used on the phone |
| Linux BCD writer (pure Python, default) | same keys and values as the hivex writer on a real 1520 BCD, for every plan; hivex reads its output back; **not yet used on a phone** |
| Windows BCD writer (bcdedit), Windows disk/USB detection | written, **not yet run on Windows** |

## Tests

```bash
python3 -m unittest discover -s tests
```

Nothing talks to a real device. The tests need `sfdisk`, `mkfs.fat` and mtools.

- `tests/test_layout.py` compares the computed partition table with what `parted` produces from the
  Android4Lumia950 `partition.sh`, on the 1520 table and on the GPT of a real 930.
- `tests/test_flows.py` runs the three modes in `--text` mode against a fake phone (`tests/fake/adb`,
  `tests/fake/fastboot`) whose eMMC is a sparse disk image with the 1520's partition table.
- `tests/test_mass.py` runs the mass storage tasks (backup, firmware, boot chain, EFIESP restore) with
  `--test` on a disk image (`LUMIA_TEST_DISK`, read and written with mtools, no root): model checks,
  the wrong phone, the Lumia Icon, the 930's locked Full mode. The boot chain test needs a real BCD
  (`LUMIA_TEST_BCD=<EFI/Microsoft/Boot/BCD of a phone backup>`); none is shipped.
- `tests/test_bcd.py` checks the plans and both Linux writers on that BCD; with `python3-hivex` it also
  compares the two writers key by key and has hivex read the pure Python output.

`tests/docker/run-tests.sh` runs everything in a Debian container that has `python3-hivex`, for the
comparison tests:

```bash
LUMIA_TEST_BCD=/path/to/BCD tests/docker/run-tests.sh -v
```

The same `--test` mode rehearses the mass storage steps on a copy of a real phone's full backup
(`LUMIA_TEST_DISK=copy.img`, `LUMIA_TEST_DIRS="MainOS=<folder with the Windows files>"`).

## Layout

```text
installer.py, installer.sh, installer.bat   entry points
checksums.sha256   pinned TWRP image(s)
rom/, twrp/        where the ROM zip (+ SHA256SUMS) and the TWRP image go
bandit_installer/
  app.py       arguments, privileges, text interface
  devices.py   the phones: models, ROM/TWRP names, payload folder, modes ready
  web.py       local server and app window
  web/         the interface (one HTML file)
  steps.py     the tasks and the three modes
  android.py   ROM and TWRP checks, lk1st fastboot, TWRP over adb, wipe, sideload
  phase2.py    Android partitions: provisioning backup, GPT, format, provisioning
  layout.py    the Android partition table (as partition.sh leaves it) and its check
  fat.py       FAT12/16 reader and formatter (DPP, EFIESP, aboot, modem)
  bcd.py       BCD plan, writers (pure Python, hivex, bcdedit) and checks
  regf.py      read-only registry hive parser
  regfw.py     registry hive writer in pure Python (the Linux BCD writer)
  gpt.py       GPT parser (the phone's partitions are always read from its GPT)
  images.py    BootShim ELF checks, boot image header, PIL firmware split
  phone.py     phone mode from USB ids, fastboot and adb
  sysdeps.py   Linux and Windows specifics
payload/       BootShim (shared); bandit/ and martini/: lk1st builds, test kernels; pinned hashes
tests/         layout, flow and mass storage tests, fake adb/fastboot, Docker runner
```

## Credits

- **Android4Lumia950** (djokcho, EpicLPer, j0sh1x, sonic011gamer and the community): the
  [Lumia 950/950 XL installer](https://github.com/Android4Lumia950/installer) (branch `cityman-temp`):
  the modes, the partition layout of `partition.sh`, the provisioning of `provision.sh` and the BCD this
  installer takes as its model.
- **imbushuo (Bingxing Wang)**: [boot-shim](https://github.com/imbushuo/boot-shim), release
  `elf-loader-pre3` (ARM32), the UEFI application that loads lk1st. Included unmodified.
- **msm8916-mainline**: [lk2nd](https://github.com/msm8916-mainline/lk2nd), base of lk1st; Lumia ports in
  [lumia-msm8974/lk2nd](https://github.com/lumia-msm8974/lk2nd) (branches `bandit` and `martini`).
- **Mainline4Lumia** (Dominduchami): [lk2nd `martini-support`](https://github.com/Mainline4Lumia/lk2nd/tree/martini-support),
  the Lumia 930 lk1st device tree and its 0x7CC00000 load address.
- **KorelisLabs**: [lumia-1520-mainline](https://github.com/KorelisLabs/lumia-1520-mainline): 1520 boot
  chain, lk1st device tree and load address, and the PIL split format reproduced in `images.py`.
- **René Lergner (HeathCliff)**: [WPinternals](https://github.com/ReneLergner/WPinternals), unlock and
  mass storage mode.
- **Richard W.M. Jones (Red Hat)**: [hivex](https://libguestfs.org/hivex.3.html), the first Linux BCD
  writer, whose way of changing a hive (`regfw.py` follows it) and output the tests compare against.
- **Joachim Metz**: the regf format notes of [libregf](https://github.com/libyal/libregf)
  (`documentation/Windows NT Registry File (REGF) format.asciidoc`), the reference for `regf.py` and
  `regfw.py`.
- **TeamWin**: [TWRP](https://twrp.me), the recovery the installer starts in RAM (bandit build from
  [android_device_nokia_bandit](https://github.com/lumia-msm8974/android_device_nokia_bandit/tree/twrp-9.0)).
- **Markynose (Mark Buchok)**: [lumia930-linux](https://github.com/Markynose/lumia930-linux), the
  BootShim → lk1st chain on the Lumia 930 (ELF entry 0x7cc00000).
- **zh / hao (2314372037)**: [android_device_nokia_martini](https://github.com/2314372037/android_device_nokia_martini)
  and its [installer](https://github.com/2314372037/installer) branch `martini`: the 930 model codes and
  the confirmation that the 950 XL partition scheme fits the 930.
- **LineageOS** and the `oppo_msm8974` kernel the bandit kernel is based on.
- Google for the Android Platform Tools and USB driver.
- The **Python Software Foundation**: the Windows package includes the official
  [embeddable Python](https://www.python.org/downloads/windows/) (PSF License), unmodified.

---

## In italiano

Installer per LineageOS 18.1 (Android 11) sui Lumia MSM8974: Lumia 1520 (`bandit`) e
Lumia 930 (`martini`, RM-1045: build di prova; ogni passo è stato eseguito su un RM-1045, la modalità Completa resta bloccata).
Il telefono si sceglie nella pagina di installazione o con `--device`; l'installer legge comunque il
modello dal telefono (DPP) e si ferma se non corrisponde. Per Windows e Linux, con una finestra
desktop in stile Windows 10 (interfaccia in inglese, italiano dalle impostazioni o con `--lang it`;
`--text` per il terminale). Tre modalità: **Completa** (da Windows sbloccato con WPinternals: backup,
catena BootShim → lk1st, partizioni Android con **Windows cancellato**, ROM), **Pulita** (partizioni
Android già presenti: dati cancellati, ROM) e **Aggiornamento** (questa ROM già installata: dati
conservati). La TWRP viene solo avviata in RAM. Metti lo zip della ROM e il suo `SHA256SUMS` in `rom/`;
la TWRP in `twrp/` (o la scarica l'installer). Pacchetti nella
[pagina delle release](https://github.com/lumia-msm8974/bandit-installer/releases), istruzioni complete
nella **[guida in italiano](docs/GUIDA.md)**. Avvio: `./installer.sh` su Linux, `installer.bat` su
Windows. Il volume su all'accensione resta sempre la modalità flash per il recupero.

### Prima di cominciare: sbloccare il bootloader (solo Windows)

**WPinternals è uno strumento separato**, di René Lergner
([github.com/ReneLergner/WPinternals](https://github.com/ReneLergner/WPinternals)): **non fa parte di
questo installer** e **funziona solo su Windows**. Per la Completa serve due volte, prima che
l'installer tocchi il telefono: per **sbloccare il bootloader** e per passare in **mass storage**
(*Switch to Mass Storage Mode*), che si attiva solo da WPinternals, quindi solo da Windows. Lo sblocco si
fa sempre prima su un PC Windows, anche se poi usi l'installer su Linux: sblocca e passa in mass storage
su Windows, poi sposta il cavo USB sul PC Linux; il telefono resta in mass storage fino al riavvio. Se
si riavvia prima della catena di avvio, rimettilo in mass storage da WPinternals su Windows. Dopo la
catena di avvio l'installer usa solo fastboot e adb: Pulita e Aggiornamento non hanno bisogno di
WPinternals.
