# Riparr Docker Edition

Linux amd64 only initially, on a normal Linux Docker engine or a Linux VM with
physical USB/SCSI optical-drive passthrough. Docker Desktop, rootless engines and
NAS platforms have not been validated for optical ripping.

The image contains MakeMKV, DVD backup/decryption tools, CD readers and audio
encoders. There is no first-run MakeMKV installation or EULA acknowledgement.
Registration/beta keys still belong in the web settings and persistent state.
The container does not manage host Wi-Fi, power, thermals, watchdogs or kernel
configuration. It runs as a non-root user without systemd or a Docker socket.

## Pass the Optical Drive Through

Run these commands **on the Linux machine running the Docker engine**, not inside
Riparr or on a separate Docker client. If Docker runs in a VM, first pass the
physical USB/SCSI drive through to that VM using the hypervisor. An ISO-backed
virtual CD-ROM is not a physical ripping drive.

1. Find the physical drive by its vendor/model:

	```sh
	lsblk -S -o NAME,TYPE,VENDOR,MODEL
	ls -l /dev/sr*
	```

2. Set its device path and find its matching SCSI-generic device. For example,
	if the physical drive is `sr1`:

	```sh
	export OPTICAL_DEVICE=/dev/sr1
	ls "/sys/class/block/$(basename "$OPTICAL_DEVICE")/device/scsi_generic/"
	```

	If this prints `sg2`, set `SCSI_DEVICE=/dev/sg2`. The numbers **do not need to
	match**. If no generic device appears, check physical-drive passthrough and load
	the host's SCSI-generic module with `sudo modprobe sg`.

3. Record the device group IDs for the container's non-root user:

	```sh
	export SCSI_DEVICE=/dev/sg2
	export OPTICAL_GID="$(stat -c '%g' "$OPTICAL_DEVICE")"
	stat -c '%n group=%g permissions=%A' "$OPTICAL_DEVICE" "$SCSI_DEVICE"
	```

The supplied Compose file already maps both device paths with read/write access
using its `devices` entries. Set these variables before starting it; Riparr cannot
add host devices from the web UI. The host must provide normal optical udev
permissions. If the two numeric groups differ, add the SCSI device's group as a
second `group_add` entry as described below.

## Prepare the Library and Marker File

Choose the **existing host directory** that will become `LIBRARY_PATH`. Mount its
NAS/share first if it is network storage. For this example, the host directory is
`/mnt/media` and Compose maps it to `/srv/library` inside the container.

`.riparr-library` is an **empty regular file, not a directory**. Create it at the
top of that host library directory, on the intended storage filesystem:

```sh
findmnt -T /mnt/media
# Check that the source above is the intended NAS or local storage before proceeding.
touch /mnt/media/.riparr-library
ls -l /mnt/media/.riparr-library
```

After startup, that same file is visible as `/srv/library/.riparr-library` inside
the container. Do not put it beside the Compose file, in the application-state
volume, or in staging storage. A folder named `.riparr-library` will not work.

The file tells Riparr that you intentionally selected this directory as its
library. Riparr requires it before accepting library writes, helping prevent an
empty fallback directory from being used when a host mount is missing. It contains
no credentials, does not mount the NAS, and does not prove the NAS is online by
itself. **Never create it on an unmounted NAS mountpoint**, since that would mark
the fallback host directory instead of the NAS.

Start/recreate the container after the host library mount is available. The
default bind configuration does not automatically propagate later host remounts.

The runtime user must have write access to the library, state and staging, and
the numeric supplemental groups must permit optical/SCSI access. The image's
default UID/GID is 1000:1000. Do not grant the `disk` group, map all of `/dev`, or
use `privileged: true` as a substitute for the correct mappings.

## Run With Compose

From this directory, set the following values in your shell or a local `.env`:

```sh
export OPTICAL_DEVICE=/dev/sr1
export SCSI_DEVICE=/dev/sg2
export OPTICAL_GID="$(stat -c '%g' "$OPTICAL_DEVICE")"
export LIBRARY_PATH=/mnt/media
export PUID=1000
export PGID=1000
export RIPARR_BIND_ADDRESS=127.0.0.1
export RIPARR_HTTP_PORT=9797
export RIPARR_IMAGE=riparr:local
docker compose build
docker compose up -d
```

The paths and GID above are examples; use your host's actual values. Optical and
SCSI nodes normally share a group. If their host GIDs differ, add the SCSI device's
numeric GID as a second `group_add` entry in the Compose file (do not duplicate the
first entry). To expose the UI to your LAN, set the bind address to
the intended host interface address and apply appropriate network access controls.
Open `http://<bind-address>:9797`, create the account, and verify the directory
under Library. The library root is configured by Docker/Compose, not the web UI;
change `LIBRARY_PATH` and recreate the container to change it. Per-kind folder
names and file naming remain editable in Riparr. The service can start without media inserted. Select the physical
ripping drive in System if more than one optical device is exposed.

Named volumes keep state and staging. Keep the SQLite database/WAL volume on
reliable local storage, not an SMB/NFS filesystem. Staging needs space for the
largest queued jobs and verification; do not substitute a small tmpfs. The library
is a separate writable bind mount and is never recursively chowned at startup.

For a custom PUID/PGID, initialize named-volume ownership once using an explicit
maintenance container before running the web service; changing the UID does not
change existing volume ownership. The default named volumes initialize correctly
for 1000:1000 from the image. Keep application code read-only and map only writable
state/staging/runtime paths.

## Updates and Recovery

Versioned releases publish `ghcr.io/<repository-owner>/<repository>:<appliance-version>`
through the release workflow after the image smoke test. No registry image was
published by local implementation/testing. Set `RIPARR_IMAGE` to the desired
published version, then use:

```sh
docker compose pull
docker compose up -d
```

For a locally built image, rebuild with `docker compose build --pull` before `up`.
Stop or finish active jobs before replacing the image. State and staging survive
recreation; do not use `down -v` unless deliberately deleting them. Back up state
before upgrades, since an older image may not understand a migrated database.
MakeMKV/tool versions change with images, not through in-container apt or the
application's source updater. Keys/settings remain persistent.

Static device mappings may require recreation after unplug/replug or changed
device numbers. Recheck the physical optical/sg pairing and groups first. The
container does not automatically claim new host devices. Hardware/USB bridges,
drive firmware and media must support the requested formats; UHD still requires
working LibreDrive. Host passthrough, raw SCSI reading and physical eject need
real hardware validation on each deployment.

The image uses an init process and forwards termination to the process group.
Shutdown requests cancellation of active jobs; Docker's stop grace provides time
for cleanup. A healthcheck reports an unhealthy running process but is not itself
an automatic restart mechanism.

## Verification

`smoke.py` exercises a fresh disposable instance and creates a test-only account;
do not run it against existing user data. Image CI tests the non-root, read-only,
capability-dropped web runtime without physical devices. Actual optical ripping
and VM passthrough remain separate hardware acceptance tests.