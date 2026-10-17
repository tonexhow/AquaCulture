# AquaCulture — scan source QA handoff (2026-10-17)

## Tested locally

- 32 / 32 Python regression tests passed (`python3 -m unittest discover -s server/tests -v`) after temporarily supplying the unchanged database Python modules and catalog-only test data from the earlier source package. The `database/` folder and temporary test DB are **excluded** from this delivery ZIP.
- Frontend JavaScript parsed successfully (`node --check client/static/index/index.js`).
- Backend Python code compiled successfully (`python3 -m compileall -q server`).
- Background job accepts immediately; duplicate jobs are rejected; cancellation releases the busy lock; a successful model run with zero boxes returns no detection; all-model failure is not misreported as an empty scene; frame selection handles duplicate and blank images; 5/10 frame settings and frame winner ranking work with fake camera/model inputs.

## Not verified in this environment

- Raspberry Pi 4B live camera capture and timing, device permissions and serial connection.
- Real FishInv/MegaFauna NCNN inference or detection accuracy on fish images (model binaries excluded by request).
- End-to-end browser + Flask integration. Flask was not available in the QA environment and installation could not access the package index; Python route compilation, worker logic and JavaScript parsing were checked instead.
- Production tunneling/reverse-proxy setup, GitHub push, or Raspberry Pi restart.
- Physical fish length/weight accuracy; a calibrated reference or camera scale remains necessary.

## Review and deployment

This is a source-only update. Merge into the **same existing repository**, not a new nested AquaCulture directory. Preserve `.env`, `models/`, `database/`, `driver/`, `network/`, .venv and any user data. Run the included test suite and a real empty-scene + fish-scene test on the Pi before treating it as production-ready.

The 3/5/10-second setting controls camera recording only. NCNN inference runs after recording, with a separate bounded processing timeout and visible progress. A completed scan is not guaranteed to finish in 3/5/10 seconds on a Pi 4B.
