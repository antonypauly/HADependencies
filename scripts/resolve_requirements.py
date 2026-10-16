#!/usr/bin/env python3
"""
Decide which packages need an ARMv7 wheel built.

usage:
  resolve_requirements.py PY_VERSION combined_requirements.txt \
      to_build.txt needs_review.txt [-c constraints.txt] [options]

Steps:
  1. Resolve the full dependency tree with `uv pip compile --universal`.
     uv has no ARMv7 target for --python-platform, so it resolves for all
     platforms and annotates each pin with its environment marker. The
     markers are then evaluated against an armv7l/Linux/CPython
     environment, dropping pins that don't apply. uv's resolver handles
     extras, backtracking, requires-python and yanked releases.
     If the combined resolution fails (conflicting pins, unsupported
     Python version, ...) the script stops with uv's error and a non-zero
     exit code. There is deliberately no fallback: HA's requirements are
     meant to be installable together, so a failure here needs fixing, not
     working around.
  2. For each resolved name==version, look at the files on PyPI and decide
     whether a usable ARMv7 wheel exists (interpreter tag, platform tag,
     glibc version). If not, write name==version to to_build.txt.

Requires: uv on PATH, and the `packaging` library.
"""

import argparse
import gzip
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("py_version", help="Target CPython version, e.g. 3.14")
parser.add_argument("requirements", help="Direct requirements file")
parser.add_argument("build_out", help="Output: name==version to build")
parser.add_argument("review_out", help="Output: items needing review")
parser.add_argument(
    "-c", "--constraints", help="Constraints file (HA package_constraints.txt)"
)
parser.add_argument(
    "--glibc",
    default=None,
    help="glibc version of the target OS; manylinux wheels needing a newer "
    "glibc are treated as unusable. Default: detected from the OS the "
    "script runs on, so run it inside the same image you build wheels in "
    "(e.g. Debian 12 = 2.36).",
)
parser.add_argument(
    "--workers", type=int, default=8, help="Parallel workers (default: 8)"
)
args = parser.parse_args()

PY = Version(args.py_version)
MINOR = PY.minor
CPTAG = "cp" + args.py_version.replace(".", "")


def detect_glibc():
    """glibc version of the OS this script runs on, e.g. '2.36'."""
    try:
        import os

        name, version = os.confstr("CS_GNU_LIBC_VERSION").split()
        if name == "glibc":
            return version
    except (AttributeError, ValueError, OSError):
        pass
    return None


if args.glibc is None:
    args.glibc = detect_glibc()
    if args.glibc is None:
        sys.exit("Could not detect glibc (non-glibc OS?); pass --glibc X.Y")
    print(f"Detected glibc {args.glibc} from the OS")

GLIBC = tuple(int(x) for x in args.glibc.split(".")[:2])

MARKER_ENV = {
    "python_version": args.py_version,
    "python_full_version": f"{args.py_version}.0",
    "implementation_name": "cpython",
    "implementation_version": f"{args.py_version}.0",
    "platform_python_implementation": "CPython",
    "platform_machine": "armv7l",
    "platform_system": "Linux",
    "platform_release": "",
    "platform_version": "",
    "sys_platform": "linux",
    "os_name": "posix",
    "extra": "",
}

SIMPLE_URL = "https://pypi.org/simple/{}/"
HEADERS = {
    "Accept": "application/vnd.pypi.simple.v1+json",
    "Accept-Encoding": "gzip",
    "User-Agent": "ha-wheels-build/2.0",
}

# manylinux legacy aliases -> glibc version
LEGACY_MANYLINUX = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}

# ---------------------------------------------------------------------------
# uv resolution
# ---------------------------------------------------------------------------

# Universal resolution normally has to succeed for EVERY platform (even
# macOS/Windows), so a conflict that only exists there (e.g. a package with
# no x86_64 macOS wheel) fails the whole run. Restricting uv's environments
# to armv7l/Linux avoids that. `environments` is only read from a
# pyproject.toml, so write a throwaway project and point uv at it.
_ENV_DIR = tempfile.mkdtemp(prefix="uv-armv7-env-")
Path(_ENV_DIR, "pyproject.toml").write_text(
    f'''[project]
name = "armv7-env"
version = "0"
requires-python = ">={args.py_version}"

[tool.uv]
environments = ["sys_platform == 'linux' and platform_machine == 'armv7l'"]
'''
)


def uv_compile(req_file, constraints):
    """Run `uv pip compile`. Returns (pins, error). pins = set of 'name==ver'."""
    cmd = [
        "uv", "pip", "compile", str(req_file),
        "--python-version", args.py_version,
        "--universal",
        "--project", _ENV_DIR,
        "--no-header", "--no-annotate", "--quiet",
    ]
    if constraints:
        cmd += ["-c", constraints]

    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0:
        err = proc.stderr.strip()
        return None, err or f"uv exited with {proc.returncode}"

    pins = set()
    for raw in proc.stdout.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        specs = list(req.specifier)
        if req.url or len(specs) != 1 or specs[0].operator != "==":
            continue
        if req.marker is not None:
            try:
                if not req.marker.evaluate(MARKER_ENV):
                    continue  # pin does not apply to armv7l/Linux
            except Exception:
                pass  # can't evaluate: keep it rather than drop it
        pins.add(f"{canonicalize_name(req.name)}=={specs[0].version}")
    return pins, None


def fail_resolution(message):
    """Write empty outputs plus the error to the review file, then exit 1."""
    Path(args.build_out).write_text("")
    Path(args.review_out).write_text(
        "".join(f"# {line}\n" for line in message.splitlines())
    )
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# PyPI wheel check
# ---------------------------------------------------------------------------

_cache = {}


def fetch_files(name):
    key = canonicalize_name(name)
    if key in _cache:
        return _cache[key]

    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(SIMPLE_URL.format(key), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                _cache[key] = json.loads(data)["files"]
                return _cache[key]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                _cache[key] = None
                return None
            err = exc
        except Exception as exc:
            err = exc
        time.sleep(2 * (attempt + 1))
    raise err


SDIST_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip")


def file_version(filename, project):
    """Version of a PyPI file, or None. `project` is the canonical name."""
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
    except Exception:
        return None

    try:
        return parse_sdist_filename(filename)[1]
    except Exception:
        pass

    # Fallback for names like "pybbox-0.0.5-alpha.zip": try every "-"
    # split until the prefix matches the project name.
    base = filename
    for ext in SDIST_EXTS:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    for i, ch in enumerate(base):
        if ch == "-" and canonicalize_name(base[:i]) == project:
            try:
                return Version(base[i + 1 :])
            except Exception:
                return None
    return None


def interp_ok(tag):
    interp = tag.interpreter
    if interp == CPTAG:
        return not tag.abi.endswith("t")  # no free-threaded builds
    m = re.fullmatch(r"py3(\d*)", interp)
    if m:
        return not m.group(1) or int(m.group(1)) <= MINOR
    m = re.fullmatch(r"cp3(\d+)", interp)
    if m:
        return tag.abi == "abi3" and int(m.group(1)) <= MINOR
    return False


def platform_ok(tag):
    plat = tag.platform
    if plat == "any":
        return True
    if "armv7l" not in plat or "musllinux" in plat:
        return False

    # glibc check for manylinux tags
    m = re.match(r"manylinux_(\d+)_(\d+)_", plat)
    if m:
        return (int(m.group(1)), int(m.group(2))) <= GLIBC
    for legacy, glibc in LEGACY_MANYLINUX.items():
        if plat.startswith(legacy + "_"):
            return glibc <= GLIBC
    return True  # plain linux_armv7l


def wheel_usable(filename):
    try:
        tags = parse_wheel_filename(filename)[3]
    except Exception:
        return False
    return any(interp_ok(t) and platform_ok(t) for t in tags)


def check(pin):
    name, version = pin.split("==")
    try:
        files = fetch_files(name)
    except Exception as exc:
        return pin, "review", f"PyPI lookup failed: {exc}"
    if files is None:
        return pin, "review", "not found on PyPI"

    ver = Version(version)
    key = canonicalize_name(name)
    release = [f for f in files if file_version(f["filename"], key) == ver]
    if not release:
        return pin, "review", f"version {version} not on PyPI"

    if any(
        f["filename"].endswith(".whl") and wheel_usable(f["filename"])
        for f in release
    ):
        return pin, "usable", None

    has_wheels = any(f["filename"].endswith(".whl") for f in release)
    return pin, "build", "wheels exist but none usable" if has_wheels else "sdist only"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    review = []

    print("Resolving combined requirements with uv...", flush=True)
    pins, err = uv_compile(args.requirements, args.constraints)
    if pins is None:
        fail_resolution(f"uv could not resolve the combined requirements:\n{err}")
    if not pins:
        fail_resolution("uv resolved no packages")
    print(f"  resolved {len(pins)} package versions")

    print(f"Checking ARMv7 wheels for {len(pins)} package versions "
          f"(glibc <= {args.glibc})...", flush=True)

    to_build, usable = {}, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(check, p) for p in sorted(pins)]):
            pin, result, note = fut.result()
            if result == "usable":
                usable += 1
            elif result == "build":
                to_build[pin] = note
            else:
                review.append(f"{pin}  # {note}")

    Path(args.build_out).write_text(
        "".join(f"{p}\n" for p in sorted(to_build))
    )
    Path(args.review_out).write_text(
        "".join(f"{r}\n" for r in sorted(set(review)))
    )

    print("\nResolution complete\n-------------------")
    print(f"Resolved package versions: {len(pins)}")
    print(f"Usable ARMv7 wheels:       {usable}")
    print(f"Wheels to build:           {len(to_build)}")
    print(f"Needs review:              {len(set(review))}")

    if to_build:
        print("\nPackages requiring ARMv7 wheels:")
        for p in sorted(to_build):
            print(f"  {p}  ({to_build[p]})")
    if review:
        print("\nPackages requiring review:")
        for r in sorted(set(review)):
            print(f"  {r}")


if __name__ == "__main__":
    main()