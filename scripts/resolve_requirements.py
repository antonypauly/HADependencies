#!/usr/bin/env python3
"""usage: resolve_requirements.py PY_VERSION combined_requirements.txt to_build.txt needs_review.txt

Replaces the bash/curl loop in the "Check PyPI metadata" step.

For every DIRECT requirement (no transitive resolution):
  1. Drop it if its environment marker is false for the target
     (linux / armv7l / cpython PY_VERSION).
  2. Pick the version:
       - "name==X"            -> X, exactly as pinned
       - ranges (>=, <, ~=, !=, no specifier, ...) -> the highest PyPI release
         that satisfies the specifier, is not fully yanked, and supports
         PY_VERSION (requires-python). Pre-releases are only used if nothing
         else matches, like pip.
  3. Look at that version's files on PyPI. If any wheel is usable on
     armv7 + PY_VERSION (pure-python, cpXY armv7l, abi3 armv7l, py3 armv7l),
     nothing to build. Otherwise write "name==version" to to_build.txt.

Anything that can't be handled (bad syntax, URL requirements, not on PyPI,
lookup failure, nothing satisfies the range) goes to needs_review.txt with a
"# reason" suffix.
"""
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

py_version, reqs_path, build_out, review_out = sys.argv[1:5]

PY = Version(py_version)
MINOR = PY.minor
CPTAG = "cp" + py_version.replace(".", "")

MARKER_ENV = {
    "python_version": py_version,
    "python_full_version": f"{py_version}.0",
    "implementation_version": f"{py_version}.0",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "platform_machine": "armv7l",
    "platform_system": "Linux",
    "sys_platform": "linux",
    "os_name": "posix",
}

SIMPLE_URL = "https://pypi.org/simple/{}/"
HEADERS = {
    "Accept": "application/vnd.pypi.simple.v1+json",
    "Accept-Encoding": "gzip",
    "User-Agent": "ha-wheels-build/1.0",
}


# ---------------------------------------------------------------- PyPI access
def fetch(name):
    """List of file dicts for a project; None if it is not on PyPI."""
    url = SIMPLE_URL.format(name)
    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data)["files"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # network hiccup, bad JSON, ...
            err = e
        time.sleep(2 * (attempt + 1))
    raise err


def safe_fetch(name):
    try:
        return fetch(name)
    except Exception as e:
        return e


def versions_of(files):
    """{Version: [file dicts]} built from the filenames of a project's files."""
    out = {}
    for f in files:
        fn = f["filename"]
        try:
            ver = (parse_wheel_filename(fn) if fn.endswith(".whl") else parse_sdist_filename(fn))[1]
        except Exception:
            continue  # .egg, .exe, odd names
        out.setdefault(ver, []).append(f)
    return out


# ------------------------------------------------------------ compatibility
def interp_ok(tag):
    i = tag.interpreter
    if i == CPTAG:
        return not tag.abi.endswith("t")  # skip free-threaded builds
    m = re.fullmatch(r"py3(\d*)", i)
    if m:
        return not m.group(1) or int(m.group(1)) <= MINOR
    m = re.fullmatch(r"cp3(\d+)", i)
    return bool(m) and tag.abi == "abi3" and int(m.group(1)) <= MINOR


def platform_ok(tag):
    p = tag.platform
    return p == "any" or ("armv7l" in p and "musllinux" not in p)


def wheel_usable(filename):
    try:
        tags = parse_wheel_filename(filename)[3]
    except Exception:
        return False
    return any(interp_ok(t) and platform_ok(t) for t in tags)


def release_eligible(files):
    """At least one non-yanked file that supports the target Python."""
    for f in files:
        if f.get("yanked"):
            continue
        rp = f.get("requires-python")
        if rp:
            try:
                if not SpecifierSet(html.unescape(rp)).contains(PY, prereleases=True):
                    continue
            except InvalidSpecifier:
                pass
        return True
    return False


def exact_pin(spec):
    specs = list(spec)
    if len(specs) == 1 and specs[0].operator == "==" and not specs[0].version.endswith(".*"):
        return specs[0].version
    return None


# --------------------------------------------------------------------- main
def parse_requirements(path):
    """[(raw_line, Requirement | None, problem | None)]; markers already applied."""
    items = []
    with open(path) as f:
        for raw in f:
            line = re.sub(r"\s+#.*$", "", raw.strip()).strip()
            if not line:
                continue
            if line.startswith("-"):
                items.append((line, None, "option line, not a requirement"))
                continue
            try:
                req = Requirement(line)
            except InvalidRequirement as e:
                items.append((line, None, f"cannot parse: {e}"))
                continue
            if req.url:
                items.append((line, None, "direct URL requirement"))
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate(MARKER_ENV):
                        continue  # not needed on this target
                except Exception:
                    pass  # can't evaluate -> keep it
            items.append((line, req, None))
    return items


def main():
    items = parse_requirements(reqs_path)
    names = sorted({canonicalize_name(r.name) for _, r, _ in items if r})
    print(f"{len(items)} requirements apply to this target, {len(names)} distinct packages")

    with ThreadPoolExecutor(max_workers=8) as pool:
        index = dict(zip(names, pool.map(safe_fetch, names)))

    to_build, review = set(), []
    skipped = 0

    for line, req, problem in items:
        if problem:
            review.append(f"{line}  # {problem}")
            continue

        files = index[canonicalize_name(req.name)]
        if isinstance(files, Exception):
            review.append(f"{line}  # PyPI lookup failed: {files}")
            continue
        if files is None:
            review.append(f"{line}  # not found on PyPI")
            continue

        if any(s.operator == "===" for s in req.specifier):
            review.append(f"{line}  # arbitrary equality (===) not supported")
            continue

        versions = versions_of(files)
        pin = exact_pin(req.specifier)

        if pin is not None:
            try:
                chosen = Version(pin)
            except InvalidVersion:
                review.append(f"{line}  # invalid pinned version")
                continue
            if chosen not in versions:
                review.append(f"{line}  # version {pin} not on PyPI")
                continue
            version_str = pin
        else:
            candidates = [v for v, fl in versions.items() if release_eligible(fl)]
            matching = list(req.specifier.filter(candidates))
            if not matching:
                review.append(f"{line}  # no release satisfies the specifier for Python {py_version}")
                continue
            chosen = max(matching)
            version_str = str(chosen)
            print(f"Resolved {line} -> {req.name}=={version_str}")

        if any(f["filename"].endswith(".whl") and wheel_usable(f["filename"]) for f in versions[chosen]):
            skipped += 1
        else:
            to_build.add(f"{req.name}=={version_str}")

    with open(build_out, "w") as f:
        f.writelines(f"{r}\n" for r in sorted(to_build))
    with open(review_out, "w") as f:
        f.writelines(f"{r}\n" for r in review)

    print(f"usable wheel already on PyPI: {skipped}, to build: {len(to_build)}, needs review: {len(review)}")


if __name__ == "__main__":
    main()