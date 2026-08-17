# Orin CDI `cudacompat` Hook Workaround

Host-side workaround that lets `smart-spotter-orin:dev` (and any GPU container)
start under `--runtime nvidia` on this Jetson Orin Nano Super.

**This is a workaround for a known NVIDIA bug, not a permanent fix.** Revisit it
whenever the container toolkit, driver, or JetPack is upgraded (see "When to
revisit" at the bottom).

---

## The problem

- **Platform:** Jetson Orin Nano Super, JetPack 7.2 / L4T r39.2, NVIDIA Container
  Toolkit **1.19.1**.
- **Symptom:** every `docker run --runtime nvidia ...` fails before the container
  starts, with:

  ```
  OCI runtime create failed: ... error running createContainer hook #2:
  panic: runtime error: slice bounds out of range [:73] with capacity 71
  ... nvidia-cdi-hook/cudacompat/cuda-elf-header.go:100
  ```

- **Root cause:** a bug in the toolkit's `nvidia-cdi-hook cudacompat` command. On
  Orin it tries to parse a CUDA forward-compatibility library's ELF header and
  panics. NVIDIA has fixed this upstream ("Fix handling of the CUDA compat header
  on Orin systems"), but **the jetson r39.2 apt repo only offers 1.19.1**, which
  still has the bug. No newer package is available to install.

### What did NOT work (don't bother retrying these)

- Removing `/usr/local/cuda*/compat` from the image — Orin uses `compat_orin`,
  and the hook runs regardless of image contents.
- `features.disable-cuda-compat-lib-hook = true` in config.toml — ignored by the
  active code path on 1.19.1.
- Stripping the hook from `/var/run/cdi/nvidia.yaml` — runtime auto-resolved to
  **csv/legacy** mode, which doesn't read that spec; and the file is tmpfs,
  regenerated on boot.
- `cuda-compat-mode = "disabled"` in the legacy modes block — no effect.

The byte offset in the panic (`[:73] capacity 71`) was **identical** across all
of these, which is the tell that none of them were on the active code path.

---

## The fix (what actually works)

Force the runtime into **explicit CDI mode** and request the GPU by its CDI
device name, so injection comes from a **CDI spec we control** (with the compat
hook stripped) instead of the buggy csv/legacy hook path.

Three parts:

### 1. Force CDI mode in the runtime config

```bash
sudo nvidia-ctk config --in-place --set nvidia-container-runtime.mode=cdi
# verify:
grep -n '^mode' /etc/nvidia-container-runtime/config.toml   # -> mode = "cdi"
```

### 2. Generate a persistent, compat-hook-free CDI spec

Generate into `/etc/cdi/` (persistent — survives reboot, unlike tmpfs
`/var/run/cdi/`), then remove the `enable-cuda-compat` hook block from it.

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml 2>/dev/null
```

Strip the hook with this script (`strip_compact.py`), pointed at
`/etc/cdi/nvidia.yaml`:

```python
p = "/etc/cdi/nvidia.yaml"
lines = open(p).read().splitlines(keepends=True)

# Find the line containing 'enable-cuda-compat'
target = None
for i, ln in enumerate(lines):
    if "enable-cuda-compat" in ln:
        target = i
        break
assert target is not None, "enable-cuda-compat not found"

# Walk back to the start of this hook block ('- hookName: createContainer')
start = target
while start >= 0 and "- hookName: createContainer" not in lines[start]:
    start -= 1
assert start >= 0, "could not find block start"

# Block runs until the next sibling list item at the same indent, or a dedent.
block_indent = len(lines[start]) - len(lines[start].lstrip())
end = target + 1
while end < len(lines):
    ln = lines[end]
    if ln.strip() == "":
        end += 1
        continue
    indent = len(ln) - len(ln.lstrip())
    if indent <= block_indent and ln.lstrip().startswith("- "):
        break
    if indent < block_indent:
        break
    end += 1

removed = lines[start:end]
assert any("enable-cuda-compat" in r for r in removed), "safety check failed"
print("Removing lines", start, "to", end - 1, ":")
print("".join(removed))
open(p, "w").write("".join(lines[:start] + lines[end:]))
print("done")
```

```bash
sudo python3 strip_compact.py
# verify it's gone:
sudo grep -c 'cuda-compat' /etc/cdi/nvidia.yaml   # -> 0
```

### 3. Disable the auto-refresh service and remove the tmpfs spec

`nvidia-cdi-refresh` regenerates the spec (with the hook back) on boot and on
driver/config changes. Disable it so `/etc/cdi/nvidia.yaml` stays authoritative,
and remove the tmpfs spec so there's no competing/hooked spec.

```bash
sudo systemctl disable --now nvidia-cdi-refresh.service nvidia-cdi-refresh.path
sudo rm -f /var/run/cdi/nvidia.yaml
```

(`systemctl mask` fails here because the unit file is a real file in
`/etc/systemd/system/`, not a symlink — `disable --now` is the correct verb.)

---

## Mandatory run invocation

Because the fix relies on CDI-by-name injection, **every** GPU container run must
request the device by its CDI name via this env var. Without it the runtime falls
back to the buggy path and the container won't start:

```bash
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  smart-spotter-orin:dev
```

(The production inference run additionally uses `--network host` for the GigE
data plane.)

---

## Verifying the fix is healthy

```bash
# Service must be disabled+inactive:
systemctl is-enabled nvidia-cdi-refresh.service   # -> disabled
systemctl is-active  nvidia-cdi-refresh.service   # -> inactive

# Only the clean /etc/cdi spec should define the gpu device:
sudo grep -l 'nvidia.com/gpu' /etc/cdi/*.yaml /var/run/cdi/*.yaml 2>/dev/null
#   -> /etc/cdi/nvidia.yaml   (and nothing in /var/run/cdi)

sudo grep -c 'cuda-compat' /etc/cdi/nvidia.yaml   # -> 0

# End-to-end: container starts, NVDEC plugin loads:
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --entrypoint bash smart-spotter-orin:dev \
  -c 'gst-inspect-1.0 nvv4l2decoder >/dev/null 2>&1 && echo OK || echo MISSING'
#   -> OK
```

This was confirmed to survive a reboot.

---

## When to revisit / undo

This workaround disables auto-refresh of the CDI spec, so the spec will **not**
auto-update on driver/JetPack changes. Revisit in these cases:

- **Toolkit upgrade (the real fix):** when the jetson apt repo offers a
  container-toolkit newer than 1.19.1 with the Orin compat-header fix, prefer
  upgrading the package and **undoing this workaround**:
  ```bash
  sudo systemctl unmask nvidia-cdi-refresh.service nvidia-cdi-refresh.path  # if masked
  sudo systemctl enable --now nvidia-cdi-refresh.service nvidia-cdi-refresh.path
  # optionally revert mode=cdi back to auto:
  sudo nvidia-ctk config --in-place --set nvidia-container-runtime.mode=auto
  ```
  Then re-test with a plain `--runtime nvidia` run. Check
  `apt-cache policy nvidia-container-toolkit` for the candidate version first.

- **Driver / JetPack upgrade:** the CDI spec may need regenerating against the new
  driver. Since auto-refresh is disabled, regenerate manually:
  re-run steps 2 and 3 above (generate to `/etc/cdi/nvidia.yaml`, strip, remove
  tmpfs spec). If the toolkit is still buggy, keep the workaround; if it's fixed,
  undo as above.

- **Container stops starting again after any system update:** re-run the
  "Verifying the fix is healthy" checks; whichever one fails points at what the
  update changed (re-enabled refresh service, regenerated hooked spec, or reset
  `mode`).

## Notes (don't re-diagnose these)

- The Dockerfile's `rm -rf /usr/local/cuda*/compat` layer is based on the
  *wrong* (early) diagnosis and does nothing useful now. Safe to remove on next
  rebuild; harmless to leave.
- `groups: cannot find name for group ID NNN` warnings on container start are
  cosmetic (host GIDs with no matching container `/etc/group` entry). Ignore.
- Rebuilding the image (`docker build`) does **not** affect this host-side fix.
  No need to re-run any of these steps after a rebuild.
