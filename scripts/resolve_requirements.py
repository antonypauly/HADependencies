#!/usr/bin/env python3
"""
usage:
  resolve_requirements.py PY_VERSION combined_requirements.txt
                       [constraints.txt] to_build.txt needs_review.txt

Resolves direct and transitive dependencies for the target Python version,
then determines which exact package versions need an ARMv7 wheel built.

Arguments:
  PY_VERSION
      Target CPython version, e.g. 3.14

  combined_requirements.txt
      Direct requirements extracted from Home Assistant requirements files.

  constraints.txt
      Optional Home Assistant package constraints file.

  to_build.txt
      Output file containing exact package==version requirements that need
      an ARMv7 wheel.

  needs_review.txt
      Output file containing requirements that could not be resolved safely.

For every dependency:
  1. Evaluate environment markers for Linux/armv7l/CPython.
  2. Apply Home Assistant constraints where present.
  3. Resolve an exact version:
       - name==X -> X exactly
       - ranges -> highest compatible PyPI release
  4. Read Requires-Dist from that exact release.
  5. Recursively resolve its dependencies.
  6. Check whether a usable ARMv7/Python wheel exists.
  7. If not, write name==version to to_build.txt.
"""

import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

if len(sys.argv) == 5:
    # Backwards-compatible form:
    # resolve_requirements.py PY_VERSION requirements build_out review_out
    py_version, reqs_path, build_out, review_out = sys.argv[1:5]
    constraints_path = None
elif len(sys.argv) == 6:
    # New form:
    # resolve_requirements.py PY_VERSION requirements constraints build_out review_out
    py_version, reqs_path, constraints_path, build_out, review_out = sys.argv[
        1:6
    ]
else:
    print(
        "usage: resolve_requirements.py "
        "PY_VERSION combined_requirements.txt "
        "[constraints.txt] to_build.txt needs_review.txt",
        file=sys.stderr,
    )
    sys.exit(2)


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
JSON_URL = "https://pypi.org/pypi/{}/{}/json"

HEADERS = {
    "Accept": "application/vnd.pypi.simple.v1+json",
    "Accept-Encoding": "gzip",
    "User-Agent": "ha-wheels-build/1.0",
}

JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "ha-wheels-build/1.0",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_json(url, headers):
    """Fetch JSON with retries."""
    err = None

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)

            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()

                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)

                return json.loads(data)

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            err = exc

        except Exception as exc:
            err = exc

        time.sleep(2 * (attempt + 1))

    raise err


# ---------------------------------------------------------------------------
# PyPI access
# ---------------------------------------------------------------------------

_file_cache = {}
_metadata_cache = {}


def fetch(name):
    """
    Return the list of files for a PyPI project.

    None means the project does not exist on PyPI.
    """
    key = canonicalize_name(name)

    if key in _file_cache:
        return _file_cache[key]

    url = SIMPLE_URL.format(key)
    data = http_json(url, HEADERS)

    if data is None:
        _file_cache[key] = None
        return None

    files = data["files"]
    _file_cache[key] = files
    return files


def safe_fetch(name):
    try:
        return fetch(name)
    except Exception as exc:
        return exc


def fetch_release_metadata(name, version):
    """
    Fetch package metadata for one exact PyPI release.

    This is where Requires-Dist is obtained.
    """
    key = (canonicalize_name(name), str(version))

    if key in _metadata_cache:
        return _metadata_cache[key]

    url = JSON_URL.format(key[0], key[1])
    data = http_json(url, JSON_HEADERS)

    if data is None:
        _metadata_cache[key] = None
        return None

    metadata = data.get("info", {})
    _metadata_cache[key] = metadata
    return metadata


def safe_fetch_release_metadata(name, version):
    try:
        return fetch_release_metadata(name, version)
    except Exception as exc:
        return exc


# ---------------------------------------------------------------------------
# Version/file helpers
# ---------------------------------------------------------------------------

def versions_of(files):
    """
    Return:

        {Version: [file dicts]}

    based on PyPI filenames.
    """
    out = {}

    for item in files:
        filename = item["filename"]

        try:
            if filename.endswith(".whl"):
                version = parse_wheel_filename(filename)[1]
            else:
                version = parse_sdist_filename(filename)[1]
        except Exception:
            continue

        out.setdefault(version, []).append(item)

    return out


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

def interp_ok(tag):
    interpreter = tag.interpreter

    if interpreter == CPTAG:
        # Do not use free-threaded cp314t etc.
        return not tag.abi.endswith("t")

    match = re.fullmatch(r"py3(\d*)", interpreter)

    if match:
        # py3 / py3X universal Python wheels.
        return not match.group(1) or int(match.group(1)) <= MINOR

    match = re.fullmatch(r"cp3(\d+)", interpreter)

    if match:
        # CPython abi3 wheels.
        return tag.abi == "abi3" and int(match.group(1)) <= MINOR

    return False


def platform_ok(tag):
    platform = tag.platform

    return (
        platform == "any"
        or ("armv7l" in platform and "musllinux" not in platform)
    )


def wheel_usable(filename):
    try:
        tags = parse_wheel_filename(filename)[3]
    except Exception:
        return False

    return any(
        interp_ok(tag) and platform_ok(tag)
        for tag in tags
    )


def release_eligible(files):
    """
    Return True if the release has at least one non-yanked file whose
    requires-python permits the target Python.
    """
    for item in files:
        if item.get("yanked"):
            continue

        requires_python = item.get("requires-python")

        if requires_python:
            try:
                if not SpecifierSet(
                    html.unescape(requires_python)
                ).contains(PY, prereleases=True):
                    continue
            except InvalidSpecifier:
                pass

        return True

    return False


def exact_pin(specifier):
    """
    Return X for exactly:

        ==X

    but not:

        ==X.*
    """
    specs = list(specifier)

    if (
        len(specs) == 1
        and specs[0].operator == "=="
        and not specs[0].version.endswith(".*")
    ):
        return specs[0].version

    return None


# ---------------------------------------------------------------------------
# Requirement parsing
# ---------------------------------------------------------------------------

def marker_applies(req):
    """
    Evaluate a requirement marker against the ARMv7/CPython target.
    """
    if req.marker is None:
        return True

    try:
        return req.marker.evaluate(MARKER_ENV)
    except Exception:
        # Preserve the old behavior: if we cannot evaluate a marker,
        # don't silently discard the dependency.
        return True


def parse_requirement_line(line):
    """
    Parse one requirement line.

    Returns:
        Requirement
        None, problem
    """
    line = re.sub(r"\s+#.*$", "", line.strip()).strip()

    if not line:
        return None, None

    if line.startswith("-"):
        return None, "option line, not a requirement"

    try:
        req = Requirement(line)
    except InvalidRequirement as exc:
        return None, f"cannot parse: {exc}"

    if req.url:
        return None, "direct URL requirement"

    if not marker_applies(req):
        return None, "marker does not apply to target"

    return req, None


def parse_requirements(path):
    """
    Parse the direct requirements file.

    Returns:
        [(raw_line, Requirement | None, problem | None)]
    """
    items = []

    with open(path) as handle:
        for raw in handle:
            line = raw.strip()

            if not line:
                continue

            req, problem = parse_requirement_line(line)

            if req is None and problem == "marker does not apply to target":
                continue

            items.append((line, req, problem))

    return items


def parse_constraints(path):
    """
    Parse Home Assistant's constraints file.

    Constraints are grouped by canonical package name.

    Multiple constraints for the same package are combined.
    """
    constraints = {}

    if not path:
        return constraints

    with open(path) as handle:
        for raw in handle:
            line = re.sub(r"\s+#.*$", "", raw.strip()).strip()

            if not line:
                continue

            if line.startswith("-"):
                continue

            try:
                req = Requirement(line)
            except InvalidRequirement:
                continue

            if req.url:
                continue

            if not marker_applies(req):
                continue

            name = canonicalize_name(req.name)

            constraints.setdefault(name, []).append(req.specifier)

    return constraints


def combined_specifier(requirement, constraints):
    """
    Combine a requirement's specifier with all matching constraints.
    """
    parts = []

    requirement_spec = str(requirement.specifier)

    if requirement_spec:
        parts.append(requirement_spec)

    name = canonicalize_name(requirement.name)

    for constraint in constraints.get(name, []):
        value = str(constraint)

        if value:
            parts.append(value)

    if not parts:
        return SpecifierSet()

    try:
        return SpecifierSet(",".join(parts))
    except InvalidSpecifier as exc:
        raise ValueError(
            f"invalid combined specifier for {requirement.name}: "
            f"{','.join(parts)} ({exc})"
        )


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

def resolve_requirement(requirement, constraints, files=None):
    """
    Resolve one Requirement to:

        (chosen Version, version string, files)

    or:

        (None, None, reason)
    """
    name = canonicalize_name(requirement.name)

    if files is None:
        files = fetch(name)

    if isinstance(files, Exception):
        return None, None, f"PyPI lookup failed: {files}"

    if files is None:
        return None, None, "not found on PyPI"

    if any(spec.operator == "===" for spec in requirement.specifier):
        return None, None, "arbitrary equality (===) not supported"

    try:
        specifier = combined_specifier(requirement, constraints)
    except ValueError as exc:
        return None, None, str(exc)

    versions = versions_of(files)

    pin = exact_pin(requirement.specifier)

    # ------------------------------------------------------------------
    # Exact direct pin.
    #
    # Keep exact HA requirements exact. A constraint may narrow it, but
    # must not silently replace it with another version.
    # ------------------------------------------------------------------
    if pin is not None:
        try:
            chosen = Version(pin)
        except InvalidVersion:
            return None, None, "invalid pinned version"

        if chosen not in versions:
            return None, None, f"version {pin} not on PyPI"

        if not specifier.contains(chosen, prereleases=True):
            return (
                None,
                None,
                f"pinned version {pin} conflicts with constraints",
            )

        return chosen, pin, versions[chosen]

    # ------------------------------------------------------------------
    # Ranges / unconstrained requirements.
    # ------------------------------------------------------------------
    candidates = [
        version
        for version, release_files in versions.items()
        if release_eligible(release_files)
    ]

    matching = list(specifier.filter(candidates))

    if not matching:
        return (
            None,
            None,
            f"no release satisfies the specifier for Python {py_version}",
        )

    chosen = max(matching)
    version_str = str(chosen)

    return chosen, version_str, versions[chosen]


# ---------------------------------------------------------------------------
# Dependency traversal
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(self, constraints):
        self.constraints = constraints

        # canonical package name -> selected Version
        self.resolved = {}

        # canonical package name -> Requirement used to select it
        self.resolved_requirements = {}

        # canonical package name -> list of reasons
        self.review = []

        # Packages whose metadata has already been traversed.
        self.expanded = set()

    def add_review(self, requirement, reason):
        self.review.append(
            f"{requirement}  # {reason}"
        )

    def resolve(self, requirement, source=None):
        """
        Resolve one requirement and recursively resolve its dependencies.

        Returns the selected Version or None.
        """
        name = canonicalize_name(requirement.name)

        if not marker_applies(requirement):
            return None

        # Fetch package files.
        files = fetch(name)

        if isinstance(files, Exception):
            self.add_review(
                str(requirement),
                f"PyPI lookup failed: {files}",
            )
            return None

        if files is None:
            self.add_review(
                str(requirement),
                "not found on PyPI",
            )
            return None

        try:
            chosen, version_str, chosen_files = resolve_requirement(
                requirement,
                self.constraints,
                files,
            )
        except Exception as exc:
            self.add_review(
                str(requirement),
                f"resolution error: {exc}",
            )
            return None

        if chosen is None:
            self.add_review(
                str(requirement),
                version_str,
            )
            return None

        # If already resolved to the same version, no work is needed.
        existing = self.resolved.get(name)

        if existing is not None:
            if existing != chosen:
                self.add_review(
                    str(requirement),
                    (
                        f"dependency conflict: already resolved "
                        f"{name}=={existing}, but this requires "
                        f"{name}=={chosen}"
                    ),
                )
                return None

            return existing

        self.resolved[name] = chosen
        self.resolved_requirements[name] = requirement

        origin = (
            f" (required by {source})"
            if source
            else ""
        )

        print(
            f"Resolved {requirement}{origin} -> "
            f"{name}=={version_str}"
        )

        # ------------------------------------------------------------------
        # Recursively inspect Requires-Dist for this exact release.
        # ------------------------------------------------------------------
        metadata = fetch_release_metadata(name, version_str)

        if isinstance(metadata, Exception):
            self.add_review(
                f"{name}=={version_str}",
                f"metadata lookup failed: {metadata}",
            )
            return chosen

        if metadata is None:
            self.add_review(
                f"{name}=={version_str}",
                "release metadata not found on PyPI",
            )
            return chosen

        requires_dist = metadata.get("requires_dist") or []

        if not requires_dist:
            return chosen

        self.expanded.add(name)

        for dependency_line in requires_dist:
            try:
                dependency = Requirement(dependency_line)
            except InvalidRequirement as exc:
                self.add_review(
                    f"{name}=={version_str} -> {dependency_line}",
                    f"cannot parse dependency: {exc}",
                )
                continue

            if dependency.url:
                self.add_review(
                    f"{name}=={version_str} -> {dependency_line}",
                    "direct URL dependency",
                )
                continue

            # Evaluate environment markers.
            #
            # This target is CPython/Linux/armv7l, so dependencies such as:
            #
            #   foo; sys_platform == "win32"
            #
            # are ignored.
            if not marker_applies(dependency):
                continue

            self.resolve(
                dependency,
                source=f"{name}=={version_str}",
            )

        return chosen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    items = parse_requirements(reqs_path)
    constraints = parse_constraints(constraints_path)

    direct_requirements = []
    review = []

    for line, req, problem in items:
        if problem:
            review.append(f"{line}  # {problem}")
            continue

        direct_requirements.append(req)

    names = sorted(
        {
            canonicalize_name(req.name)
            for req in direct_requirements
        }
    )

    print(
        f"{len(direct_requirements)} direct requirements, "
        f"{len(names)} distinct packages"
    )

    if constraints_path:
        print(
            f"Loaded constraints from {constraints_path}: "
            f"{len(constraints)} packages"
        )

    resolver = Resolver(constraints)

    # Resolve direct dependencies concurrently at the top level.
    #
    # Recursive dependency expansion itself remains deterministic.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(resolver.resolve, req): req
            for req in direct_requirements
        }

        for future in as_completed(futures):
            req = futures[future]

            try:
                future.result()
            except Exception as exc:
                resolver.add_review(
                    str(req),
                    f"unexpected resolver error: {exc}",
                )

    review.extend(resolver.review)

    # ------------------------------------------------------------------
    # Now check every resolved package for a usable ARMv7 wheel.
    # ------------------------------------------------------------------

    to_build = set()
    skipped = 0

    resolved_items = sorted(
        resolver.resolved.items(),
        key=lambda item: item[0],
    )

    print(
        f"Checking ARMv7 wheels for "
        f"{len(resolved_items)} resolved packages..."
    )

    def check_wheel(name, version):
        files = fetch(name)

        if isinstance(files, Exception):
            return (
                name,
                version,
                "review",
                f"PyPI lookup failed: {files}",
            )

        if files is None:
            return (
                name,
                version,
                "review",
                "not found on PyPI",
            )

        versions = versions_of(files)

        if version not in versions:
            return (
                name,
                version,
                "review",
                f"version {version} not on PyPI",
            )

        release_files = versions[version]

        usable = any(
            item["filename"].endswith(".whl")
            and wheel_usable(item["filename"])
            for item in release_files
        )

        if usable:
            return name, version, "usable", None

        return name, version, "build", None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(check_wheel, name, version)
            for name, version in resolved_items
        ]

        for future in as_completed(futures):
            name, version, result, reason = future.result()

            requirement = f"{name}=={version}"

            if result == "usable":
                skipped += 1

            elif result == "build":
                to_build.add(requirement)

            else:
                review.append(
                    f"{requirement}  # {reason}"
                )

    # ------------------------------------------------------------------
    # Write outputs.
    # ------------------------------------------------------------------

    with open(build_out, "w") as handle:
        for requirement in sorted(to_build):
            handle.write(f"{requirement}\n")

    with open(review_out, "w") as handle:
        for item in sorted(set(review)):
            handle.write(f"{item}\n")

    print()
    print("Resolution complete")
    print("-------------------")
    print(f"Resolved packages:       {len(resolver.resolved)}")
    print(f"Usable ARMv7 wheels:     {skipped}")
    print(f"Wheels to build:         {len(to_build)}")
    print(f"Needs review:            {len(set(review))}")

    if to_build:
        print()
        print("Packages requiring ARMv7 wheels:")
        for requirement in sorted(to_build):
            print(f"  {requirement}")

    if review:
        print()
        print("Packages requiring review:")
        for item in sorted(set(review)):
            print(f"  {item}")


if __name__ == "__main__":
    main()