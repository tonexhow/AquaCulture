# AquaCulture Phase 2C — Settings, mobile scanning & file storage

## Scope and compatibility

- Redesigned Settings / Records navigation, notification cards, species data tables,
  reference-only catalog, archived species and responsive mobile controls.
- Maintained the legacy notification configuration structure, alert thresholds,
  save actions and subscriber APIs. Recovered missing backend notification modules
  from the immediately preceding AquaCulture release without altering their logic.
- Retained `scanner/fish_recommendation.py`, `sensors.py`, and
  `settings/admin_password.py` byte-for-byte from the user ZIP. Minimum 3 sensors
  and the current ammonia contribution are UNCHANGED.
- Fixed duplicate `title` and custom data-tooltip rendering; added a 4:3 mobile
  camera frame without changing the detection/captured-image orientation.
- Restored offline Socket.IO client under `client/static/vendor/` (license kept).
- Kept Bulma installed locally, with small overlay CSS files for current design.

## Database / media changes

- All 14 legacy species images are stored at `database/media/species/`.
- All 25 legacy prediction ZIPs are stored at `database/media/predictions/`.
- SQLite columns `species.image_path` and `prediction.data_path` contain
  relative paths (no historical media BLOBs retained); 14 thumbnails generated.
- File migration checks size/sha256 before dropping BLOB bytes, and does not
  repeat on later startups.
- Species are split into 20 legacy active PH-relevant finfish records, 145
  archived legacy rows and 14 further PH reference-only species. Every original
  species ID and rule remains recoverable; history counts are preserved.
- The original 165-entry `species_rules.xlsx` workbook is kept for legacy audit.
  Do not reseed the active catalog from that workbook or run the destructive
  `database/create_database.py --yes` command on a database with real records.
  Reference-only fish have no fabricated scoring ranges and do not join RBS.
- Added read-only archive/research tables, CSV exports and paged record tables.
  Downloads still require admin session and historical prediction ZIP links
  continue to work through the existing download API.

## Model locations — files not included

NCNN inference is preferred for the Raspberry Pi CPU:

    AquaCulture/models/FishInv_ncnn_model/model.ncnn.param
    AquaCulture/models/FishInv_ncnn_model/model.ncnn.bin
    AquaCulture/models/MegaFauna_ncnn_model/model.ncnn.param
    AquaCulture/models/MegaFauna_ncnn_model/model.ncnn.bin

Raw PyTorch weights (optional, laptop development):

    AquaCulture/models/model-2.pt
    AquaCulture/models/model-3.pt

Neither `models/` nor weights are packaged for this phase.

## Hardware

`driver/nano.ino` and `driver/wiring.txt` were brought back as a reference for
future hardware setup; the sketch has NOT been tested on real Arduino hardware.
MQ-137 gas serial field and legacy ammonia sensor alias need a future approved
hardware review before deployment. No existing sensor/RBS code was changed.

## Verification performed

- `python -m pytest server/tests -q`: 21 passed (no Flask dependencies needed).
- SQLite integrity check passed; original 14 images / 25 prediction ZIPs have
  identical SHA-256 hashes before and after file extraction.
- Original 165 species IDs accounted for; 4 subscribers, 25 predictions,
  1 water reading and 10 logs preserved; original scoring/sensor source untouched.
- Browser layout checks exercised dashboard + Settings on desktop/tablet and
  mobile; viewport overflow and mobile camera aspect checked; JS errors: none.

## Still pending

- Full Flask + Socket.IO production startup (not available in this build
  environment); real hardware and model tests need physical devices/weights.
- Verify every legacy fish tolerance range against peer-reviewed/agency data.
- Expand catalog toward 60–80 *verified* PH aquaculture fish (currently 20
  scored + 14 reference-only), not with invented water tolerance values.
- Align MQ-137 gas physics, Arduino JSON field and 3-of-5 RBS only after
  user consent; current behavior intentionally preserved.
- UniSMS configuration validation/credit sending requires live credentials
  and funds; no SMS was sent or simulated as delivered.
- BLE network provisioning and Pi headless deployment are future tasks.

## Windows start

    python -m pip install -r server/requirements.txt
    python server/app.py

Then open http://127.0.0.1:5000
