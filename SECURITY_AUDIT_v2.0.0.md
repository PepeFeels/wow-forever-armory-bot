# v2.0.0 dependency audit

Audit date: 2026-11-14
Tool: `pip-audit 2.10.1`, installed in an isolated virtual environment under
`/tmp`; the project runtime dependencies were not installed or modified by the
tool setup. The runtime and build hash locks were audited separately.

## Findings before hardening

The runtime lock contained 12 packages. The two direct runtime dependencies,
`discord.py==2.7.1` and `Pillow==13.0.0`, and the ten transitive runtime
dependencies had no known vulnerabilities in the audit database at the time of
the scan.

The build lock contained one direct build dependency, `setuptools==80.9.0`.
The scanner returned three records representing two unique advisories:

- `PYSEC-2026-3447` (also `CVE-2026-59890` and `GHSA-h35f-9h28-mq5c`):
  setuptools versions before 83.0.0 can miss an `sdist` exclusion when Unicode
  filenames are normalized differently on APFS or HFS+. This requires source
  distribution creation on an affected filesystem and matching exclusion
  rules.
- `GHSA-xmj3-9gf7-h52w`: a legacy wheel installation path on Windows can write
  namespace-package metadata outside the requested destination when handling
  attacker-controlled wheel metadata.

The findings were in the direct build dependency, not in runtime packages.
The project builds on Linux x86_64 and installs hash-locked wheels; the audited
scenarios were not the normal production installation path. They still affect
the supported source-build workflow enough to justify a narrow build-tool
update. No runtime dependency was upgraded.

## Hardening applied

`setuptools` is now pinned to `84.1.0` in both `pyproject.toml` and
`deploy/requirements-build.lock`. This is the scanner's listed fix version for
both unique findings. The PyPI wheel SHA-256 was independently calculated as:

`5231689c6ac8156cd155cfb98c3fd6fb252c3e7e9bd3d78b64d3c55f132624c9`

The build lock is audited again after the update. This document records the
pre-update findings and must be refreshed when the lock changes or a later
vulnerability scan reports new results. A clean scan is time-bound; it does not
guarantee that future advisories will not affect these pins.

## Re-scan after hardening

After changing only the build backend to `setuptools==84.1.0`, `pip-audit`
reported no known vulnerabilities in either lock: 12 runtime packages and one
build package. The runtime direct dependencies remain `discord.py==2.7.1` and
`Pillow==13.0.0`; the ten remaining runtime packages are transitive. The build
lock contains the single direct build dependency, setuptools. No runtime
dependency or transitive package was changed to address the build-only findings.
