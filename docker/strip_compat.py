p = "/etc/cdi/nvidia.yaml"
with open(p) as f:
    lines = f.read().splitlines(keepends=True)

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
    # next sibling list item or a dedent ends the block
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
