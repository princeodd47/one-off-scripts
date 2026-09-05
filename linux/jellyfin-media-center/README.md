# Jellyfin Media Center — Phase 1 Trial

Scaled-down trial of a home media center, using hardware already on hand, before
buying dedicated NAS/mini-PC hardware (see the original full build plan the user
has saved separately for the eventual scaled-up version with Immich + RAID).

**End goal (unchanged, just not affordable yet):** a proper headless server with
real NAS-grade storage, per the original build plan. Everything in this document
is a budget-constrained interim step using hardware already owned — not a
replacement for that goal. Once budget allows, the plan is to migrate off this
laptop-plus-USB-drive setup onto dedicated hardware, at which point the
"connected to a TV with a keyboard" arrangement goes away and it goes back to
being tucked-away headless.

**Scope of this phase:** one laptop (Dell Latitude, spec TBD — Toshiba Portege
R835-P56X considered as a backup candidate) running Jellyfin, with a single USB
drive (WD10000H1U-00, 1TB, AC-powered) directly attached. No Immich, no RAID —
that's a later phase once this is proven out.

**Not headless, for now.** Budget constraints mean there's no separate device
to act as a "TV box" client, so the server laptop itself sits connected directly
to the TV over HDMI and is driven with a wireless keyboard/mouse — video plays
locally without going through the network at all. The only remote access is SSH,
for updates and pushing files onto the drive. This is a stopgap arrangement, not
the target architecture.

## Stack

- Ubuntu Desktop 24.04 LTS — a **lightweight flavor recommended** (Xubuntu/XFCE)
  given the laptop hardware here is older/budget-tier and a heavy desktop
  environment eats into the CPU headroom Jellyfin needs
- USB drive formatted ext4, mounted via a **systemd automount unit** (not fstab) —
  so a drive that gets unplugged and replugged recovers without manual `mount` commands
- **Rootless Podman**, managed via **Quadlet** systemd units (no Docker, no compose file)
- Jellyfin server, single container, port 8096
- **Jellyfin Media Player** (mpv-based desktop client) running locally, talking to
  the server over `localhost` and outputting to the TV via HDMI — this avoids the
  extra transcoding a browser-based player would force for codecs the browser
  itself can't decode, which matters more on older/weaker CPUs
- OpenSSH, for remote administration only (updates, `scp`/`rsync` file transfers)

Unit files referenced below live in [`units/`](units/) in this folder — copy them to
the laptop rather than retyping.

---

## 1. Install the OS

1. Flash **Xubuntu 24.04 LTS** (or another lightweight Ubuntu Desktop flavor) to a
   USB installer, install with the laptop connected to the TV over HDMI so you can
   see what you're doing, plus the wireless keyboard/mouse.
2. During install: create your user. The desktop installer doesn't offer an
   OpenSSH checkbox like the server installer does — install it after first boot:
   ```bash
   sudo apt update && sudo apt install -y openssh-server
   ```
3. Optional but convenient for an appliance-like box: enable auto-login to the
   desktop session (Settings → Users, or edit the display manager's config) so it
   comes up ready to watch something after a power cycle without needing the
   keyboard for a login screen.

## 2. Base OS setup

```bash
sudo apt update && sudo apt full-upgrade -y

# Safety net: don't suspend if the lid ever gets bumped/closed accidentally
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo sed -i 's/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' /etc/systemd/logind.conf
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo systemctl restart systemd-logind
```

Prefer wired Ethernet over Wi-Fi if you can run a cable to where the TV is — SSH
access and any file pushes are more reliable that way, though Wi-Fi is fine too
since this box no longer needs to be tucked away out of reach.

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
sudo ufw allow OpenSSH     # the only inbound access this box needs from the LAN
sudo ufw allow 8096/tcp    # only if you also want other devices to stream from it
sudo ufw enable            # only if ufw wasn't already active
hostname -I                # get the laptop's LAN IP, for SSH'ing in later
```

Browse to `http://localhost:8096` **on the laptop itself** (once the desktop and
a browser are up), run Jellyfin's setup wizard, and add `/media/movies` and
`/media/tv` (the in-container paths) as libraries. Other devices on the LAN can
still reach `http://<laptop-ip>:8096` if you ever want to stream to them too —
that's not disabled, just not the primary use case here.

## 7. Local playback on the TV

Install Jellyfin Media Player (mpv-based desktop client) via Flatpak, the
simplest cross-distro path:

```bash
sudo apt install -y flatpak
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install -y flathub com.github.iwalton3.jellyfin-media-player
```

Launch it (`flatpak run com.github.iwalton3.jellyfin-media-player`), point it at
`http://localhost:8096`, and log in with the Jellyfin account you created above.
Set it full-screen for the TV.

If you enabled auto-login in step 1, you can also set this to autostart on login
(Xubuntu: Settings → Session and Startup → Application Autostart → add the same
`flatpak run ...` command) so powering on the laptop goes straight to a
ready-to-browse screen.

## 8. Verify

- Play a file locally through Jellyfin Media Player on the TV — confirm HDMI
  video/audio output and that playback is direct (check the file's playback info
  in Jellyfin for "Direct Play" vs "Transcode").
- Reboot the laptop; confirm the drive remounts (first access after boot),
  Jellyfin comes back up on its own, and (if autostart is set) the player is
  ready without touching the keyboard.
- Confirm SSH still works from another machine on the LAN (`ssh <user>@<laptop-ip>`)
  — this is your only remote path now, so verify it before you stop needing to
  sit at the TV with the keyboard for admin tasks.
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

## Eventual target (budget allowing)

This whole laptop-plus-USB-drive-plus-TV arrangement is a stand-in for real
hardware, not the destination. Once budget allows, migrate to the original full
build plan: a dedicated mini PC or NAS-class box, proper multi-drive storage
(RAID via `mdadm`), running headless and tucked away — with the
keyboard/mouse/HDMI-to-TV setup retired in favor of streaming to whatever
client devices are on the network (smart TV apps, phones, etc.) instead of
local playback on the server itself.
