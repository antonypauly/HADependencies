#!/usr/bin/env python3
"""usage: make_index.py all_wheels.tsv OUT_DIR

Builds a PEP 503 simple index (OUT_DIR/simple/...) from the wheel list written
by list_wheels.sh, i.e. wheels from ALL releases, each linking to the release
it lives in. If the same filename appears in several releases, the first line
wins, which is the newest release since list_wheels.sh emits newest first.
"""
import os
import re
import sys

tsv, out = sys.argv[1:3]

packages = {}  # normalized name -> [(filename, url)]
seen = set()
with open(tsv) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        _tag, fn, url = line.split("\t")
        if fn in seen:
            continue
        seen.add(fn)
        norm = re.sub(r"[-_.]+", "-", fn.split("-")[0]).lower()
        packages.setdefault(norm, []).append((fn, url))

root = os.path.join(out, "simple")
os.makedirs(root, exist_ok=True)

with open(os.path.join(root, "index.html"), "w") as f:
    f.write("<!DOCTYPE html><html><body>\n")
    for name in sorted(packages):
        f.write(f'<a href="{name}/">{name}</a><br>\n')
    f.write("</body></html>\n")

for name, files in packages.items():
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write("<!DOCTYPE html><html><body>\n")
        for fn, url in sorted(files):
            f.write(f'<a href="{url}">{fn}</a><br>\n')
        f.write("</body></html>\n")

print(f"Indexed {len(seen)} wheels for {len(packages)} packages", file=sys.stderr)
