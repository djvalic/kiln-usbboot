#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for ad5x_usbboot.py, the X2600 boot ROM USB loader.

No hardware and no pyusb: the transfers go to a fake device with pyusb's
ctrl_transfer()/write() interface, and --dry-run is compared line by
line. The images are synthetic, built here in the layout of Kiln's
spl-mmc.img / spl-usb.bin / spl-usb2.bin (an Ingenic SPL header) or as a
raw stage-1 image. Real images can be checked too:
KILN_SPL_USB=path/to/spl-usb.bin, and for run-uboot
KILN_SPL_USB2=path/to/spl-usb2.bin with KILN_UBOOT=path/to/u-boot.bin.

  python3 -m unittest -v
"""
import importlib.util
import array
import hashlib
import os
import random
import struct
import tempfile
import unittest

import ad5x_usbboot as ub

REAL_IMAGE = os.environ.get("KILN_SPL_USB", "")
REAL_IMAGE2 = os.environ.get("KILN_SPL_USB2", "")
REAL_UBOOT = os.environ.get("KILN_UBOOT", "")


class FakeUsbError(Exception):
    pass


class FakeDevice:
    """pyusb's Device as far as the tool uses it, recording every call."""

    def __init__(self, cpu_info=b"X2600", fail=None, short_write=False):
        self.calls = []
        self.cpu_info = cpu_info
        self.fail = fail or set()  # bRequest values that raise
        self.short_write = short_write
        self.idVendor, self.idProduct = ub.VID, ub.PID
        self.bus, self.address, self.speed = 3, 7, 3

    def ctrl_transfer(self, bmRequestType, bRequest, wValue=0, wIndex=0,
                      data_or_wLength=None, timeout=None):
        self.calls.append(("ctrl", bmRequestType, bRequest, wValue, wIndex,
                           data_or_wLength, timeout))
        if bRequest in self.fail:
            raise FakeUsbError("[Errno 110] Operation timed out")
        if bmRequestType & 0x80:
            return array.array("B", self.cpu_info[:data_or_wLength])
        return 0

    def write(self, endpoint, data, timeout=None):
        self.calls.append(("write", endpoint, bytes(data), timeout))
        if "write" in self.fail:
            raise FakeUsbError("[Errno 32] Pipe error")
        return len(data) - 1 if self.short_write else len(data)

    def get_active_configuration(self):
        raise FakeUsbError("no descriptors in the fake")


def spl_bin(size=10240, seed=1):
    r = random.Random(seed)
    return bytes(r.randrange(256) for _ in range(size))


def mmc_image(size=10240):
    """Kiln's spl-mmc.img layout: "LPSM" and the block count, the INGE
    params copy at 0x100 with the image length, the code from 0x800, in
    whole 512-byte blocks."""
    n = (0x800 + size + 511) // 512 * 512
    d = bytearray(n)
    d[0:4] = b"LPSM"
    struct.pack_into("<I", d, 4, n // 512)
    d[0x100:0x104] = b"INGE"
    struct.pack_into("<I", d, 0x104, n)
    d[0x800:0x800 + size] = spl_bin(size)
    return bytes(d)


def usb_image(size=10240):
    """spl-usb.bin: spl-mmc.img with the KILNUSB1 tag at 0x7F0."""
    d = bytearray(mmc_image(size))
    d[0x7F0:0x7F8] = b"KILNUSB1"
    return bytes(d)


def raw_image(size):
    """A stage-1 image without an Ingenic SPL header."""
    return spl_bin(size, seed=2)


def usb2_image(size=10240):
    """spl-usb2.bin: spl-mmc.img with the KILNUSB2 tag at 0x7F0."""
    d = bytearray(mmc_image(size))
    d[0x7F0:0x7F8] = b"KILNUSB2"
    return bytes(d)


def uboot_bin(size=70000):
    """A stand-in for U-Boot proper's raw u-boot.bin (no header)."""
    return spl_bin(size, seed=7)


class Run:
    """main() with a fake device; output, errors and pauses collected."""

    def __init__(self, argv, dev=None):
        self.out, self.err, self.closed, self.slept = [], [], [], []
        self.dev = dev if dev is not None else FakeDevice()

        def opener(bus, address):
            self.opened = (bus, address)
            return self.dev
        self.rc = ub.main(argv, out=self.out.append, err=self.err.append,
                          opener=opener, closer=self.closed.append,
                          sleep=self.slept.append)


def tmpfile(data):
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    f.write(data)
    f.close()
    return f.name


class TestRequests(unittest.TestCase):
    """The transfer sequence against the fake device: the golden calls."""

    def setUp(self):
        self.img = usb_image()
        self.path = tmpfile(self.img)

    def tearDown(self):
        os.unlink(self.path)

    def expected(self, flush=True, n=None):
        n = len(self.img) if n is None else n
        calls = [
            ("ctrl", 0xC0, 0x00, 0, 0, 5, 1000),
            ("ctrl", 0x40, 0x01, 0x8000, 0x1000, None, 1000),
            ("ctrl", 0x40, 0x02, n >> 16, n & 0xFFFF, None, 1000),
            ("write", 0x01, self.img, 10000),
        ]
        if flush:
            calls.append(("ctrl", 0x40, 0x03, 0, 0, None, 1000))
        calls.append(("ctrl", 0x40, 0x04, 0x8000, 0x1800, None, 1000))
        return calls

    def test_run_spl_golden(self):
        r = Run(["run-spl", self.path])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.dev.calls, self.expected())
        self.assertEqual(r.closed, [r.dev])
        self.assertIn("boot ROM: 'X2600' (5 bytes: 5832363030)", r.out)
        self.assertTrue(any("started: %d bytes at 0x80001000, entry "
                            "0x80001800" % len(self.img) in o
                            for o in r.out))

    def test_no_flush(self):
        r = Run(["run-spl", "--no-flush", self.path])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.dev.calls, self.expected(flush=False))

    def test_timeouts_and_cpu_info_length(self):
        r = Run(["--timeout", "250", "--bulk-timeout", "3000",
                 "--cpu-info-length", "8", "run-spl", self.path])
        self.assertEqual(r.rc, 0, r.err)
        c = r.dev.calls
        self.assertEqual(c[0], ("ctrl", 0xC0, 0, 0, 0, 8, 250))
        self.assertEqual(c[3][3], 3000)
        self.assertTrue(all(x[-1] == 250 for x in c if x[0] == "ctrl"))

    def test_bus_address_passed(self):
        r = Run(["--bus", "3", "--address", "7", "info"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.opened, (3, 7))

    def test_info(self):
        r = Run(["info"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.dev.calls, [("ctrl", 0xC0, 0, 0, 0, 5, 1000)])
        self.assertTrue(r.out[0].startswith("device a108:eaef on bus 3 "
                                            "address 7, high speed"))
        self.assertIn("GET_CPU_INFO: 'X2600' (5 bytes: 5832363030)", r.out)

    def test_speed_from_packets_when_unknown(self):
        # pyusb on Windows leaves dev.speed empty (alexshev86, #68)
        class Ep:
            bEndpointAddress, bmAttributes, wMaxPacketSize = 0x01, 2, 512

        class Intf(list):
            bInterfaceClass, bNumEndpoints = 0xFF, 1

        class Cfg:
            def __getitem__(self, key):
                return Intf([Ep()])

        dev = FakeDevice()
        dev.speed = None
        dev.get_active_configuration = lambda: Cfg()
        self.assertIn("high (from the 512-byte packets) speed",
                      ub.describe_device(dev).splitlines()[0])
        dev.get_active_configuration = FakeDevice.get_active_configuration.__get__(dev)
        self.assertIn("unknown speed", ub.describe_device(dev).splitlines()[0])

    def test_start1_unanswered(self):
        r = Run(["run-spl", self.path],
                FakeDevice(fail={ub.PROG_START1}))
        self.assertEqual(r.rc, ub.EXIT_START_UNCONFIRMED)
        self.assertEqual(len(r.dev.calls), 6)  # everything went out
        self.assertIn("watch the serial console", r.err[0])
        self.assertEqual(r.closed, [r.dev])

    def test_set_addr_fails(self):
        r = Run(["run-spl", self.path],
                FakeDevice(fail={ub.SET_DATA_ADDR}))
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(len(r.dev.calls), 2)  # stopped there
        self.assertIn("SET_DATA_ADDR", r.err[0])

    def test_bulk_fails_or_short(self):
        r = Run(["run-spl", self.path], FakeDevice(fail={"write"}))
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertIn("bulk OUT 0x01", r.err[0])
        self.assertEqual(r.dev.calls[-1][0], "write")
        r = Run(["run-spl", self.path], FakeDevice(short_write=True))
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertIn("of %d bytes went out" % len(self.img), r.err[0])
        self.assertEqual(r.dev.calls[-1][0], "write")  # no START1

    def test_untagged_image_sent_with_a_note(self):
        mmc = mmc_image()
        p = tmpfile(mmc)
        try:
            r = Run(["run-spl", p])
        finally:
            os.unlink(p)
        self.assertEqual(r.rc, 0, r.err)
        self.assertTrue(r.out[0].startswith("note: no KILNUSB1 tag"))
        self.assertEqual(r.dev.calls[3], ("write", 1, mmc, 10000))

    def test_raw_image_sent_with_a_note(self):
        raw = raw_image(9432)       # Cloner's stage 1 isn't block-sized
        p = tmpfile(raw)
        try:
            r = Run(["run-spl", p])
        finally:
            os.unlink(p)
        self.assertEqual(r.rc, 0, r.err)
        self.assertTrue(r.out[0].startswith("note: no Ingenic SPL header"))
        self.assertEqual(r.dev.calls[2],
                         ("ctrl", 0x40, 0x02, 0, 9432, None, 1000))
        self.assertEqual(r.dev.calls[3], ("write", 1, raw, 10000))
        self.assertEqual(r.dev.calls[-1],
                         ("ctrl", 0x40, 0x04, 0x8000, 0x1800, None, 1000))

    def test_split_and_alignment(self):
        self.assertEqual(ub.split(0x80001800), (0x8000, 0x1800))
        self.assertEqual(ub.split(0x5400), (0, 0x5400))
        self.assertRaises(ub.UsbBootError, ub.split, 1 << 32)
        t = ub.UsbTransport(FakeDevice())
        self.assertRaises(ub.UsbBootError, ub.send, t, 0x80001002, b"1234")
        self.assertEqual(t.dev.calls, [])


class TestRunUboot(unittest.TestCase):
    """run-uboot: spl-usb2.bin as stage 1, U-Boot proper as stage 2."""

    def setUp(self):
        self.spl = usb2_image()
        self.uboot = uboot_bin()
        self.spl_path, self.ub_path = tmpfile(self.spl), tmpfile(self.uboot)

    def tearDown(self):
        os.unlink(self.spl_path)
        os.unlink(self.ub_path)

    def expected(self, stage1_timeout=20000):
        n, m = len(self.spl), len(self.uboot)
        return [
            ("ctrl", 0xC0, 0x00, 0, 0, 5, 1000),
            ("ctrl", 0x40, 0x01, 0x8000, 0x1000, None, 1000),
            ("ctrl", 0x40, 0x02, n >> 16, n & 0xFFFF, None, 1000),
            ("write", 0x01, self.spl, 10000),
            ("ctrl", 0x40, 0x03, 0, 0, None, 1000),
            # the status stage waits for the SPL's return (DDR test)
            ("ctrl", 0x40, 0x04, 0x8000, 0x1800, None, stage1_timeout),
            ("ctrl", 0x40, 0x01, 0x8010, 0x0000, None, 1000),
            ("ctrl", 0x40, 0x02, m >> 16, m & 0xFFFF, None, 1000),
            ("write", 0x01, self.uboot, 10000),
            ("ctrl", 0x40, 0x03, 0, 0, None, 1000),
            ("ctrl", 0x40, 0x05, 0x8010, 0x0000, None, 1000),
        ]

    def test_golden(self):
        r = Run(["run-uboot", self.spl_path, self.ub_path])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.dev.calls, self.expected())
        self.assertEqual(r.slept, [0.2])    # between the two stages
        self.assertEqual(r.closed, [r.dev])
        self.assertIn("boot ROM: 'X2600' (5 bytes: 5832363030)", r.out)
        self.assertIn("stage 1 done: DDR is up, the boot ROM is back", r.out)
        self.assertTrue(r.out[-1].startswith(
            "started: U-Boot proper, 70000 bytes at 0x80100000"))

    def test_timeouts(self):
        r = Run(["--timeout", "250", "--bulk-timeout", "3000", "run-uboot",
                 "--stage1-timeout", "5000", self.spl_path, self.ub_path])
        self.assertEqual(r.rc, 0, r.err)
        c = r.dev.calls
        self.assertEqual(c[5], ("ctrl", 0x40, 0x04, 0x8000, 0x1800, None,
                                5000))
        self.assertEqual([x[-1] for x in c if x[0] == "write"], [3000, 3000])
        self.assertTrue(all(x[-1] == 250 for i, x in enumerate(c)
                            if x[0] == "ctrl" and i != 5))
        r = Run(["run-uboot", "--stage1-timeout", "0", self.spl_path,
                 self.ub_path])
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertIn("--stage1-timeout must be positive", r.err[0])
        self.assertEqual(r.dev.calls, [])

    def test_stage1_never_returns(self):
        # the SPL hung (or never got to DDR): no U-Boot is sent
        r = Run(["run-uboot", self.spl_path, self.ub_path],
                FakeDevice(fail={ub.PROG_START1}))
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(r.dev.calls, self.expected()[:6])
        self.assertEqual(r.slept, [])
        self.assertIn("didn't come back to the boot ROM within 20000 ms",
                      r.err[0])
        self.assertIn("U-Boot wasn't sent", r.err[0])
        self.assertEqual(r.closed, [r.dev])

    def test_start2_unanswered(self):
        r = Run(["run-uboot", self.spl_path, self.ub_path],
                FakeDevice(fail={ub.PROG_START2}))
        self.assertEqual(r.rc, ub.EXIT_START_UNCONFIRMED)
        self.assertEqual(r.dev.calls, self.expected())  # everything went out
        self.assertIn("U-Boot went out", r.err[0])
        self.assertIn("watch the serial console", r.err[0])

    def test_stage2_bulk_fails(self):
        dev = FakeDevice()
        writes = []

        def write(endpoint, data, timeout=None):
            writes.append(len(data))
            dev.calls.append(("write", endpoint, bytes(data), timeout))
            if len(writes) == 2:
                raise FakeUsbError("[Errno 32] Pipe error")
            return len(data)
        dev.write = write
        r = Run(["run-uboot", self.spl_path, self.ub_path], dev)
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(r.dev.calls, self.expected()[:9])  # no PROG_START2
        self.assertIn("bulk OUT 0x01", r.err[0])

    def test_needs_the_stage2_tag(self):
        # spl-usb.bin, spl-mmc.img or a raw stage 1 would boot something
        # else instead of going back to the ROM: refused before any
        # transfer
        for data, words in ((usb_image(), "not spl-usb.bin"),
                            (mmc_image(), "not spl-mmc.img"),
                            (raw_image(9432), "no Ingenic SPL header")):
            p = tmpfile(data)
            try:
                r = Run(["run-uboot", p, self.ub_path])
            finally:
                os.unlink(p)
            self.assertEqual(r.rc, ub.EXIT_ERROR, words)
            self.assertFalse(hasattr(r, "opened"), words)
            self.assertEqual(r.dev.calls, [])
            self.assertIn(words, r.err[0])
            self.assertIn("spl-usb2.bin", r.err[0])

    def test_stage2_image_refused_by_run_spl(self):
        # it would wait in the ROM for a stage 2 that never comes
        r = Run(["run-spl", self.spl_path])
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(r.dev.calls, [])
        self.assertIn("this is spl-usb2.bin", r.err[0])
        self.assertIn("use run-uboot", r.err[0])

    def test_uboot_refusals(self):
        uimage = b"\x27\x05\x19\x56" + bytes(60) + self.uboot
        for data, words in ((uimage, "a uImage"),
                            (usb2_image(), "an SPL image"),
                            (b"", "not a u-boot.bin")):
            p = tmpfile(data)
            try:
                r = Run(["run-uboot", self.spl_path, p])
            finally:
                os.unlink(p)
            self.assertEqual(r.rc, ub.EXIT_ERROR, words)
            self.assertFalse(hasattr(r, "opened"), words)
            self.assertIn(words, r.err[0])
        r = Run(["run-uboot", self.spl_path, "/nonexistent/u-boot.bin"])
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertFalse(hasattr(r, "opened"))

    def test_dry_run_golden(self):
        r = Run(["--dry-run", "run-uboot", self.spl_path, self.ub_path],
                dev=False)
        self.assertEqual(r.rc, 0, r.err)
        n, m = len(self.spl), len(self.uboot)
        self.assertEqual(r.out, [
            "dry run: nothing is sent; the transfers, in order:",
            "GET_CPU_INFO  ctrl IN  bmRequestType=0xc0 bRequest=0x00 "
            "wValue=0x0000 wIndex=0x0000 wLength=5 timeout=1000ms",
            "SET_DATA_ADDR ctrl OUT bmRequestType=0x40 bRequest=0x01 "
            "wValue=0x8000 wIndex=0x1000 wLength=0 timeout=1000ms "
            "(address 0x80001000)",
            "SET_DATA_LEN  ctrl OUT bmRequestType=0x40 bRequest=0x02 "
            "wValue=0x0000 wIndex=0x%04x wLength=0 timeout=1000ms "
            "(%d bytes)" % (n, n),
            "data          bulk OUT ep=0x01 length=%d sha256=%s "
            "timeout=10000ms" % (n, hashlib.sha256(self.spl).hexdigest()),
            "FLUSH_CACHE   ctrl OUT bmRequestType=0x40 bRequest=0x03 "
            "wValue=0x0000 wIndex=0x0000 wLength=0 timeout=1000ms",
            "PROG_START1   ctrl OUT bmRequestType=0x40 bRequest=0x04 "
            "wValue=0x8000 wIndex=0x1800 wLength=0 timeout=20000ms "
            "(entry 0x80001800; stage 1 returns)",
            "SET_DATA_ADDR ctrl OUT bmRequestType=0x40 bRequest=0x01 "
            "wValue=0x8010 wIndex=0x0000 wLength=0 timeout=1000ms "
            "(address 0x80100000)",
            "SET_DATA_LEN  ctrl OUT bmRequestType=0x40 bRequest=0x02 "
            "wValue=0x%04x wIndex=0x%04x wLength=0 timeout=1000ms "
            "(%d bytes)" % (m >> 16, m & 0xFFFF, m),
            "data          bulk OUT ep=0x01 length=%d sha256=%s "
            "timeout=10000ms" % (m, hashlib.sha256(self.uboot).hexdigest()),
            "FLUSH_CACHE   ctrl OUT bmRequestType=0x40 bRequest=0x03 "
            "wValue=0x0000 wIndex=0x0000 wLength=0 timeout=1000ms",
            "PROG_START2   ctrl OUT bmRequestType=0x40 bRequest=0x05 "
            "wValue=0x8010 wIndex=0x0000 wLength=0 timeout=1000ms "
            "(entry 0x80100000)",
        ])
        self.assertFalse(hasattr(r, "opened"))  # no device opened
        self.assertEqual(r.slept, [])           # and no pause


class TestDryRun(unittest.TestCase):
    def test_golden(self):
        data = usb_image()
        p = tmpfile(data)
        try:
            r = Run(["--dry-run", "run-spl", p], dev=False)
        finally:
            os.unlink(p)
        self.assertEqual(r.rc, 0, r.err)
        n = len(data)
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(r.out, [
            "dry run: nothing is sent; the transfers, in order:",
            "GET_CPU_INFO  ctrl IN  bmRequestType=0xc0 bRequest=0x00 "
            "wValue=0x0000 wIndex=0x0000 wLength=5 timeout=1000ms",
            "SET_DATA_ADDR ctrl OUT bmRequestType=0x40 bRequest=0x01 "
            "wValue=0x8000 wIndex=0x1000 wLength=0 timeout=1000ms "
            "(address 0x80001000)",
            "SET_DATA_LEN  ctrl OUT bmRequestType=0x40 bRequest=0x02 "
            "wValue=0x0000 wIndex=0x%04x wLength=0 timeout=1000ms "
            "(%d bytes)" % (n, n),
            "data          bulk OUT ep=0x01 length=%d sha256=%s "
            "timeout=10000ms" % (n, sha),
            "FLUSH_CACHE   ctrl OUT bmRequestType=0x40 bRequest=0x03 "
            "wValue=0x0000 wIndex=0x0000 wLength=0 timeout=1000ms",
            "PROG_START1   ctrl OUT bmRequestType=0x40 bRequest=0x04 "
            "wValue=0x8000 wIndex=0x1800 wLength=0 timeout=1000ms "
            "(entry 0x80001800)",
        ])
        self.assertFalse(hasattr(r, "opened"))  # no device opened

    def test_info(self):
        r = Run(["--dry-run", "info"], dev=False)
        self.assertEqual(r.rc, 0)
        self.assertEqual(len(r.out), 2)
        self.assertIn("GET_CPU_INFO  ctrl IN  bmRequestType=0xc0", r.out[1])


class TestRefusals(unittest.TestCase):
    def run_bad(self, data, words):
        p = tmpfile(data)
        try:
            r = Run(["--dry-run", "run-spl", p], dev=False)
        finally:
            os.unlink(p)
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(len(r.out), 0)  # refused before any transfer
        self.assertIn(words, r.err[0])

    def test_too_small(self):
        self.run_bad(b"\0" * 100, "not a stage-1 image")
        self.run_bad(usb_image()[:0x803], "not a stage-1 image")

    def test_header_without_params(self):
        d = bytearray(usb_image())
        d[0x100:0x104] = b"XXXX"
        self.run_bad(bytes(d), "without the INGE params copy")

    def test_count_and_length(self):
        d = bytearray(usb_image())
        d[4] += 1
        self.run_bad(bytes(d), "block count")
        self.run_bad(usb_image() + b"\0", "block count")
        d = bytearray(usb_image())
        struct.pack_into("<I", d, 0x104, len(d) + 512)
        self.run_bad(bytes(d), "params length")

    def test_chainload(self):
        d = bytearray(usb_image())
        d[0x900:0x900 + len(ub.CHAINLOAD_MARKER)] = ub.CHAINLOAD_MARKER
        self.run_bad(bytes(d), "chainload-test SPL")

    def test_too_big(self):
        # past the 0x7000 bytes the ROM takes, with or without a header
        self.run_bad(raw_image(0x7001), "at most 28672")
        self.run_bad(usb_image(0x7000 - 0x800 + 1), "at most 28672")

    def test_largest_accepted(self):
        self.assertEqual(ub.check_image(usb_image(0x7000 - 0x800)), [])
        self.assertEqual(len(ub.check_image(raw_image(0x7000))), 1)

    def test_missing_file(self):
        r = Run(["run-spl", "/nonexistent/spl-usb.bin"])
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(r.dev.calls, [])

    def test_bad_timeout(self):
        r = Run(["--timeout", "0", "info"])
        self.assertEqual(r.rc, ub.EXIT_ERROR)
        self.assertEqual(r.dev.calls, [])

    @unittest.skipIf(importlib.util.find_spec("usb"),
                     "pyusb is installed here")
    def test_no_pyusb(self):
        out, err = [], []
        rc = ub.main(["info"], out=out.append, err=err.append)
        self.assertEqual(rc, ub.EXIT_ERROR)
        self.assertIn("pyusb isn't installed", err[0])


class TestImageLayout(unittest.TestCase):
    """The X2600 stage-1 addresses and the tag Kiln's SPL looks for."""

    def test_constants(self):
        self.assertEqual(ub.LOAD_ADDR, 0x80001000)
        self.assertEqual(ub.HEADER_SIZE, 0x800)
        self.assertEqual(ub.ENTRY_ADDR, 0x80001800)
        self.assertEqual(ub.STAGE1_MAX, 0x7000)
        self.assertEqual((ub.VID, ub.PID), (0xA108, 0xEAEF))
        # U-Boot proper's CONFIG_TEXT_BASE, in DDR (KSEG0), past stage 1
        self.assertEqual(ub.UBOOT_ADDR, 0x80100000)
        self.assertGreaterEqual(ub.UBOOT_ADDR, ub.LOAD_ADDR + ub.STAGE1_MAX)
        self.assertEqual(ub.UIMAGE_MAGIC, struct.pack(">I", 0x27051956))

    def test_tag(self):
        """Kiln's SPL reads the tag at 0x800017f0 as two LE words,
        0x4e4c494b "KILN" and 0x31425355 "USB1", or 0x32425355 "USB2" for
        stage 1 only (Kiln's U-Boot patch 0034)."""
        self.assertEqual(ub.LOAD_ADDR + ub.USB_TAG_OFF, 0x800017F0)
        self.assertEqual(struct.pack("<II", 0x4E4C494B, 0x31425355),
                         ub.USB_TAG)
        self.assertEqual(struct.pack("<II", 0x4E4C494B, 0x32425355),
                         ub.USB_TAG_STAGE2)
        # the soft-entry value: "SWB" << 8 | 2, Ingenic's softburn
        self.assertEqual(0x42575302, 0x425753 << 8 | 2)

    def test_layout(self):
        for size in (4, 10240, 0x5800 - 0x800 - 512):
            mmc, usb = mmc_image(size), usb_image(size)
            self.assertEqual(len(usb), len(mmc))
            self.assertEqual(len(usb) % 512, 0)
            self.assertLessEqual(len(usb), ub.STAGE1_MAX)
            self.assertEqual(usb[0x800:0x800 + size], spl_bin(size))
            diff = [i for i in range(len(usb)) if usb[i] != mmc[i]]
            self.assertEqual(diff, list(range(0x7F0, 0x7F8)))
            self.assertEqual(ub.check_image(usb), [])
            notes = ub.check_image(mmc)
            self.assertEqual(len(notes), 1)
            self.assertIn("spl-usb.bin", notes[0])
            usb2 = usb2_image(size)
            diff = [i for i in range(len(usb2)) if usb2[i] != usb[i]]
            self.assertEqual(diff, [0x7F7])     # "1" vs "2"
            self.assertEqual(ub.check_image(usb2, stage2=True), [])


@unittest.skipUnless(os.path.isfile(REAL_IMAGE),
                     "KILN_SPL_USB isn't set to an spl-usb.bin")
class TestRealImage(unittest.TestCase):
    """A real spl-usb.bin, e.g. from Kiln OS's u-boot/build.sh."""

    def test_check_and_dry_run(self):
        with open(REAL_IMAGE, "rb") as f:
            data = f.read()
        self.assertEqual(ub.check_image(data), [])
        r = Run(["--dry-run", "run-spl", REAL_IMAGE], dev=False)
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(len(r.out), 7)



@unittest.skipUnless(os.path.isfile(REAL_IMAGE2) and
                     os.path.isfile(REAL_UBOOT),
                     "KILN_SPL_USB2 and KILN_UBOOT aren't set to "
                     "spl-usb2.bin and u-boot.bin")
class TestRealUboot(unittest.TestCase):
    """A real spl-usb2.bin and u-boot.bin, e.g. from Kiln OS's
    u-boot/build.sh or a release's rescue kit (boot/)."""

    def test_check_and_dry_run(self):
        with open(REAL_IMAGE2, "rb") as f:
            spl = f.read()
        with open(REAL_UBOOT, "rb") as f:
            uboot = f.read()
        self.assertEqual(ub.check_image(spl, stage2=True), [])
        ub.check_uboot(uboot)
        r = Run(["--dry-run", "run-uboot", REAL_IMAGE2, REAL_UBOOT],
                dev=False)
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(len(r.out), 12)


if __name__ == "__main__":
    unittest.main()
