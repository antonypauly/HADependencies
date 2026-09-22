# HA armv7 Wheels

Prebuilt Python wheels for `linux_armv7l`, built for packages that don't
publish official armv7 wheels on PyPI. Intended for Home Assistant on a
Raspberry Pi (DietPi) but usable by anyone on a matching platform.

> **Status:** untested. Install/usage instructions will be added once
> the wheels have been verified on real hardware.

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
