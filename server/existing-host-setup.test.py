#!/usr/bin/env python3
"""Focused existing-host setup checks. Run: python server/existing-host-setup.test.py."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
temporary = tempfile.TemporaryDirectory(prefix="riparr-host-test-")
os.environ["RIPARR_DB"] = os.path.join(temporary.name, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db, main, makemkv as MK, platform as P


def test_directory_transport():
    from riparr import shares as SH
    library = Path(temporary.name) / "library"
    library.mkdir()
    marker = library / ".riparr-library"
    marker.touch()
    source = Path(temporary.name) / "sample.bin"
    source.write_bytes(b"disc data" * 1024)
    share = {"backend": "directory", "path": str(library)}
    destination = SH.transport(share)
    assert isinstance(destination, SH.DirectoryTransport)
    assert destination.reachable()
    assert destination.put(str(source), "Movies/Film.mkv")["ok"]
    assert destination.size("Movies/Film.mkv") == source.stat().st_size
    assert SH.verify_remote(destination, "Movies/Film.mkv", str(source))["ok"]
    assert destination.tree_sizes("Movies") == {"Film.mkv": source.stat().st_size}
    for invalid in ("../outside", "/absolute", "folder/../../outside"):
        try:
            destination.describe(invalid)
        except SH.BadPath:
            pass
        else:
            raise AssertionError("A path escaped the library")
    outside = Path(temporary.name) / "outside"
    outside.mkdir()
    (library / "escape").symlink_to(outside, target_is_directory=True)
    try:
        destination.describe("escape/file")
    except SH.BadPath:
        pass
    else:
        raise AssertionError("A symlink escaped the library")
    destination.delete("Movies/Film.mkv")
    assert destination.size("Movies/Film.mkv") is None
    marker.unlink()
    assert not destination.reachable()
    try:
        destination.put(str(source), "Movies/unavailable.mkv")
    except SH.BadPath:
        pass
    else:
        raise AssertionError("An unavailable library accepted a write")
    assert not (library / "Movies/unavailable.mkv").exists()
    from riparr import rip
    marker.touch()
    with patch.object(P, "CONTAINER", True), patch.object(P, "LIBRARY_MOUNT", str(library)):
        assert main.directory_library_verify(user="test")["verified"]
        destination = db.destination("movie")[0]
        assert destination["backend"] == "directory"
        assert P.library_mounted()
        assert rip._library_root("movie") == str(library)
        assert isinstance(SH.transport(destination), SH.DirectoryTransport)
        marker.unlink()
        assert not P.library_mounted()
        assert not MK.can_install()[0]
        assert not MK.start_install(False)["ok"]
        from riparr import updater
        assert not updater.install()["ok"]
        assert not P.power_action("reboot")[0]
        assert not P.provision_bridge_available()
        assert P._makemkv_eula_accepted()
    db.set("directory_library_verified", None)


def test_directory_pipeline():
    from riparr import rip
    library = Path(temporary.name) / "pipeline-library"
    staging = Path(temporary.name) / "pipeline-staging"
    library.mkdir()
    staging.mkdir()
    (library / ".riparr-library").touch()
    disc = dict(P._MOCK_DISCS["bluray"], size_bytes=64 * 2**20)
    with patch.object(P, "CONTAINER", True), patch.object(P, "LIBRARY_MOUNT", str(library)), \
            patch.object(P, "STAGING", str(staging)), patch.object(rip, "STAGING", str(staging)), \
            patch.object(rip, "MOCK_FAST", True), patch.dict(P._MOCK_DISCS, bluray=disc), \
            patch.dict(os.environ, RIPARR_MOCK_DISC="bluray", RIPARR_MOCK_CONTENT="movie"):
        db.set("directory_library_verified", 123)
        db.set("setup_complete", True)
        db.set("verify_mode", "quick")
        db.set("on_unknown_disc", "label")
        for output in ("mkv", "backup"):
            for mode in ("burst", "direct"):
                db.set("rip_mode", output)
                db.set("transfer_mode", mode)
                job_id = db.create_job(state="queued", disc_label="DIRECTORY_TEST", kind="movie",
                                       fingerprint="directory-%s-%s" % (output, mode))
                rip._run_job(db.get_job(job_id))
                job = db.get_job(job_id)
                if job["state"] == "transferring":
                    rip._send_one(job)
                job = db.get_job(job_id)
                assert job["state"] == "done", job
                assert (library / job["remote_name"]).exists()
        assert list(library.rglob("*.mkv"))
        assert list(library.rglob("*.m2ts"))
        from riparr import shares
        try:
            rip._library_path("movie", "../outside")
        except shares.BadPath:
            pass
        else:
            raise AssertionError("A direct write escaped the library")
    db.set("directory_library_verified", None)
    db.set("rip_mode", "mkv")
    db.set("transfer_mode", "burst")


def test_storage_wording():
    with patch.object(db, "active_job", return_value=None), \
            patch.object(P, "card_speed", return_value={"write_mbs": 10, "read_mbs": 20}), \
            patch.object(P, "library_status", return_value={"mounted": True}):
        for container in (False, True):
            with patch.object(P, "CONTAINER", container):
                result = main.storage_speedtest(main.SpeedTest(), user="test")
                assert result["recommend"] == (None if container else "direct")
                assert ("staging storage" in result["why"]) is container
                capacity = main._capacity(main.WINDOW_BYTES * 2, direct=True)
                assert capacity["mode"] == "direct"
                assert ("the card isn't the limit" in capacity["phrase"]) is not container
                assert ("available library space is the limit" in capacity["phrase"]) is container


def test_production_mode():
    with patch.object(P.os.path, "exists", return_value=False):
        for value, expected in (("1", True), ("0", False)):
            with patch.dict(os.environ, RIPARR_APPLIANCE=value):
                assert P._is_appliance() is expected
    unit = os.path.join(os.path.dirname(__file__), "..", "packaging", "riparr.service")
    with open(unit) as source:
        config = source.read()
    assert "Environment=RIPARR_APPLIANCE=1" in config
    assert "EnvironmentFile=-/etc/default/riparr" in config


def test_web_tool_bridge():
    request = os.path.join(temporary.name, "makemkv.request")
    with patch.object(P, "IS_APPLIANCE", True), \
            patch.object(MK.os, "geteuid", return_value=1000), \
            patch.object(MK, "bridge_available", return_value=True), \
            patch.object(MK, "_root_installer_current", return_value=True), \
            patch.object(MK, "installing", return_value=False), \
            patch.object(MK, "REQUEST", request):
        assert not MK.start_install(False)["ok"]
        assert not os.path.exists(request)
        assert MK.start_install(True)["ok"]
        assert os.path.exists(request)


def test_drive_indexes():
    transcript = ('DRV:5,2,999,12,"Virtual CD-ROM","Installer","/dev/sr0"\n'
                  'DRV:3,2,999,12,"USB, optical drive","Film","/dev/sr1"\n'
                  'DRV:9,256,999,0,"","",""\n')
    result = SimpleNamespace(stdout=transcript)
    with patch.object(P, "MOCK", False), patch.object(MK, "_drive_map", None), \
            patch.object(MK, "_drive_topology", return_value=("connected",)), \
            patch.object(MK.subprocess, "run", return_value=result) as scan:
        assert MK.disc_source("/dev/sr1") == "disc:3"
        assert MK.disc_source("/dev/sr0") == "disc:5"
        assert scan.call_count == 1
        assert "--noscan" in scan.call_args.args[0]
        try:
            MK.disc_source("/dev/sr2")
        except RuntimeError:
            pass
        else:
            raise AssertionError("An unmapped drive fell back to disc:0")


def test_drive_selection():
    virtual = {"id": "virtual", "device": "/dev/sr0", "present": True}
    physical = {"id": "usb-serial", "device": "/dev/sr1", "present": True}
    db.set("optical_drive", None)
    assert P._selected_drive([physical]) == physical
    assert P._selected_drive([virtual, physical]) is None
    db.set("optical_drive", "usb-serial")
    assert P._selected_drive([virtual, physical]) == physical
    assert P._selected_drive([virtual]) is None
    reconnected = dict(physical, device="/dev/sr3")
    assert P._selected_drive([virtual, reconnected])["device"] == "/dev/sr3"
    with patch.object(P, "MOCK", False), \
            patch.object(P, "optical_drives", return_value=[physical]), \
            patch.object(P.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stderr="", stdout="")) as command:
        assert P.eject()["ok"]
        assert command.call_args.args[0] == ["eject", "/dev/sr1"]
        assert P.close_tray(wait=0)[0]
        assert command.call_args.args[0] == ["eject", "-t", "/dev/sr1"]
        command.reset_mock()
        assert not P.eject("/dev/sr0")["ok"]
        command.assert_not_called()
    db.set("optical_drive", None)


def test_drive_inventory():
    devices = ["/dev/sr0", "/dev/sr1"]
    def paths(pattern):
        return list(devices) if pattern == "/dev/sr*" else []
    db.set("optical_drive", "usb")
    with patch.object(P, "MOCK", False), patch.object(P, "_glob", side_effect=paths), \
            patch.object(P, "_read", return_value="generic"), \
            patch.object(P, "_optical_identity", side_effect=lambda path:
                         "usb" if path == "/dev/sr1" else "virtual"), \
            patch.object(P.OPT, "capabilities", return_value={"profiles": [16], "dvd": True}), \
            patch.object(P.OPT, "tray_status", return_value=(False, "empty")), \
            patch.object(P.os, "access", return_value=False), \
            patch.object(P, "_disc_cache", {}), patch.object(P, "_caps_cache", {}), \
            patch.object(P, "usb_host_ports", return_value={"host": [], "peripheral": []}):
        assert P.optical_drives()[0]["device"] == "/dev/sr1"
        assert "permissions" in P.optical_diagnosis()["hint"]
        with patch.object(P, "makemkv_status", return_value={"installed": False}):
            drive_check = next(item for item in main._autorip_state()["checks"]
                               if item["what"] == "A drive to read them in")
            assert drive_check["state"] == "fail"
        P._disc_cache["/dev/sr1"] = ("usb", {})
        devices.remove("/dev/sr1")
        assert P.optical_drives() == []
        assert "/dev/sr1" not in P._disc_cache
        assert len(P.optical_diagnosis()["available"]) == 1
    db.set("optical_drive", None)


def test_installer_controls():
    root = Path(__file__).resolve().parent.parent
    for script in ("bootstrap.sh", "install.sh", "makemkv-install.sh"):
        subprocess.run(["bash", "-n", str(root / "tools" / script)], check=True)
    sandbox = Path(temporary.name) / "shell"
    binaries = sandbox / "bin"
    binaries.mkdir(parents=True)
    (sandbox / "validation").mkdir()
    log = sandbox / "commands"
    for name in ("id", "sudo", "apt-get", "mkdir", "tee", "systemctl", "journalctl",
                 "dmesg", "systemd-tmpfiles", "swapon"):
        script = '#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$RIPARR_TEST_LOG"\n'
        script += ("echo 0\n" if name == "id" else "exit 97\n" if name == "sudo"
                   else "cat >/dev/null\n" if name == "tee" else "exit 0\n")
        executable = binaries / name
        executable.write_text(script)
        executable.chmod(0o755)
    env = dict(os.environ, HOME=str(sandbox), PATH=str(binaries) + os.pathsep + os.environ["PATH"],
               RIPARR_TEST_LOG=str(log), RIPARR_PRESERVE_SWAP="1")
    result = subprocess.run(["bash", str(root / "tools/bootstrap.sh")], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "journald.conf.d/99-riparr.conf" in commands
    assert "zram-tools" not in commands and "zramswap" not in commands
    assert "sudo " not in commands
    assert "Existing swap configuration left unchanged" in result.stdout
    mkv = (root / "tools/makemkv-install.sh").read_text()
    helper = re.search(r"(?ms)^as_root\(\) \{.*?^\}", mkv).group(0)
    result = subprocess.run(["bash"], input=helper + "\nas_root printf root-ok", env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == "root-ok"
    assert "sudo make install" not in mkv and "as_root make install" in mkv
    assert "DPkg::Lock::Timeout=1200" in mkv
    config = sandbox / "riparr.conf"
    config.write_text("UNRELATED=keep\n")
    installer = (root / "tools/install.sh").read_text().split('say "Installing Riparr"', 1)[0]
    installer = installer.replace("SYSTEM_CONFIG=/etc/default/riparr", "SYSTEM_CONFIG=" + str(config))
    for port in ("8877", "", "8888"):
        result = subprocess.run(["bash"], input=installer + '\nprintf "%s" "$PORT"',
                                env=dict(env, RIPARR_PORT=port), capture_output=True,
                                text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout == (port or "8877")
    assert "UNRELATED=keep" in config.read_text()
    assert config.read_text().count("RIPARR_PORT=") == 1


def test_application_update():
    root = Path(__file__).resolve().parent.parent
    sandbox = Path(temporary.name) / "update"
    binaries = sandbox / "bin"
    source = sandbox / "payload"
    installed = sandbox / "opt"
    data = sandbox / "data"
    for directory in (binaries, source / "tools", source / "packaging", source / "server/riparr",
                      installed / "server/riparr", installed / ".venv/bin", data / ".MakeMKV"):
        directory.mkdir(parents=True)
    log = sandbox / "commands"
    for name in ("id", "getent", "systemctl", "curl", "chown", "apt-get", "useradd",
                 "usermod", "groupadd", "modprobe", "udevadm", "sysctl"):
        script = '#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$RIPARR_TEST_LOG"\n'
        script += "echo 0\n" if name == "id" else "exit 0\n"
        binary = binaries / name
        binary.write_text(script)
        binary.chmod(0o755)
    installer = (root / "tools/install.sh").read_text()
    config = sandbox / "config"
    for original, replacement in (("DATA_DIR=/var/lib/riparr", "DATA_DIR=" + str(data)),
                                  ("SYSTEM_CONFIG=/etc/default/riparr", "SYSTEM_CONFIG=" + str(config)),
                                  ("/usr/local/lib/riparr", str(sandbox / "system")),
                                  ("/etc/avahi", str(sandbox / "avahi")),
                                  ("/etc/systemd", str(sandbox / "systemd")),
                                  ("/etc/sysctl.d", str(sandbox / "sysctl")),
                                  ("/etc/modules-load.d", str(sandbox / "modules")),
                                  ("/srv/staging", str(sandbox / "staging"))):
        installer = installer.replace(original, replacement)
    entry = source / "tools/install.sh"
    entry.write_text(installer)
    (source / "tools/makemkv-install.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "packaging/apply-system.sh").write_text(
        '#!/bin/sh\nprintf "apply-system\\n" >> "$RIPARR_TEST_LOG"\n')
    (source / "server/riparr/main.py").write_text("# NEW\n")
    (source / "server/riparr/__init__.py").write_text('__version__ = "9.9.9"\n')
    (source / "server/requirements.txt").write_text("")
    marker = installed / "server/riparr/main.py"
    marker.write_text("# OLD\n")
    python = installed / ".venv/bin/python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    pip = installed / ".venv/bin/pip"
    pip.write_text('#!/bin/sh\nprintf "pip %s\\n" "$*" >> "$RIPARR_TEST_LOG"\n')
    pip.chmod(0o755)
    preserved = {config: "RIPARR_PORT=8877\nUNRELATED=keep\n",
                 data / "riparr.db": "account, shares, history, optical_drive=usb-serial",
                 data / ".MakeMKV/settings.conf": 'app_Key = "keep"\n',
                 python: python.read_text(), pip: pip.read_text()}
    for path, content in preserved.items():
        path.write_text(content)
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
               RIPARR_TEST_LOG=str(log), RIPARR_INSTALL_DIR=str(installed),
               RIPARR_UPDATE_ONLY="1", RIPARR_PORT="", RIPARR_HOST_ADDRESS="vm.example.lan")
    def run():
        return subprocess.run(["bash", str(entry)], env=env,
                              capture_output=True, text=True, timeout=30)
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "# NEW\n"
    assert all(path.read_text() == content for path, content in preserved.items())
    commands = log.read_text()
    assert "apply-system" in commands and "systemctl restart riparr.service" in commands
    assert "pip install --quiet -r" in commands and "--upgrade pip" not in commands
    for forbidden in ("apt-get", "useradd", "usermod", "groupadd", "modprobe", "udevadm", "sysctl",
                      "systemctl enable", "systemctl restart avahi", "systemctl restart systemd-resolved",
                      str(data)):
        assert forbidden not in commands, commands
    log.write_text("")
    result = subprocess.run(["bash", str(entry)], env=dict(env, RIPARR_PORT="9988"),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and "cannot change the HTTP port" in result.stderr
    assert config.read_text() == preserved[config]
    assert "apply-system" not in log.read_text()
    for missing in (marker, python):
        original = missing.read_bytes()
        missing.unlink()
        before = config.read_bytes()
        log.write_text("")
        result = run()
        assert result.returncode != 0 and "No complete Riparr installation" in result.stderr, \
            result.stdout + result.stderr
        assert config.read_bytes() == before
        assert "apply-system" not in log.read_text()
        missing.write_bytes(original)
        if missing == python:
            missing.chmod(0o755)


def test_share_mount_request():
    body = main.ShareCreate(host="nas.example.lan", share="Media", path="Movies",
                            username="DOMAIN/user", password="a%quoted'$password")
    verified = {"ok": True, "verified": True, "share": "Media", "path": "Movies"}
    for requested in (True, False):
        def remount():
            assert db.default_share()["verified_at"]
            return requested
        with patch.object(main.SH, "test_write", return_value=verified), \
                patch.object(P, "request_remount", side_effect=remount) as request:
            result = main.shares_create(body, user="test")
            assert result["mount_requested"] is requested
            assert bool(result["warning"]) is not requested
            request.assert_called_once()
    count = len(db.list_shares())
    with patch.object(main.SH, "test_write", return_value={"ok": False}), \
            patch.object(P, "request_remount") as request:
        try:
            main.shares_create(body, user="test")
        except main.HTTPException as error:
            assert error.status_code == 400
        else:
            raise AssertionError("An unverified share was saved")
        assert len(db.list_shares()) == count
        request.assert_not_called()


if __name__ == "__main__":
    try:
        db.init()
        for test in (test_directory_transport, test_directory_pipeline, test_storage_wording, test_production_mode, test_web_tool_bridge, test_share_mount_request,
                     test_drive_indexes, test_drive_selection, test_drive_inventory,
                     test_installer_controls, test_application_update):
            test()
            print("ok", test.__name__)
    finally:
        temporary.cleanup()