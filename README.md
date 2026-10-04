# HA armv7 Wheels

Prebuilt Python wheels for `linux_armv7l` (32-bit ARM), published as a
[PEP 503](https://peps.python.org/pep-0503/) package index. They cover
packages that don't publish official armv7 wheels on PyPI, so Home
Assistant Core on a 32-bit Raspberry Pi running **DietPi** can install
everything as prebuilt wheels instead of compiling on the device.

> **Status:** unofficial and community-maintained. Tested on a Raspberry
> Pi 2B running DietPi. Other boards, operating systems
> and Python versions are untested.

## Why this exists

Home Assistant depends on many Python packages (numpy, cryptography,
cffi, pycares, av, ...). PyPI doesn't publish armv7 wheels for many of
them, so `uv`/`pip` falls back to compiling from source on the device.
On a 32-bit Raspberry Pi that is very slow, needs a full compiler
toolchain, and often fails.

Home Assistant has deprecated 32-bit ARM and the Core install method, but
many people still run them on hardware that works fine. This repo builds
the missing wheels in CI so those systems keep working.

- **Target:** `linux/arm/v7`, glibc
- **Wheels:** attached to GitHub Releases, one per Home Assistant version (tag `ha-wheels-<version>`)
- **Index:** served via GitHub Pages and covers wheels from all releases

## Usage (DietPi)

DietPi installs Home Assistant Core into `/opt/homeassistant/.venv` and
uses `uv`. To make it pull wheels from this index:

### 1. Check that your system matches

```bash
uname -m    # must print armv7l
ldd --version
```

### 2. Add the index to uv's config

Add the following to your uv config file (for example `/etc/uv/uv.toml`):

```toml
index-strategy = "unsafe-best-match"

[[index]]
url = "https://antonypauly.github.io/HADependencies/simple/"

[[index]]
url = "https://www.piwheels.org/simple"
```

[piwheels](https://www.piwheels.org/) is optional and third-party; it
fills in packages this repo doesn't build.

### 3. Install or reinstall Home Assistant

```bash
dietpi-software reinstall 157
```

### Troubleshooting

- **A package gets compiled or the install fails:** check that its wheel
  exists at `https://antonypauly.github.io/HADependencies/simple/<package>/`
  and that your Python version and glibc match.
- **`ImportError` after install:** open an issue with the package name,
  Python version and `ldd --version` output.
- **Missing wheel:** open an issue with the package name so it can be
  added to the build.

### Other systems

The wheels are plain `linux_armv7l` glibc wheels, so they may also work
on other 32-bit Debian-based systems with Python 3.14, but this is
untested and unsupported.

## How it works

1. A scheduled workflow detects new Home Assistant releases.
2. The build workflow fetches that release's requirements.
3. Packages that already have a wheel (here or on PyPI) are skipped.
4. Missing ones are built in a per-package matrix (Docker + QEMU), checked
   with an import test, and published to a release.
5. The GitHub Pages index is regenerated to include all releases.

See [`.github/workflows/build-ha-wheels.yml`](.github/workflows/build-ha-wheels.yml)
for the full pipeline. To trigger a build manually, go to
Actions → "Build HA Wheels for armv7" → Run workflow (optionally set
`homeassistant_version`).

## Limitations

- Unofficial and not affiliated with Home Assistant or the Open Home Foundation.
- Only armv7 and one Python version are built at a time.
- Some wheels may be missing if a build failed.
- 32-bit ARM is no longer supported by Home Assistant itself; this may
  break at any time as upstream packages drop 32-bit support.

## Licensing

Most wheels here are rebuilds of permissively-licensed packages (BSD, MIT,
Apache, etc.) — no different from installing them from PyPI, just compiled
for armv7.

**One exception: the `av` (PyAV) wheel.**

That wheel is built against an FFmpeg binary that includes `x264` and
`x265`, which are GPL-licensed. Because of that, the compiled `av` wheel
in this repo is distributed under the **GPL-3.0**, not PyAV's own
BSD-3-Clause license.

- License text: [`LICENSE-av.txt`](./LICENSE-av.txt)
- FFmpeg build used: [PyAV-Org/pyav-ffmpeg](https://github.com/PyAV-Org/pyav-ffmpeg), tag `8.1.2-1`
- FFmpeg source: [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg)
- PyAV source: [PyAV-Org/PyAV](https://github.com/PyAV-Org/PyAV)

If you only need HA integrations that don't require `av`, you can ignore
this and use the rest of the index as normal — the GPL terms apply only
to that one wheel.

## Building locally

See [`.github/workflows/build-ha-wheels.yml`](./.github/workflows/build-ha-wheels.yml)
for the full build pipeline (Docker/QEMU-based, matrix per package).
