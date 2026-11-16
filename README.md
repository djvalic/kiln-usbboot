# kiln-usbboot

**Status: partly tested on real hardware.** `info` has run against an
AD5X's boot ROM (alexshev86, Windows 10, djvalic/kiln-os#68): the
descriptors are as expected and GET_CPU_INFO answers `X2600`. `run-spl`
has not run on a printer yet. The protocol was cross-checked against
Ingenic's GPL SDK source, Rockbox's jztool and ballaswag/ingenic-usbboot,
and the tests run against a fake USB device; until `run-spl` has run on a
printer, expect that something may need changing.

An open loader for the USB mode of the Ingenic X2600 boot ROM. I made it
for the Flashforge AD5X and [Kiln OS](https://github.com/djvalic/kiln-os):
it cold-starts an SPL (stage 1) from the boot ROM with **nothing written
to the eMMC**. The ROM loads the image into its SRAM and runs it. Kiln's
SPL then sets up the clocks and DDR, reads the boot flag and boots the
kernel from the eMMC, read-only, as it would from a normal start. That is
how a new SPL can be tested from a reset chip before it ever goes onto a
board.

It is one Python file, `ad5x_usbboot.py`, under GPL-2.0-or-later.

## How it differs from the other tools

| Tool | Source | SoCs | What it does |
|---|---|---|---|
| Ingenic USBCloner | closed | many, X2600 included | reads and writes the flash (eMMC, NAND, NOR) through Ingenic's burner stages |
| Rockbox `jztool` | open (GPL) | X1000, X1600 | loads and runs boot code on Rockbox's Ingenic targets |
| ballaswag/ingenic-usbboot | open | X2000E | runs Ingenic's burner to write the flash |
| **kiln-usbboot** | open (GPL) | X2600 | loads and starts a stage-1 image only |

kiln-usbboot can't write the eMMC, by design: it has no burner stage and
no flash commands, only the ROM's "load this" and "start this" requests.
What the image you send does is up to that image; Kiln's SPL only reads
the eMMC.

## Requirements

- Python 3 (3.7 or newer).
- [pyusb](https://pypi.org/project/pyusb/) and libusb-1.0, for real use
  (not for `--dry-run` or the tests). In a venv:

  ```
  python3 -m venv ~/.venv/usb
  ~/.venv/usb/bin/pip install pyusb
  ~/.venv/usb/bin/python3 ad5x_usbboot.py info
  ```

  libusb-1.0 comes from your distribution (`libusb` on Arch,
  `libusb-1.0-0` on Debian and Ubuntu).
- On Windows (tested by alexshev86 on Windows 10, djvalic/kiln-os#68): in a
  venv, `pip install pyusb libusb-package`, then copy the `libusb-1.0.dll`
  from the `libusb_package` folder next to the venv's `python.exe` so
  pyusb finds a backend. It opened the boot ROM through the libusb-win32
  driver USBCloner had already installed, with no driver change, and
  USBCloner still worked afterwards. Two things look different there:
  pyusb doesn't report the speed, so `info` works it out from the
  512-byte packets, and every device shows as bus 0 address 1, so
  `--bus`/`--address` can't pick one of two attached boot ROMs (attach
  only one).
- Access to the boot ROM device, `a108:eaef`. Either run as root or add a
  udev rule, e.g. `/etc/udev/rules.d/70-ingenic-usbboot.rules`:

  ```
  SUBSYSTEM=="usb", ATTR{idVendor}=="a108", ATTR{idProduct}=="eaef", MODE="0660", TAG+="uaccess"
  ```

  then `sudo udevadm control --reload` and plug the device in again.
- A serial console on the printer helps a lot: on the AD5X, the DEBUG
  header (UART0, 115200 8N1, 1.8 V levels: measure TX at idle before you
  connect a 3.3 V adapter). Log it to a file (`tio -L`,
  `picocom --logfile`).

## Usage

```
python3 ad5x_usbboot.py info
python3 ad5x_usbboot.py run-spl spl-usb.bin
python3 ad5x_usbboot.py --dry-run run-spl spl-usb.bin
```

- `info` asks the ROM for its CPU info (GET_CPU_INFO) and prints the
  device's descriptors: bus, speed, interface and endpoints.
- `run-spl IMAGE` loads the image to 0x80001000 and starts it at
  0x80001800: GET_CPU_INFO, SET_DATA_ADDR, SET_DATA_LEN, the image on bulk
  OUT 0x01, FLUSH_CACHE, PROG_START1. `--no-flush` leaves FLUSH_CACHE out.
- `--dry-run` prints every transfer in order (request, wValue, wIndex,
  length, timeout, and the image's SHA-256) and sends nothing. It needs
  neither pyusb nor a device.
- `--timeout MS` (control transfers, default 1000), `--bulk-timeout MS`
  (default 10000), `--cpu-info-length N` (default 5, what the X2600 answers), and `--bus` and
  `--address` to pick one device when more than one is attached.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | done (`run-spl`: the image went out and PROG_START1 was answered) |
| 1 | error; nothing was started |
| 3 | everything went out, but PROG_START1 got no answer: the ROM may have jumped before it replied. Watch the serial console |

### The image

Any X2600 stage-1 image laid out from 0x80001000: a 2 KiB parameter area
first, the code from offset 0x800 (0x80001800, where PROG_START1 jumps),
at most 0x7000 bytes in all. The tool sends the whole file as one block,
the way Ingenic's Cloner sends its parameter records and stage 1.

The main use is Kiln's `spl-usb.bin` from Kiln OS's U-Boot build. It is
Kiln's eMMC SPL image (`spl-mmc.img`) with an 8-byte tag, `KILNUSB1`, at
offset 0x7F0 of the header, which tells Kiln's SPL it was started over
USB. Other images don't need the tag. When an image has an Ingenic SPL
header (`LPSM` at 0, the `INGE` parameters at 0x100), the tool checks that
the sizes in it agree with the file. Without that header, it notes that
the image is sent as a raw stage-1 image.

## Getting the printer into USB mode

### J28

J28 is the X2600's USB OTG port on the AD5X mainboard: a white 5-pin JST
PH 2.0 mm connector labelled `USB`, pins `GND GND P N 5V` (P is D+, N is
D-). The outside USB-C port is on another controller and can't be used.
Newer boards have J28 unfitted; the footprint is there and a connector
can be soldered in.

Connect D+, D- and GND to the laptop. **Don't connect the laptop's 5 V
until you have measured the `5V` pin**: if the printer already powers it,
leave the laptop's 5 V off, so there are never two supplies on one line.

### Entering USB boot

Either way, the laptop then sees `a108:eaef` (`lsusb -d a108:eaef`), and
the printer is off the network because Linux isn't running.

- **Soft entry, no opening of the printer besides J28.** On the running
  printer, as root:

  ```
  devmem 0x100000cc 32 0x42575302
  reboot
  ```

  This writes Ingenic's soft USB boot request ("SWB" << 8 | 2) into
  CPM_SLPC; after the reboot the ROM starts in USB mode. alexshev86 found
  and tested this on an AD5X running Kiln OS
  ([djvalic/kiln-os#68](https://github.com/djvalic/kiln-os/issues/68)).
  Don't do it while the printer is printing.
- **K4 at power-on.** Closing the pad pair K4 changes the boot-select
  straps (`boot_sel[2:0]`, R370-R374 next to K4) from `101` (eMMC) to
  `111` (USB), so the ROM starts in USB mode at every power-on, even with
  a good SPL on the eMMC. Each power cycle is then a true cold start.

### Getting back

A power cycle (off for a few seconds) returns the printer to its normal
eMMC boot: CPM_SLPC doesn't survive power-off. With K4 closed, open it
again first. Kiln's SPL also clears CPM_SLPC when it starts over USB, so
the next reboot goes back to the eMMC. None of this writes the eMMC.

## The protocol

Vendor requests on EP0 (recipient device), data on bulk OUT 0x01:

| Request | Type | wValue / wIndex |
|---|---|---|
| 0x00 GET_CPU_INFO | IN 0xC0, wLength 8 | 0 / 0 |
| 0x01 SET_DATA_ADDR | OUT 0x40 | address bits 31:16 / 15:0 (4-byte aligned) |
| 0x02 SET_DATA_LEN | OUT 0x40 | length, split the same way |
| 0x03 FLUSH_CACHE | OUT 0x40 | 0 / 0 |
| 0x04 PROG_START1 | OUT 0x40 | entry point, split the same way |
| 0x05 PROG_START2 | OUT 0x40 | stage 2 in DDR (not used here) |

Sources: Ingenic's GPL device side (SDK U-Boot `common/spl/usb_boot.c`,
`include/usb_boot/usb_boot.h`, `include/cloner/cloner.h`) and host side
(`common/usb_load.c`); Rockbox `utils/jztool` (directions, GET_CPU_INFO
as IN, endpoints, timeouts); ballaswag/ingenic-usbboot (the same lineage
on the X2000). The X2600 addresses come from Ingenic Cloner's
`firmwares/x2600/config.cfg` (`ginfo=0x80001000`, `spl=0x80001800`,
`d2i_len=0x7000`; a text config, nothing of Cloner is used) and the SDK's
`include/configs/burner_x2600.h`. The docstring at the top of
`ad5x_usbboot.py` cites them line by line.

FLUSH_CACHE before PROG_START1 follows Ingenic's `usb_load.c`, because
the X2600's load window is cached (KSEG0). Rockbox's X1000 path and
ballaswag's X2000 path don't flush (their stage 1 sits in uncached or
TCSM memory); Rockbox's X1600 path flushes before the upload. If the
X2600 ROM disagrees, `--no-flush` is there.

What only hardware can tell: the X2600's GET_CPU_INFO answer and
endpoints, whether the ROM answers PROG_START1 before it jumps, and
whether the cache flush is right.

## Tests

```
python3 -m unittest -v
```

Standard library only, no device and no pyusb: the transfers go to a
fake USB device and the dry run is compared line by line, on synthetic
images. To check a real image as well, point `KILN_SPL_USB` at it:
`KILN_SPL_USB=path/to/spl-usb.bin python3 -m unittest -v`.

## Credits

- **alexshev86**: the soft entry into USB boot, and the J28 and K4
  findings ([djvalic/kiln-os#68](https://github.com/djvalic/kiln-os/issues/68)).
- **jolting**: the mainline U-Boot and SPL port for the X2600 and the AD5X
  ([djvalic/kiln-os#68](https://github.com/djvalic/kiln-os/issues/68)).
- **Ingenic**: the GPL device-side and host-side USB boot code in their
  SDK.
- **Rockbox `jztool`**, by Aidan MacDonald.
- **[ballaswag/ingenic-usbboot](https://github.com/ballaswag/ingenic-usbboot)**.

## License

GPL-2.0-or-later. See `LICENSE`.
