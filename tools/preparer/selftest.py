#!/usr/bin/env python3
"""Checks for the parts of the card write that do not need the card.

Run it anywhere: `python3 selftest.py`.

The Preparer writes SD cards on three operating systems and can only be run on one of
them at a time, so the pieces most likely to corrupt a card silently -- sector
arithmetic, device-name matching, partition classification -- are written as functions
that take data and return data, and are checked here against tables. That convention
already existed for the Wi-Fi and disk parsers; this extends it across the write.

**What this does not do is prove a card boots.** It cannot, on any platform. It is the
floor, not the ceiling: it catches the arithmetic, and hardware catches the rest.
"""
import io
import subprocess
import sys

import hostos
from hostos import linux, windows

FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append("%s\n     got  %r\n     want %r" % (name, got, want))


def group(title):
    print("\n%s" % title)


# ───────────────────────────── Device identifiers ─────────────────────────────
# The last thing standing between a typo and somebody's root disk, so both halves
# matter: what it accepts, and what it refuses.

def test_device_ids():
    group("device identifiers")
    from hostos import darwin
    table = [
        (darwin, "disk4", True), (darwin, "disk11", True),
        (darwin, "disk4s1", False), (darwin, "rdisk4", False),
        (darwin, "/dev/disk4", False), (darwin, "", False), (darwin, "sdb", False),

        (linux, "sdb", True), (linux, "mmcblk0", True), (linux, "nvme0n1", True),
        (linux, "sdb1", False), (linux, "mmcblk0p1", False), (linux, "/dev/sdb", False),
        (linux, "sd", False), (linux, "", False), (linux, "disk4", False),

        (windows, r"\\.\PHYSICALDRIVE2", True), (windows, r"\\.\physicaldrive2", True),
        (windows, r"\\.\PHYSICALDRIVE", False), (windows, "C:", False),
        (windows, r"\\.\PHYSICALDRIVE2 ", False), (windows, "", False),
    ]
    for mod, dev, want in table:
        check("%s.valid_device_id(%r)" % (mod.NAME, dev), mod.valid_device_id(dev), want)
    print("  %d identifiers" % len(table))


# ───────────────────────────── Partition naming ─────────────────────────────
# sdb1 but mmcblk0p1: a name already ending in a digit takes a `p`. Getting this wrong
# on a card in a native reader -- the exact hardware this tool is for -- provisions a
# device that is not there.

def test_partition_names():
    group("partition names")
    table = [("sdb", "/dev/sdb1"), ("sdc", "/dev/sdc1"),
             ("mmcblk0", "/dev/mmcblk0p1"), ("nvme0n1", "/dev/nvme0n1p1")]
    for dev, want in table:
        check("linux.partition_devices(%r)" % dev,
              linux.partition_devices(dev, 1), [want])
    from hostos import darwin
    check("darwin.partition_devices('disk4')", darwin.partition_devices("disk4", 1),
          ["/dev/rdisk4s1", "/dev/disk4s1"])
    check("windows.partition_devices", windows.partition_devices(r"\\.\PHYSICALDRIVE2", 1),
          [])
    print("  %d layouts" % (len(table) + 2))


# ───────────────────────────── Which mounts are ours ─────────────────────────────
# /dev/sdb owns /dev/sdb1. It does not own /dev/sdbb1, and unmounting that would mean
# yanking a second card out from under whatever is using it.

def test_mount_matching():
    group("mount matching")
    text = (
        "sysfs /sys sysfs rw 0 0\n"
        "/dev/sda2 / ext4 rw 0 0\n"
        "/dev/sdb1 /media/jack/boot vfat rw 0 0\n"
        "/dev/sdb2 /media/jack/boot/nested ext4 rw 0 0\n"
        "/dev/sdbb1 /media/jack/OTHER-CARD vfat rw 0 0\n"
        "/dev/mmcblk0p1 /media/jack/card vfat rw 0 0\n"
        "/dev/sdb10 /media/jack/with\\040space vfat rw 0 0\n"
    )
    # Nested mounts first, so a child is released before its parent.
    check("sdb", linux.parse_mounts(text, "sdb"),
          ["/media/jack/boot/nested", "/media/jack/with space", "/media/jack/boot"])
    check("sdbb (not sdb's)", linux.parse_mounts(text, "sdbb"),
          ["/media/jack/OTHER-CARD"])
    check("mmcblk0", linux.parse_mounts(text, "mmcblk0"), ["/media/jack/card"])
    check("sda", linux.parse_mounts(text, "sda"), ["/"])
    check("sdz (nothing)", linux.parse_mounts(text, "sdz"), [])
    print("  5 cases, including the /dev/sdbb1 trap and an escaped space")


# ───────────────────────────── The Windows command line ─────────────────────────────

def test_cmdline():
    group("windows command line")
    cases = [
        [r"C:\Program Files\Riparr\writer.py"],
        ["C:\\dir\\"],
        ["--dev", r"\\.\PHYSICALDRIVE2"],
        ["a b", 'c"d', "e\\\\f"],
        ["--sha256", ""],
    ]
    for c in cases:
        check("cmdline(%r)" % c, windows.cmdline(c), subprocess.list2cmdline(c))
    print("  %d command lines, against subprocess.list2cmdline" % len(cases))


# ───────────────────────────── Sector arithmetic ─────────────────────────────
# The Windows sink is the only one of the three that does its own block handling, and it
# is the one that cannot be tried out here. Every byte handed to it must reach the card
# in order, and the tail must be padded up to a whole sector -- a short final write is
# rejected outright on an unbuffered handle, which would fail the write at 99%.

def test_sink_blocking():
    group("windows sector arithmetic")

    class Fake(windows._WinSink):
        def __init__(self):
            self.buf = bytearray()
            self.err = ""
            self.handle = 1
            self.scratch = None
            self.out = bytearray()

        def _emit(self, data):
            assert len(data) % windows.SECTOR == 0, "unaligned write of %d" % len(data)
            self.out += data

        def _teardown(self):
            pass

    # close() also flushes the handle and asks Windows to re-read the partition table.
    # Neither exists here, so kernel32 is stubbed for the duration -- the point of this
    # test is the arithmetic, and stubbing is what lets it run at all off-platform.
    class FakeK32:
        def FlushFileBuffers(self, handle):
            return 1

    real_k32, real_ioctl = windows._k32, windows._ioctl
    windows._k32 = lambda: FakeK32()
    windows._ioctl = lambda *a, **k: b""
    try:
        _run_sink_cases(Fake)
    finally:
        windows._k32, windows._ioctl = real_k32, real_ioctl
    print("  6 chunk patterns, each byte-compared end to end")


def _run_sink_cases(Fake):
    for label, chunks in [
        ("4 MiB chunks, exact", [b"A" * (4 << 20)] * 3),
        ("ragged chunks", [b"B" * 100003] * 40),
        ("one short write", [b"C" * 17]),
        ("nothing at all", []),
        ("exactly one sector", [b"D" * windows.SECTOR]),
        ("a sector minus one", [b"E" * (windows.SECTOR - 1)]),
    ]:
        s = Fake()
        for c in chunks:
            s.write(c)
        rc, err = s.close()
        source = b"".join(chunks)
        pad = (-len(source)) % windows.SECTOR
        check("%s: rc" % label, (rc, err), (0, ""))
        check("%s: bytes reach the card in order" % label,
              bytes(s.out), source + b"\x00" * pad)


# ───────────────────────────── Image classification ─────────────────────────────
# Which provisioning route a written card takes. Reading this wrong means a card that
# boots and does nothing -- which is how the first night of this project was lost.

def test_partition_layout():
    group("image classification")
    import writer

    def mbr(types):
        b = bytearray(512)
        for i, t in enumerate(types):
            b[0x1BE + 16 * i + 4] = t
        b[510:512] = b"\x55\xaa"
        return bytes(b)

    table = [
        ("Raspberry Pi (FAT32 LBA + Linux)", [0x0c, 0x83], ("fat-boot", 1)),
        ("Raspberry Pi (FAT32 CHS)", [0x0b, 0x83], ("fat-boot", 1)),
        ("Armbian / Allwinner (one ext4)", [0x83], ("single-ext4", 1)),
        ("ext4 plus a second partition", [0x83, 0x83], ("unknown", None)),
        ("an empty table", [], ("unknown", None)),
    ]
    real = hostos.open_reader
    try:
        for label, types, want in table:
            data = mbr(types)
            hostos.open_reader = lambda dev, _d=data: io.BytesIO(_d)
            writer.hostos.open_reader = hostos.open_reader
            check(label, writer._partition_layout("disk9"), want)
        # A device that will not open at all must classify as unknown, not crash.
        def boom(dev):
            raise OSError(5, "Input/output error")
        hostos.open_reader = boom
        writer.hostos.open_reader = boom
        check("an unreadable card", writer._partition_layout("disk9"), ("unknown", None))
    finally:
        hostos.open_reader = real
        writer.hostos.open_reader = real
    print("  %d layouts" % (len(table) + 1))


# ───────────────────────────── The platform contract ─────────────────────────────
# hostos/__init__.py promises that adding a platform means adding one module. This is
# what makes that promise checkable rather than aspirational.

CONTRACT = [
    "CAN_WRITE", "PROVISION_IN_IMAGE", "valid_device_id", "block_device", "raw_device", "partition_devices",
    "unmount_disk", "probe_writable", "open_sink", "open_reader", "flush",
    "rescan_partitions", "mount_boot", "eject", "explain_write_error", "elevate",
    "list_block_devices", "scan_wifi", "saved_network_password",
    "open_url", "reveal", "keep_awake_command", "NAME",
]


def test_contract():
    group("platform contract")
    import importlib
    for name in ("darwin", "linux", "windows", "unsupported"):
        mod = importlib.import_module("hostos." + name)
        missing = [f for f in CONTRACT if not hasattr(mod, f)]
        check("hostos.%s implements the contract" % name, missing, [])
    print("  %d names across 4 backends" % len(CONTRACT))


# ───────────────────────────── Raspberry Pi first boot ─────────────────────────────
# Trixie dropped custom.toml for cloud-init, and a card whose seed files are wrong boots
# to a stock image with nothing saying why. What can be checked without a Pi is that the
# Preparer's own settings come out the other end intact -- the awkward SSID, the hash,
# the key going to root -- and that writing a card twice does not stack cmdline tokens.
# The files were also validated against the image's own cloud-init 25.2 schema when this
# was written (see rpi_cloudinit.py); that needs cloud-init itself, so it is not here.

def test_cloud_init():
    group("raspberry pi first boot (cloud-init)")
    import os
    import tempfile
    import core
    import rpi_cloudinit as CI
    import writer
    ssid = 'Mike\'s "Wi-Fi" \\ caf\u00e9'
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample riparr-preparer"
    toml = core.build_toml({
        "hostname": "riparr", "user": "riparr", "pw_hash": "$6$salt$Zx/y.hash",
        "ssid": ssid, "secure": True, "wifi_pw": "hunter22hunter", "hidden": True,
        "country": "gb", "timezone": "America/Chicago", "keymap": "us",
        "authorized_key": key})
    c = CI.parse_custom_toml(toml)
    check("the SSID survives custom.toml", c["ssid"], ssid)
    check("the hash survives", c["pw_hash"], "$6$salt$Zx/y.hash")
    check("the PSK is the derived key, not the passphrase", len(c["psk"]), 64)
    check("the country is upper-cased", c["country"], "GB")
    check("the key is read", c["authorized_keys"], [key])

    seed = CI.build(toml, now_ms=7)
    ud, nc = seed["user-data"], seed["network-config"]
    check("user-data is a cloud-config", ud.splitlines()[0], "#cloud-config")
    check("root gets the key without a refusal", "disable_root: false" in ud, True)
    check("sshd is started", "[ systemctl, enable, --now, ssh ]" in ud, True)
    check("the SSID is YAML-escaped", '"Mike\'s \\"Wi-Fi\\" \\\\ caf\u00e9":' in nc, True)
    check("hidden is carried", "hidden: true" in nc, True)
    check("instance id", seed["meta-data"], "instance-id: riparr-7\n")

    open_net = CI.build(core.build_toml({
        "hostname": "r", "user": "r", "pw_hash": "x", "ssid": "Cafe", "secure": False,
        "country": "US", "authorized_key": key}))["network-config"]
    check("an open network says so", "key-management: none" in open_net, True)

    base = "console=tty1 root=PARTUUID=4d8fd085-02 rootwait resize\n"
    once = CI.patch_cmdline(base, "riparr-1", "GB")
    twice = CI.patch_cmdline(once, "riparr-2", "US")
    check("cmdline keeps what was there", once.split()[:4], base.split())
    check("writing twice leaves one of each", twice,
          "console=tty1 root=PARTUUID=4d8fd085-02 rootwait resize "
          "cfg80211.ieee80211_regdom=US ds=nocloud;i=riparr-2\n")

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "cmdline.txt"), "w") as f:
        f.write(base)
    check("the writer reports every file read back", writer._write_cloud_init(d, toml), None)
    check("and all four are on the card",
          sorted(n for n in os.listdir(d)),
          ["cmdline.txt", "meta-data", "network-config", "user-data"])
    print("  settings round-trip, open and hidden networks, idempotent cmdline")


def test_boards():
    group("board registry")
    import boards
    ids = [b["id"] for b in boards.BOARDS]
    check("ids are unique", len(ids), len(set(ids)))
    check("the default is the tested board", boards.get(boards.default_id())["tier"],
          "verified")
    pis = [b for b in boards.BOARDS if b["os"] == "raspios"]
    check("every Pi uses the one Raspberry Pi OS image",
          {boards.image_source(b["id"])["url"] for b in pis}, {boards.RASPIOS_URL})
    for b in boards.BOARDS:
        check("%s has bands it can name" % b["id"],
              set(b.get("bands", ["2.4", "5"])) <= {"2.4", "5", "6"}, True)
    check("the 2.4 GHz-only boards say so",
          sorted(b["id"] for b in boards.BOARDS if b.get("bands") == ["2.4"]),
          ["raspberrypi3b", "raspberrypizero2w"])
    print("  %d boards, %d of them Raspberry Pi" % (len(ids), len(pis)))


def test_forget_host_keys():
    group("forgetting an old card's host key")
    import os, shutil, tempfile
    import core
    if not shutil.which("ssh-keygen"):
        print("  skipped: no ssh-keygen on this host")
        return
    d = tempfile.mkdtemp()
    try:
        f = os.path.join(d, "known_hosts")
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
        with open(f, "w") as fh:
            fh.write("riparr.local %s\nnas.local %s\n" % (key, key))
        check("the box's names", core.host_key_names(" Riparr "), ["riparr", "riparr.local"])
        check("the old card is forgotten",
              core.forget_host_keys(f, core.host_key_names("riparr")), 1)
        check("someone else's host is kept", "nas.local" in open(f).read(), True)
        check("forgetting twice is harmless",
              core.forget_host_keys(f, core.host_key_names("riparr")), 0)
        check("a missing file is harmless",
              core.forget_host_keys(os.path.join(d, "nope"), ["x"]), 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_remote_target():
    group("existing-host target validation")
    import finish
    base = {"existing_host": True, "user": "ubuntu", "password": " a'$ \\",
            "hostname": "vm.example.lan"}
    for host in ("vm.example.lan", "vm.local", "192.168.10.2", "vm"):
        target = finish.validate_target(dict(base, hostname=host))
        check("exact target %s" % host, target["hostname"], host)
        check("password is not trimmed", target["password"], base["password"])
        check("default ports", (target["ssh_port"], target["port"]), (22, 9797))
    for changes in ({"hostname": "http://vm"}, {"hostname": "user@vm"},
                    {"hostname": "vm;id"}, {"hostname": "256.1.1.1"},
                    {"hostname": "::1"}, {"hostname": "-vm"},
                    {"ssh_port": 0}, {"port": 65536}, {"port": "no"},
                    {"port": 22.5}, {"port": True},
                    {"password": ""}, {"password": "a\nb"}, {"user": ""}):
        try:
            finish.validate_target(dict(base, **changes))
        except ValueError:
            continue
        check("invalid target refused: %r" % changes, True, False)
    check("card config remains unchanged", finish.validate_target({"hostname": "riparr"}),
          {"hostname": "riparr"})

    from unittest.mock import patch
    for uid, sudo_rc in (("0", 0), ("1000", 0), ("1000", 1)):
        finisher = finish.Finisher(base)
        calls = []
        def command(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return ((sudo_rc, "") if cmd.startswith("sudo -n") else (0, uid))
        with patch.object(finisher, "_password_command", side_effect=command):
            finisher._preflight_existing()
        check("root/sudo selection", finisher.sudo, uid != "0")
        check("password sudo only when needed", finisher.sudo_password,
              uid != "0" and sudo_rc != 0)
        finisher._say("secret: " + base["password"])
        check("password redacted from logs", base["password"] in finisher.log[-1], False)
        check("config has no password", "password" in finisher.cfg, False)
    finisher = finish.Finisher(base)
    with patch.object(finish, "_port_open", return_value=True) as probe, \
            patch.object(finisher, "_sweep", side_effect=AssertionError("no sweep")):
        finisher.step_find()
        check("exact remote address", finisher.address, base["hostname"])
        check("SSH port probed", probe.call_args.args, (base["hostname"], 22))

    from unittest.mock import Mock
    import os, tempfile, bridge
    with tempfile.TemporaryDirectory() as assets:
        api = bridge.Bridge.__new__(bridge.Bridge)
        api.assets, api.setup_thread, api.last_seen = assets, None, {}
        api.setup_path, api.nosleep = os.path.join(assets, "progress.json"), Mock()
        with patch.object(bridge.core, "payload_ok", return_value=(True, [])), \
                patch.object(bridge.threading, "Thread"):
            result = api.start_setup(dict(base, remote_user="ubuntu", update_only=True))
            check("no card key needed", result["ok"], True)
            instance = api.finisher
            assert isinstance(instance, finish.Finisher)
            check("update action forwarded without a card key", instance.update_only, True)
            check("separate host trust file", instance.known_hosts,
                  os.path.join(assets, "known_hosts_existing"))
            check("progress contains no password", base["password"] in
                  open(api.setup_path).read(), False)
            instance._cleanup_existing()
            check("password cleared on cleanup", instance.password, None)
            api.setup_thread = None
            check("card path still requires its key",
                  api.start_setup({"hostname": "riparr"})["ok"], False)
        finisher = finish.Finisher(dict(base, known_hosts=os.path.join(assets, "hosts")))
        channel = Mock()
        channel.recv_ready.side_effect = [True, False]
        channel.recv.return_value = b"ready\n"
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        finisher.client = Mock()
        finisher.client.get_transport.return_value.open_session.return_value = channel
        finisher.sudo = finisher.sudo_password = True
        finisher._password_command("printf ready", privileged=True)
        check("sudo secret sent only on stdin", channel.sendall.call_args.args,
              ((base["password"] + "\n").encode("utf-8"),))
        check("command contains no password", base["password"] in
              channel.exec_command.call_args.args[0], False)
        channel.close.assert_called_once()
        class ChangedKey(Exception):
            pass
        class BadPassword(Exception):
            pass
        client = Mock()
        client.connect.side_effect = ChangedKey()
        module = Mock(SSHClient=Mock(return_value=client), BadHostKeyException=ChangedKey,
                      AuthenticationException=BadPassword)
        with patch.dict(sys.modules, paramiko=module):
            try:
                finisher._connect_existing()
            except finish.StepFailed as error:
                check("changed host key refused", error.step, "connect")
            else:
                check("changed key must not be accepted", True, False)
        client.save_host_keys.assert_not_called()
        finisher._cleanup_existing()


def test_update_routing():
    group("fresh setup versus application update")
    import finish
    from contextlib import ExitStack
    from unittest.mock import patch
    from types import SimpleNamespace
    for existing in (False, True):
        for update in (False, True):
            cfg = {"hostname": "vm.example.lan" if existing else "riparr",
                   "existing_host": existing, "update_only": update,
                   "user": "ubuntu", "password": "test-password", "port": 9876}
            finisher = finish.Finisher(cfg)
            finisher.address = cfg["hostname"]
            calls = []
            with ExitStack() as stack:
                for key in ("find", "connect", "copy", "bootstrap", "install", "verify"):
                    def step(key=key):
                        calls.append(key)
                        finisher._finish_step(key)
                    stack.enter_context(patch.object(finisher, "step_" + key, side_effect=step))
                stack.enter_context(patch.object(finisher, "_cleanup_existing"))
                check("setup/update finishes", finisher.run(), True)
            check("bootstrap runs only for fresh setup", "bootstrap" in calls, not update)
            check("progress omits skipped bootstrap", "bootstrap" in finisher.state, not update)
            check("progress completes", finisher._pct(), 100.0)
            check("target address retained", finisher.address, cfg["hostname"])
            if not existing:
                result = SimpleNamespace(returncode=0, stdout="root\nriparr\nLinux\n", stderr="")
                with patch.object(finisher, "_forget_host_key") as forget, \
                        patch.object(finish.subprocess, "run", return_value=result):
                    finisher.step_connect()
                    check("update keeps the remembered host key", forget.called, not update)
            with patch.object(finisher, "_password_command", return_value=(0, "")) as password, \
                    patch.object(finisher, "_run_remote", return_value=0) as key_command:
                finisher.step_install()
                command = (password if existing else key_command).call_args.args[0]
                check("installer update flag", "RIPARR_UPDATE_ONLY=1" in command, update)
                if existing and update:
                    check("update does not request a new port", "RIPARR_PORT=" in command, False)


def main():
    print("Riparr Preparer selftest — host is %s, card writing %s"
          % (hostos.NAME, "supported" if hostos.CAN_WRITE else "not supported"))
    for fn in (test_contract, test_device_ids, test_partition_names,
               test_mount_matching, test_cmdline, test_sink_blocking,
               test_partition_layout, test_cloud_init, test_boards,
               test_forget_host_keys, test_remote_target, test_update_routing):
        fn()
    print()
    if FAILED:
        print("FAILED (%d):\n" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
