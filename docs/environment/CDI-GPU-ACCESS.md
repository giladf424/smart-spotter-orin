# GPU access in containers on this Orin

Two separate defects can stop `smart-spotter-orin:dev` from using the GPU. They
have different symptoms, different causes, and — importantly — **their fixes
pull in opposite directions** on one setting. Read the triage table first and
work only the row that matches your symptom.

State in this document was verified on **2026-08-18** against the running
system. Platform: Jetson Orin Nano Super, JetPack 7.2 / L4T r39.2, NVIDIA
Container Toolkit 1.19.1.

---

## Triage

| Symptom | Cause | Check |
|---|---|---|
| Container **never starts**. `docker run --runtime nvidia` fails with `OCI runtime create failed: ... createContainer hook #2: panic: slice bounds out of range` | [A: compat hook panic](#cause-a--the-cudacompat-hook-panics) | `grep -c enable-cuda-compat /etc/cdi/nvidia.yaml` → want **0** |
| Container **starts**, but CUDA sees no GPU: `cudaGetDeviceCount()` segfaults (exit 139) or returns `100` (`CUDA_ERROR_NO_DEVICE`). Host `nvidia-smi` and `cuInit(0)` are fine | [B: stale device majors](#cause-b--stale-device-majors-in-the-cdi-spec) | `ls -la /dev/nvgpu/igpu0/ctrl` vs the `major:` for that path in `/etc/cdi/nvidia.yaml` — a mismatch confirms it |
| dmesg full of alarming `dce:` / `NVRM:` errors | Neither — see [red herring](#red-herring-the-dce-boot-errors) | Ignore. Do not flash the board. |

Both causes route through the same mechanism: the runtime is in **CDI mode**, so
everything injected into a container comes from the spec at
`/etc/cdi/nvidia.yaml`. Cause A is a bad *hook* in that spec; cause B is bad
*device numbers* in it.

Before working either row, confirm the host GPU itself is healthy — this rules
out a real driver or firmware fault and tells you the problem is container-side:

```bash
python3 -c "import ctypes; print('cuInit:', ctypes.CDLL('libcuda.so.1').cuInit(0))"
#   want 0
```

---

## Cause A — the `cudacompat` hook panics

Toolkit 1.19.1 ships a `nvidia-cdi-hook cudacompat` command that tries to parse
a CUDA forward-compatibility library's ELF header. On Orin it panics:

```
panic: runtime error: slice bounds out of range [:73] with capacity 71
... nvidia-cdi-hook/cudacompat/cuda-elf-header.go:100
```

The hook runs as a `createContainer` hook, so the container dies before any of
our code runs. NVIDIA fixed this upstream ("Fix handling of the CUDA compat
header on Orin systems"), but **the jetson r39.2 apt repo still only offers
1.19.1**:

```bash
apt-cache policy nvidia-container-toolkit | grep -E 'Installed|Candidate'
#   Installed: 1.19.1-1
#   Candidate: 1.19.1-1      <- still no fixed package as of 2026-08-18
```

So the hook has to be removed from the spec by hand. `docker/strip_compat.py`
does that: it finds the `enable-cuda-compat` line, walks back to its
`- hookName: createContainer` block start, and deletes the whole block. It
`assert`s if the hook is absent, which is deliberate — see
[removing the workaround](#removing-the-workaround).

### What did NOT work

Do not spend time re-trying these. The panic's byte offset (`[:73] capacity 71`)
was **identical** across all of them, which is the tell that none were on the
active code path:

- Removing `/usr/local/cuda*/compat` from the image — Orin uses `compat_orin`,
  and the hook runs regardless of image contents.
- `features.disable-cuda-compat-lib-hook = true` in `config.toml` — ignored by
  1.19.1's active code path.
- Stripping the hook from `/var/run/cdi/nvidia.yaml` — the runtime auto-resolved
  to csv/legacy mode, which does not read that spec, and the file is on tmpfs
  and regenerated at boot anyway.
- `cuda-compat-mode = "disabled"` in the legacy modes block — no effect.

What did work was forcing the runtime into explicit CDI mode and requesting the
GPU by CDI device name, so injection comes from a spec we control.

---

## Cause B — stale device majors in the CDI spec

`/etc/cdi/nvidia.yaml` records each device node's **major/minor numbers as they
were at generation time**, and the CDI runtime `mknod`s exactly those numbers
inside the container.

The nvgpu nodes (`/dev/nvgpu/igpu0/*`, `/dev/nvhost-*-gpu`) use **dynamically
allocated majors that can change across reboots**. On this board they currently
span four: 493 (44 nodes), 494, 496, 497. By contrast `/dev/nvidia*` uses static
major 195 — which is exactly why every host-level check keeps passing while
containers fail.

After a reboot reshuffles the majors, a container gets device nodes pointing at
kernel devices that do not exist. `libcuda` finds no GPU, and you get
`CUDA_ERROR_NO_DEVICE` or a segfault.

The service that exists to prevent this is `nvidia-cdi-refresh`, which
regenerates the spec on boot. **It had been disabled** as part of an early
workaround for cause A, so the spec was never regenerated and every reboot could
re-break containers. That is the trap: fixing A by disabling refresh causes B.

### Red herring: the DCE boot errors

Every boot of this board logs:

```
dce: dce_admin_send_cmd_ver: version : dcefw:[0x4] dcekmd:[0x4] err:[0x0]
dce: dce_admin_setup_clients_ipc:978  Get queue info failed for [2]
NVRM: GPU0 rpcRmApiControl_dce: ... cmd:0x731341 result 0xffff [NV_ERR_GENERIC]
NVRM: GPU0 rpcRmApiControl_dce: ... cmd:0x730282 result 0x1f [NV_ERR_INVALID_ARGUMENT]
```

This has been present on **every boot since first provisioning**, including all
periods when inference worked. IPC channel type 2 is the DCE *display* client
and the `0x73xxxx` RM calls are display-class controls; on this headless board
they fail harmlessly and have zero effect on CUDA compute. Do not flash the board
because of these lines.

---

## Current state

This is the configuration that handles both causes at once. Every line below was
checked against the running system on 2026-08-18.

**1. Runtime forced to CDI mode** — `/etc/nvidia-container-runtime/config.toml`
line 21 reads `mode = "cdi"`. Consequence: every GPU container must request the
device by CDI name (see [running containers](#running-containers)).

**2. Single persistent spec** at `/etc/cdi/nvidia.yaml`. `/var/run/cdi/` does not
exist, so there is no competing spec. The refresh service is pointed at the
persistent path by an override appended to
`/etc/nvidia-container-toolkit/nvidia-cdi-refresh.env`:

```
NVIDIA_CTK_CDI_OUTPUT_FILE_PATH=/etc/cdi/nvidia.yaml
```

**3. `nvidia-cdi-refresh` is ENABLED**, both `.service` and `.path`. This is what
keeps the majors current, and it is the point where the two causes conflict:
cause A's early workaround said to disable this service, and doing so is what
allowed cause B to recur on every boot. Leave it enabled.

**4. The hook is stripped after every regeneration**, via drop-in
`/etc/systemd/system/nvidia-cdi-refresh.service.d/50-strip-compat.conf`:

```ini
[Service]
ExecStartPost=/usr/bin/python3 /usr/local/sbin/strip-cuda-compat.py
```

Without this, step 3 would defeat step 1: a freshly generated spec re-includes
the panicking hook.

`/usr/local/sbin/strip-cuda-compat.py` is a **copy** of
`docker/strip_compat.py`. It must live outside `/home`: the unit runs with
`CapabilityBoundingSet=CAP_SYS_MODULE CAP_SYS_ADMIN CAP_MKNOD` — root *without*
`CAP_DAC_OVERRIDE` — so it cannot traverse the 750-mode home directory.
Referencing the repo copy fails with `EACCES`, and that failure leaves a freshly
generated **hooked** spec live.

⚠️ **The copy does not track the repo.** After editing
`docker/strip_compat.py`, reinstall it:

```bash
sudo install -m 644 ~/smart_spotter/orin/docker/strip_compat.py \
  /usr/local/sbin/strip-cuda-compat.py
```

(Verified identical on 2026-08-18.)

### Running containers

Because injection comes from the CDI spec, the device must be requested by CDI
name. Without this env var the runtime falls back to the path that panics:

```bash
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  smart-spotter-orin:dev
```

The production inference run adds `--network host` for the GigE data plane.

---

## Health check

Run this after any reboot, driver change, or system update:

```bash
# 1. Runtime mode
grep -n '^mode' /etc/nvidia-container-runtime/config.toml     # mode = "cdi"

# 2. Refresh service enabled, and its last run succeeded
systemctl is-enabled nvidia-cdi-refresh.service               # enabled
systemctl show nvidia-cdi-refresh.service -p Result           # Result=success
# "inactive (dead)" is correct for a oneshot that finished. "failed" is not.

# 3. Hook is gone from the spec
grep -c enable-cuda-compat /etc/cdi/nvidia.yaml               # 0

# 4. No competing tmpfs spec
ls /var/run/cdi/ 2>&1                    # No such file or directory

# 5. Spec majors match the live kernel devices
ls -la /dev/nvgpu/igpu0/ctrl                                  # e.g. "493, 16"
grep -A1 'path: /dev/nvgpu/igpu0/ctrl$' /etc/cdi/nvidia.yaml  # major: 493

# 6. End to end
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --entrypoint python3 smart-spotter-orin:dev \
  -c "from cuda.bindings import runtime as c; print(c.cudaGetDeviceCount())"
#   want: (cudaError_t.cudaSuccess, 1)
```

Whichever check fails points at what changed. Check 5 failing is cause B; check 3
failing is cause A about to happen on the next container start.

## Manual recovery, one boot

If the spec is stale or hooked and you need containers working now:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo python3 /usr/local/sbin/strip-cuda-compat.py
```

The strip step is **mandatory** — a freshly generated spec always re-includes the
hook. Do not pass `--mode=cdi` to `cdi generate`: `--mode` there selects the
*discovery* mode (`auto|csv|nvml|…`), and `cdi` is only meaningful as the
*runtime* mode in `config.toml`.

## Rebuilding the durable setup from scratch

```bash
sudo install -m 644 -o root -g root \
  ~/smart_spotter/orin/docker/strip_compat.py /usr/local/sbin/strip-cuda-compat.py
# add NVIDIA_CTK_CDI_OUTPUT_FILE_PATH to the .env file, and write the drop-in
# (both shown under "Current state")
sudo systemctl daemon-reload
sudo systemctl enable --now nvidia-cdi-refresh.service
sudo systemctl enable nvidia-cdi-refresh.path
sudo nvidia-ctk config --in-place --set nvidia-container-runtime.mode=cdi
```

---

## Removing the workaround

The strip script `assert`s when the `enable-cuda-compat` hook is absent. That is
intentional: once a toolkit past 1.19.1 lands and drops the hook,
`nvidia-cdi-refresh.service` starts **failing loudly** instead of silently doing
nothing. That failure is the signal to remove the workaround:

```bash
sudo rm /etc/systemd/system/nvidia-cdi-refresh.service.d/50-strip-compat.conf
sudo rm /usr/local/sbin/strip-cuda-compat.py
sudo systemctl daemon-reload && sudo systemctl restart nvidia-cdi-refresh.service
```

Keep the env-file output-path override — it is harmless and keeps a single spec
location. `mode = "cdi"` can also stay; reverting it to `auto` is optional and
untested here.

Check `apt-cache policy nvidia-container-toolkit` before assuming a fix is
available. As of 2026-08-18 the candidate is still 1.19.1-1.

## Notes — do not re-diagnose these

- `groups: cannot find name for group ID NNN` on container start is cosmetic:
  host GIDs with no matching entry in the container's `/etc/group`. Ignore.
- Rebuilding the image (`docker build`) does **not** affect any of this. It is
  all host-side. No need to re-run anything after a rebuild.
- `/etc/cdi/nvidia.yaml.bak-20260702` is a backup from the cause-B incident.
  Harmless; it defines no devices the runtime reads.
- **Open item:** `Dockerfile` step 0 removes `/usr/local/cuda*/compat`. The
  "what did NOT work" list above says this is inert, since the hook runs
  regardless of image contents — but that has never been tested by actually
  removing the layer. A rebuild without it will settle the question; record the
  result here.
