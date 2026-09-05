# Jellyfin Media Center — Phase 1 Trial

Scaled-down trial of a home media center, using hardware already on hand, before
buying dedicated NAS/mini-PC hardware (see the original full build plan the user
has saved separately for the eventual scaled-up version with Immich + RAID).

**Scope of this phase:** one Dell Latitude laptop running only Jellyfin,
with a single USB drive (WD10000H1U-00, 1TB, AC-powered) directly attached.
No Immich, no RAID — that's a later phase once this is proven out.

## Stack

- Ubuntu Server 24.04 LTS (headless)
- USB drive formatted ext4, mounted via a **systemd automount unit** (not fstab) —
  so a drive that gets unplugged and replugged recovers without manual `mount` commands
- **Rootless Podman**, managed via **Quadlet** systemd units (no Docker, no compose file)
- Jellyfin, single container, port 8096

Unit files referenced below live in [`units/`](units/) in this folder — copy them to
the laptop rather than retyping.

---

## 1. Install the OS

1. Flash Ubuntu Server 24.04 LTS to a USB installer, install headless on the laptop.
2. During install: create your user, enable OpenSSH so the box can be administered
   remotely without a monitor/keyboard attached long-term.

## 2. Base OS setup (headless, lid closed)

```bash
sudo apt update && sudo apt full-upgrade -y

# Prevent suspend on lid close / idle — needed since this runs headless, lid shut
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo sed -i 's/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' /etc/systemd/logind.conf
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo systemctl restart systemd-logind
```

Prefer wired Ethernet over Wi-Fi for a server that needs to stay reachable.

## 3. Format the USB drive and set up automount

⚠️ This formats the drive — back up anything currently on the WD10000H1U-00 first.

```bash
lsblk                              # identify the drive, e.g. /dev/sdb
sudo parted /dev/sdb --script mklabel gpt mkpart primary ext4 0% 100%
sudo mkfs.ext4 -L media /dev/sdb1
sudo mkdir -p /mnt/media
sudo blkid /dev/sdb1               # copy the UUID printed here
```

Copy the two unit files from [`units/`](units/) onto the laptop, filling in the UUID
from `blkid` into `mnt-media.mount`:

```bash
sudo cp mnt-media.mount /etc/systemd/system/mnt-media.mount
sudo cp mnt-media.automount /etc/systemd/system/mnt-media.automount
sudo nano /etc/systemd/system/mnt-media.mount   # replace REPLACE-WITH-UUID
```

Enable the automount (not the plain `.mount` unit — the automount unit brings the
mount unit up lazily on first access):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mnt-media.automount
ls /mnt/media                      # first access triggers the actual mount
sudo chown -R <user>:<user> /mnt/media
mkdir -p /mnt/media/movies /mnt/media/tv
```

**Why automount instead of an `/etc/fstab` line:** a plain fstab entry (even with
`nofail`) only mounts at boot. If the drive is unplugged and replugged while the
system is running, nothing remounts it automatically — you'd have to SSH in and
run `mount` by hand. The automount unit watches `/mnt/media` and (re)triggers the
underlying `.mount` unit the next time anything tries to access that path, so a
normal "unplug, replug, browse the library later" sequence recovers on its own.

**What actually happens if the drive is unplugged while Jellyfin is running:**
- Any active playback from that drive fails immediately.
- The mount starts returning I/O errors on any further access; systemd's device
  tracking notices the underlying device disappeared and tears down the `.mount`
  unit (it's bound to the udev device).
- Once replugged, the *next* access to `/mnt/media` (e.g. Jellyfin's own library
  scan, or you running `ls /mnt/media`) causes the automount unit to bring the
  mount back.
- **The Jellyfin container itself needs a restart to see the fresh mount.** A bind
  mount into a container is a snapshot of the host mount at the moment the
  container started — by default it does not follow a later unmount/remount of
  the same host path. So after recovering the host-side mount, run:
  ```bash
  systemctl --user restart jellyfin.service
  ```
  This is a one-command fix, not a full reinstall — just worth knowing it's a
  manual step rather than fully automatic.

## 4. Install rootless Podman

```bash
sudo apt install -y podman uidmap slirp4netns fuse-overlayfs

# Confirm subuid/subgid ranges exist for your user (Ubuntu usually sets these automatically)
grep <user> /etc/subuid /etc/subgid
# If empty:
# sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 <user>

# Required so your user's systemd instance (and its containers) runs at boot
# without an active login session
sudo loginctl enable-linger <user>

# Sanity check
podman run --rm hello-world
```

## 5. Quadlet unit for Jellyfin

```bash
mkdir -p ~/.config/containers/systemd/
mkdir -p ~/jellyfin/config ~/jellyfin/cache
cp units/jellyfin.container ~/.config/containers/systemd/jellyfin.container

systemctl --user daemon-reload
systemctl --user start jellyfin.service
systemctl --user status jellyfin.service
```

Because of `loginctl enable-linger` plus `WantedBy=default.target` in the unit,
this comes back up automatically on reboot with no login required.

## 6. Open access and configure

```bash
sudo ufw allow 8096/tcp    # only if ufw is active
hostname -I                # get the laptop's LAN IP
```

Browse to `http://<laptop-ip>:8096` from another device, run Jellyfin's setup
wizard, and add `/media/movies` and `/media/tv` (the in-container paths) as
libraries.

## 7. Verify

- Play a file from another device on the LAN (confirms direct-play end to end).
- Reboot the laptop; confirm the drive remounts (first access after boot) and
  Jellyfin comes back up on its own.
- Unplug/replug the drive while the container is running and confirm the
  recovery steps in section 3 work: access `/mnt/media` to retrigger the
  automount, then `systemctl --user restart jellyfin.service`.
- Optional: `systemctl --user enable --now podman-auto-update.timer` to keep the
  Jellyfin image current (works with the `AutoUpdate=registry` label already on
  the unit).

---

## Phase 2 (not yet)

Once this trial proves out: add a second dedicated drive and an Immich
container (same Quadlet pattern, new `.container` file). Immich needs an x86
host with real filesystem access for its Postgres DB (no NTFS/exFAT), which
this laptop already satisfies — the Raspberry Pi 4 on hand is not a good fit
for Immich's ML features (ARM). RAID/DAS/multi-drive scaling stays out of
scope until this phase is validated.
