"""Remove the enable-cuda-compat hook from the host's CDI spec.

Runs on the HOST, as root — not in the container; the Dockerfile never copies
this in. nvidia-container-toolkit 1.19.1 ships a cudacompat hook that panics
parsing an ELF header on Orin, so any container started with --runtime nvidia
dies before it runs. Deleting the hook block from /etc/cdi/nvidia.yaml is the
workaround.

Must run after every `nvidia-ctk cdi generate`, because a fresh spec puts the
hook back. nvidia-cdi-refresh.service does that automatically via an
ExecStartPost drop-in, which calls the installed copy at
/usr/local/sbin/strip-cuda-compat.py — keep that copy in sync with this file.

The asserts are deliberate: once a fixed toolkit drops the hook, this fails
loudly and the service goes red, which is the signal to remove the workaround.

Full background: docs/environment/CDI-GPU-ACCESS.md
"""

p = "/etc/cdi/nvidia.yaml"
with open(p) as f:
    lines = f.read().splitlines(keepends=True)

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

# Block items are indented; the block runs until the next line at the SAME
# indent as the '- hookName' line (next list item) or a dedent.
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

new = lines[:start] + lines[end:]
with open(p, "w") as f:
    f.write("".join(new))
print("done")
