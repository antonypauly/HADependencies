#!/usr/bin/env python3
"""usage: filter_built.py CPTAG all_wheels.tsv to_build.txt

Reads name==version requirements from to_build.txt and prints (stdout) only
those that have NO matching wheel in any release. Skipped ones go to stderr.

A published wheel counts as a match when its parsed (name, version) equals the
requirement's AND it is usable for this Python: interpreter tag == CPTAG
(e.g. cp313), abi3, or pure-interpreter tags like py3 (e.g. the uv wheel that
is downloaded from PyPI rather than built).
"""
import sys

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

cptag, tsv, reqs = sys.argv[1:4]

built = set()
with open(tsv) as f:
    for line in f:
        fn = line.rstrip("\n").split("\t")[1]
        try:
            name, ver, _build, tags = parse_wheel_filename(fn)
        except Exception:
            continue
        if any(
            t.interpreter == cptag or t.abi == "abi3" or t.interpreter.startswith("py")
            for t in tags
        ):
            built.add((name, ver))

with open(reqs) as f:
    for line in f:
        req = line.strip()
        if not req:
            continue
        name, _, ver = req.partition("==")
        try:
            key = (canonicalize_name(name), Version(ver))
        except InvalidVersion:
            print(req)  # can't parse it -> safer to build it
            continue
        if key in built:
            print(f"Already built, skipping: {req}", file=sys.stderr)
        else:
            print(req)
