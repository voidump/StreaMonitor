# Vendored front-end libraries

## mpegts.js

- Upstream: https://github.com/xqq/mpegts.js (Apache-2.0)
- File: `mpegts.js` (minified production build)
- Provenance: vendored in commit 96c27b2 without a recorded version. The
  minified file carries no version string; treat it as "a 1.7.x-era build"
  until replaced.
- Upgrade procedure (also fixes the unknown-version problem):
  1. `npm pack mpegts.js@<version>` (or download the release `dist/mpegts.js`)
  2. Copy `dist/mpegts.js` here, update this file with the exact version,
     download URL and `shasum -a 256 mpegts.js` output.
| Date | Version | sha256 |
|------|---------|--------|
| 2026-08-23 | unknown (pre-existing) | `e998f7a6cec5fcab56ac14e8c22d61a42565c1ebf7174b3fda078343eda6cab0` |
