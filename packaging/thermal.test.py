#!/usr/bin/env python3
"""thermal.sh against a fake /sys: steps down when hot, up when cool, and holds the rip
ceiling and two cores only while a disc is being read.

Run: python3 packaging/thermal.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
root = tempfile.mkdtemp(prefix="riparr-thermal-")
cpu = os.path.join(root, "sys/devices/system/cpu")
pol = os.path.join(cpu, "cpufreq/policy0")
os.makedirs(pol)
FREQS = [480000, 720000, 1008000, 1200000, 1416000]
for name, value in (("scaling_available_frequencies", " ".join(map(str, FREQS))),
                    ("cpuinfo_max_freq", "1416000"), ("cpuinfo_min_freq", "480000"),
                    ("scaling_max_freq", "1416000")):
    open(os.path.join(pol, name), "w").write(value + "\n")
for i in range(4):
    os.makedirs(os.path.join(cpu, "cpu%d" % i), exist_ok=True)
    if i:
        open(os.path.join(cpu, "cpu%d" % i, "online"), "w").write("1\n")
open(os.path.join(cpu, "online"), "w").write("0-3\n")
zone = os.path.join(root, "sys/class/thermal/thermal_zone0")
os.makedirs(zone)
status = os.path.join(root, "status.json")
ripflag = os.path.join(root, "ripping")
failures = []


def check(name, got, want):
    print("  %s %s" % ("ok  " if got == want else "FAIL", name))
    if got != want:
        print("       got %r, wanted %r" % (got, want))
        failures.append(name)


def temp(c):
    open(os.path.join(zone, "temp"), "w").write("%d\n" % (c * 1000))


def read(path):
    return open(path).read().strip()


def settle(seconds=1.2):
    time.sleep(seconds)


temp(60)
env = dict(os.environ, RIPARR_THERMAL_ROOT=root, RIPARR_THERMAL_STATUS=status,
           RIPARR_THERMAL_INTERVAL="0.1", RIPARR_THERMAL_COOL_SETTING="1",
           RIPARR_THERMAL_RIPPING_FILE=ripflag)
proc = subprocess.Popen(["sh", os.path.join(HERE, "thermal.sh")], env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    print("idle and cool: nothing touched")
    settle()
    check("full speed", read(os.path.join(pol, "scaling_max_freq")), "1416000")
    check("all cores", read(os.path.join(cpu, "cpu3", "online")), "1")

    print("ripping: the rip ceiling and two cores")
    open(ripflag, "w").close()
    settle()
    check("1008 MHz", read(os.path.join(pol, "scaling_max_freq")), "1008000")
    check("cores 2 and 3 off", [read(os.path.join(cpu, "cpu%d" % i, "online"))
                                for i in (1, 2, 3)], ["1", "0", "0"])
    check("and it says so", json.load(open(status))["state"], "ripping")

    print("too hot while ripping: steps below the rip ceiling")
    temp(90)
    settle(1.5)
    check("down to the floor", read(os.path.join(pol, "scaling_max_freq")), "480000")
    check("reported as throttled", json.load(open(status))["state"], "ripping, throttled")

    print("rip over, cooled: everything comes back")
    os.remove(ripflag)
    temp(65)
    settle(1.5)
    check("full speed again", read(os.path.join(pol, "scaling_max_freq")), "1416000")
    check("all cores again", [read(os.path.join(cpu, "cpu%d" % i, "online"))
                              for i in (1, 2, 3)], ["1", "1", "1"])
    check("normal", json.load(open(status))["state"], "normal")

    print("the floor runs even with the setting off")
    proc.terminate()
    proc.wait()
    env["RIPARR_THERMAL_COOL_SETTING"] = "0"
    open(ripflag, "w").close()
    temp(90)
    proc = subprocess.Popen(["sh", os.path.join(HERE, "thermal.sh")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settle(1.5)
    check("still steps down when hot", read(os.path.join(pol, "scaling_max_freq")),
          "480000")
    check("but leaves the cores alone", read(os.path.join(cpu, "cpu3", "online")), "1")
finally:
    proc.terminate()
    proc.wait()

print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
