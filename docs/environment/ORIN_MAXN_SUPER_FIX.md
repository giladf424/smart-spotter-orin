# Orin Nano — MAXN SUPER / 25W Power Mode Missing on JetPack 7.2 (and the Fix)

## Summary

After a clean first-time install of **JetPack 7.2 / Jetson Linux r39.2** on our **Jetson Orin Nano Super (module P3767-0005)** using the Jetson ISO installer, the device only exposed **7W** and **15W** power modes. The **25W** and **MAXN SUPER** modes were missing from both the GUI power-mode dropdown and the `nvpmodel` configuration.

Root cause: the device was flashed with the **non-super board configuration** (`jetson-orin-nano-devkit`) instead of the **super** one (`jetson-orin-nano-devkit-super`). MAXN SUPER is gated at the firmware/board-config level, so no amount of `nvpmodel` config-file editing could expose it. The fix corrects the board identity in firmware in-place, with no host machine and no reflash.

---

## Environment

- **Module:** P3767-0005 (Orin Nano 8GB, Super-capable SKU) — confirmed via EEPROM string `699-13767-0005-301`
- **JetPack:** 7.2
- **Jetson Linux (L4T):** r39.2 (`R39 (release), REVISION: 2.0`)
- **Firmware version at time of issue:** 39.2.0 (slots normal)
- **Install method:** Jetson ISO installer (USB), fresh install
- **CPU cores online:** 0–5 (6 cores — the trimmed-core 0005 layout, relevant below)

---

## Symptoms

- Power-mode dropdown showed only `0: 15W` and `1: 7W`.
- `sudo nvpmodel -q` reported `NV Power Mode: 15W` (mode 0).
- `/etc/nvpmodel.conf` was symlinked to `nvpmodel_p3767_0003.conf`, a **non-super** table defining only 15W/7W — with no MAXN_SUPER entry.
- The symlink reset itself back to the non-super table on every reboot.

---

## Why the Obvious Fixes Did Not Work

Several intuitive attempts failed, and understanding *why* is what led to the real fix:

1. **Relinking `/etc/nvpmodel.conf` to a super table** (`nvpmodel_p3767_0000_super.conf`): failed with `Error opening /sys/devices/system/cpu/cpu6/online` — that table assumes an **8-core** module (cpu6/cpu7), but the 0005 has only 6 cores. Wrong table for the SKU.

2. **Relinking to `nvpmodel_p3767_0004_super.conf`** (the correct trimmed-core table, defining 10W/25W/MAXN_SUPER): the mode change required a reboot, but on reboot the boot process **re-derived the symlink back to the non-super `0003` table** from the board's firmware identity. The change never persisted.

3. **The key realization:** `/etc/nvpmodel.conf` is a *leaf*. The board's firmware identity (the **TNSPEC** in `/etc/nv_boot_control.conf`) is the *root*. Because the firmware identified the board as plain `jetson-orin-nano-devkit` (non-super), the system kept regenerating the non-super power table. The fix has to change the **firmware board identity**, not the symlink.

Confirmed via:

```bash
cat /etc/nv_boot_control.conf
# TNSPEC 3767-301-0005-M.1-1-1-jetson-orin-nano-devkit-   <-- plain "devkit", NOT "devkit-super"
```

---

## The Fix (in-place, no host machine, no reflash)

> This is the community-verified fix for this exact bug (P3767-0005, JetPack 7.2), confirmed on our device. It edits the TNSPEC to the **super** identity, then uses the bootloader package's own reconfigure path to rewrite the firmware to match.

**Prerequisite check — the make-or-break condition:** the `nvidia-l4t-bootloader` package must be installed, or the firmware-rewrite step silently does nothing.

```bash
dpkg -l | grep -i "nvidia-l4t-bootloader"
# Must show:  ii  nvidia-l4t-bootloader  39.2.0-...  (installed)
```

If that package is **not** installed, do **not** run the perl edit — use the SDK Manager reflash route (JetPack 6.2.x rollback via Ubuntu 22.04 host or Windows 11 + WSL2) instead.

**Steps (all as root):**

```bash
# 0. Back up the file we're about to edit
sudo cp /etc/nv_boot_control.conf /etc/nv_boot_control.conf.bak

# 1. Enter a root shell
sudo -i

# 2. Rewrite the board identity: jetson-orin-nano-devkit- -> jetson-orin-nano-devkit-super-
#    -0777 slurps the whole file so \n matches at end of the TNSPEC/COMPATIBLE_SPEC lines
perl -i -0777 -pe 's/nano-devkit-\n/nano-devkit-super-\n/g' /etc/nv_boot_control.conf

# 3. Verify BOTH lines now end in -super- before continuing
grep -n "devkit" /etc/nv_boot_control.conf
#   1:TNSPEC          ...jetson-orin-nano-devkit-super-
#   2:COMPATIBLE_SPEC ...jetson-orin-nano-devkit-super-

# 4. Rewrite the firmware/bootloader to match the new identity (this is the actual fix)
dpkg-reconfigure nvidia-l4t-bootloader

# 5. Remove the stale non-super nvpmodel symlink so it regenerates correctly
rm /etc/nvpmodel.conf

# 6. Reboot (may run a firmware capsule-update cycle on boot — do NOT cut power)
reboot
```

**After reboot** (back as normal user), set and confirm MAXN SUPER:

```bash
sudo nvpmodel -m 2 --verbose --force
sudo nvpmodel -q
```

---

## Verification (post-fix, confirmed on our device)

```bash
sudo nvpmodel -q
# NV Power Mode: MAXN_SUPER
# 2

tr '\0' '\n' < /proc/device-tree/compatible
# nvidia,p3768-0000+p3767-0005-super   <-- firmware now carries the "-super" identity
# nvidia,p3767-0005
# nvidia,tegra234
```

- The GUI power-mode dropdown now shows **15W / 25W / MAXN SUPER**.
- MAXN SUPER **persists across reboots** (the original bug was non-persistence — this is the real confirmation).
- The `-super` suffix in the device-tree compatible string is the definitive proof the firmware accepted the super board identity (it is baked into the board config, not a runtime override that resets).

---

## Notes / Gotchas

- **Barrel-jack PSU:** MAXN SUPER is uncapped. Run the device on the 19V barrel-jack supply, not USB-C, or sustained GPU load (e.g. TensorRT inference) will brown out or thermal-throttle — which later looks like inference-latency jitter, not an obvious power error.
- **Correct nvpmodel table for P3767-0005:** `nvpmodel_p3767_0004_super.conf` (10W / 25W / MAXN_SUPER, 6-core layout). The `0000_super` table is for the 8-core P3767-0000 and will error on cpu6/cpu7.
- **Mode IDs differ from NVIDIA's generic docs:** in the correct table, MAXN_SUPER is **ID 2**. Use the IDs from the actual conf, not the screenshots in the quick-start guide.
- **Reflash implication:** if this Orin is ever re-flashed, the firmware board identity is rewritten from the flash config, so this fix must be re-applied (or the device flashed directly with the super board config). Worth noting in the build docs so it's not a future mystery.
- **Rollback:** `/etc/nv_boot_control.conf.bak` holds the original (non-super) config if a revert is ever needed.

---

## References

- NVIDIA forum, "25w and MAXN SUPER not seen in jetpack 7.2" — accepted solution (a5ehren) with the in-place TNSPEC + `dpkg-reconfigure` method.
- NVIDIA forum, "JetPack 7.2 / Jetson Linux r39.2 … Getting Started and feedback thread" — WSL2 / SDK Manager reflash alternative (tekboart), used if `nvidia-l4t-bootloader` is absent.
- Cause: JetPack 7.2 USB/ISO installer flashing brand-new 8GB dev kits (firmware >36.4, <36.5) with the non-super board config instead of letting the user choose super vs non-super.
