# GPU "unreachable" in containers — root cause & fix (2026-07-02)

## Symptom

Containers lose GPU access, typically after a reboot:

- `docker run --runtime nvidia … cudaGetDeviceCount()` segfaults (exit 139) or
  CUDA returns error `100` (`CUDA_ERROR_NO_DEVICE`).
- Host-level CUDA is **fine**: `nvidia-smi` works, `cuInit(0)` returns 0,
  TensorRT builds engines.
- dmesg shows DCE errors that look scary but are **unrelated** (see below).

## Red herring: the DCE boot errors

Every boot of this board logs:

```
dce: dce_admin_send_cmd_ver: version : dcefw:[0x4] dcekmd:[0x4] err:[0x0]
dce: dce_admin_setup_clients_ipc:978  Get queue info failed for [2]
NVRM: GPU0 rpcRmApiControl_dce: ... cmd:0x731341 result 0xffff [NV_ERR_GENERIC]
NVRM: GPU0 rpcRmApiControl_dce: ... cmd:0x730282 result 0x1f [NV_ERR_INVALID_ARGUMENT]
```

This signature has been present on **every boot since first provisioning**,
including all periods when inference worked. IPC channel type 2 is the DCE
*display* client; the 0x73xxxx RM calls are display-class controls. On this
headless board they fail harmlessly and have **zero effect on CUDA compute**.
Do not flash the board because of these lines.

## Actual root cause: stale device majors in the CDI spec

`/etc/cdi/nvidia.yaml` records each device node's **major/minor numbers at
generation time**, and the CDI runtime `mknod`s exactly those numbers inside
containers. The nvgpu nodes (`/dev/nvgpu/igpu0/*`, `/dev/nvhost-*-gpu`) use
**dynamically allocated majors that can change across reboots** (e.g. 494/495
on one boot, 493/494 on the next). `/dev/nvidia*` uses static major 195, which
is why host checks kept passing.

After a reboot reshuffles the majors, containers get device nodes pointing at
nonexistent kernel devices → `libcuda` (the correct, host-mounted
`nvgpu/libcuda.so.1.1`) finds no GPU → `CUDA_ERROR_NO_DEVICE` / segfault.

The service that exists to prevent exactly this — `nvidia-cdi-refresh` — had
been **disabled** as part of the nvidia-container-toolkit 1.19.1
`enable-cuda-compat` hook bug workaround, so the spec was never regenerated
and every reboot could re-break containers.

## Quick diagnosis

```bash
# Compare live majors vs the spec — a mismatch confirms this issue:
ls -la /dev/nvgpu/igpu0/ctrl                       # e.g. "493, 16"
grep -m1 -A1 "path: /dev/nvgpu/igpu0/ctrl$" /etc/cdi/nvidia.yaml   # "major: NNN"

# Confirm host GPU is fine (rules out driver/firmware):
python3 -c "import ctypes; print('cuInit:', ctypes.CDLL('libcuda.so.1').cuInit(0))"   # want 0
```

## Manual (one-boot) fix

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo python3 /usr/local/sbin/strip-cuda-compat.py
```

Notes:
- Do **not** pass `--mode=cdi` to `cdi generate` — `--mode` there is the
  *discovery* mode (`auto|csv|nvml|…`); `cdi` is only valid as the runtime
  mode in `/etc/nvidia-container-runtime/config.toml`.
- The strip step is mandatory: a freshly generated spec re-includes the
  `enable-cuda-compat` hook that panics under toolkit 1.19.1.

## Durable fix (implemented 2026-07-02)

`nvidia-cdi-refresh.service` + `.path` re-enabled, with two adjustments:

1. **Output path override** — appended to
   `/etc/nvidia-container-toolkit/nvidia-cdi-refresh.env`:

   ```
   NVIDIA_CTK_CDI_OUTPUT_FILE_PATH=/etc/cdi/nvidia.yaml
   ```

   The stock unit writes to `/var/run/cdi/nvidia.yaml`, which would create a
   second, competing spec. We keep a single spec in `/etc/cdi`.

2. **Strip after every regeneration** — drop-in
   `/etc/systemd/system/nvidia-cdi-refresh.service.d/50-strip-compat.conf`:

   ```ini
   [Service]
   ExecStartPost=/usr/bin/python3 /usr/local/sbin/strip-cuda-compat.py
   ```

   `/usr/local/sbin/strip-cuda-compat.py` is a **copy** of
   `~/smart_spotter/orin/docker/strip_compat.py`. It must live outside
   `/home` because the unit runs with
   `CapabilityBoundingSet=CAP_SYS_MODULE CAP_SYS_ADMIN CAP_MKNOD` — root
   *without* `CAP_DAC_OVERRIDE` — and cannot traverse the 750-mode home
   directory (referencing the repo copy fails with `EACCES`, and that failure
   leaves a freshly generated **hooked** spec live!).

With this in place the spec is regenerated (with current majors) and
re-stripped on every boot, and additionally whenever the driver modules or
`nvidia-ctk` change (via the `.path` unit).

Setup commands, for reference / re-doing from scratch:

```bash
sudo install -m 644 -o root -g root ~/smart_spotter/orin/docker/strip_compat.py /usr/local/sbin/strip-cuda-compat.py
# (write the env line and the drop-in as shown above)
sudo systemctl daemon-reload
sudo systemctl enable --now nvidia-cdi-refresh.service
sudo systemctl enable nvidia-cdi-refresh.path
```

## Verify after any reboot

```bash
systemctl status nvidia-cdi-refresh.service            # inactive (dead) after clean oneshot run; NOT "failed"
grep -c enable-cuda-compat /etc/cdi/nvidia.yaml         # want 0
ls /var/run/cdi/ 2>&1                                   # want: No such file or directory
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --entrypoint python3 smart-spotter-orin:dev \
  -c "from cuda.bindings import runtime as c; print(c.cudaGetDeviceCount())"   # want (cudaSuccess, 1)
```

## Two things to remember

1. **Script copy drift** — `/usr/local/sbin/strip-cuda-compat.py` does not
   track the repo. If you edit `docker/strip_compat.py`, re-run:

   ```bash
   sudo install -m 644 ~/smart_spotter/orin/docker/strip_compat.py /usr/local/sbin/strip-cuda-compat.py
   ```

2. **Planned obsolescence** — the strip script *asserts* if the
   `enable-cuda-compat` hook is absent, so after upgrading
   nvidia-container-toolkit past 1.19.1 (bug fixed upstream),
   `nvidia-cdi-refresh.service` will start **failing loudly**. That is the
   signal to remove the workaround:

   ```bash
   sudo rm /etc/systemd/system/nvidia-cdi-refresh.service.d/50-strip-compat.conf
   sudo rm /usr/local/sbin/strip-cuda-compat.py
   sudo systemctl daemon-reload && sudo systemctl restart nvidia-cdi-refresh.service
   ```

   (Keep the env-file output-path override — it's harmless and keeps a single
   spec location.)
