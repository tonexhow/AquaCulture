# AquaCulture — Visual scanning flow update

## What changed

- `server/scanner/size_detection.py`: distinguishes 3/5/10-second camera recording from downstream AI analysis; implements a background job with a stable job ID, discrete stages, per-frame progress, quality selection, best-frame ranking, cancellation and independently bounded AI processing. Empty scenes return a completed result; model failures return errors instead of pretending nothing was detected.
- `server/live_streaming.py`: capture callback reports true recording elapsed time and number of sampled frames without opening a second camera owner.
- `server/scanner/marine_ncnn_worker.py`: reports `ok=false` if no model could execute; the existing model formats and paths remain unchanged.
- `server/api_manager.py`: visual scans now use HTTP 202 + authenticated status polling, avoiding web proxy disconnects during slow inference. Existing cancel route remains.
- `client/templates/index.html`, `client/static/index/index.js` and `phase2c.css`: four-stage progress, recording timer, per-frame thumbnails and model results, green winner, animated discarded cards, cancel action and reload/reconnect recovery.
- `server/tests/test_visual_pipeline.py`: fake-camera tests requiring neither sensor wiring nor actual NCNN model files.

## Performance and semantics

- The selected **recording duration** measures how long the backend samples the camera's existing MJPEG frame cache; it is *not* an end-to-end scan timeout or an MP4 recorder.
- The number of shortlisted frames (up to five by default, up to ten optionally) is a **maximum**. Exact duplicates and unreadable frames are omitted.
- Running existing Pi 4B models may take 20–30+ seconds per frame with two NCNN models and a fresh Python worker. The display uses completed frames / total frames for AI-stage progress. It does not invent a smooth percentage while a model is still processing.
- Frame-selection quality alone cannot prove a frame contains fish. The **final** green winner is chosen after NCNN processing, preferring confirmed fish/aquatic detection, higher confidence, fish count, then visual quality.
- Rejected frames are discarded from the temporary shortlist only; this update does not delete user documents or overwrite previous prediction archives.
- An empty scene is marked "No aquatic object detected" only when at least one model successfully performed image analysis. When all models fail, the scan returns an error.

## Raspberry Pi deployment checklist

1. Keep your current `.env`, `models/`, `database/`, `driver/`, `network/` and virtual environment. **Merge only the changed source files.** The ZIP omits those folders on purpose.
2. In the actual project on Pi, verify `models/FishInv_ncnn_model` and `models/MegaFauna_ncnn_model` have valid `.bin` and `.param` exports, and `.venv/bin/python` imports `ultralytics` and `ncnn`.
3. Restart your usual AquaCulture service, then hard-refresh browser assets.
4. Start a **3-second / 5-frame** scan with an empty camera scene. The recording timer should end in ~3 s, AI progress continues afterward, and the final response should be **no detection** (not a never-ending spinner).
5. Run another scan with a real fish visible, at different distances. Confirm actual bounding boxes, per-frame counts, confident selected frame and measurement warnings.
6. Start a scan and press **Cancel** during inference. The child worker should exit and allow a new scan.
7. Test **5 seconds / 10 frames** only after the faster test succeeds. On Pi 4B this may take minutes.
8. Observe `GET /api/prediction/status` as an authenticated Admin: stages `recording` / `selecting` / `predicting` / `packaging` / `complete`, or `failed` / `cancelled`, with `progress`, `frames` and `last_result`.

## Automated checks without hardware or weights

```bash
cd ~/AquaCulture
.venv/bin/python -m unittest discover -s server/tests -p 'test_visual_pipeline.py' -v
.venv/bin/python -m compileall -q server/scanner server/api_manager.py server/live_streaming.py
```

## API examples (requires existing Admin authentication)

`POST /api/prediction/start` with JSON:

```json
{"duration_seconds": 3, "top": 5, "target_fps": 4}
```

Returns HTTP 202:

```json
{"ok": true, "accepted": true, "job_id": "...", "status": {"stage": "recording", "running": true}}
```

Then poll `GET /api/prediction/status` (~850 ms suggested) using the existing Admin cookie or token. Call `POST /api/prediction/cancel` to cancel. Status responses include small image previews; keep this endpoint authenticated and no-cache in production.

## Unchanged areas

- No modifications to the water RBS algorithm, Arduino pins, sensor driver or hardware communications.
- No pretrained weights or model binaries included or rewritten.
- No database schema migration is required for this scanning flow.

## Known limitations

- This development environment cannot validate your physical Pi camera, wiring, actual runtime speed, species accuracy or calibrated length measurements.
- Jobs reside in the Flask backend's process memory. A backend restart loses in-flight work; a multi-process deployment requires an external shared job queue or a single backend worker for visual scans.
- NCNN workers remain separate per analyzed frame for crash isolation; persistent runtime loading is a future optimization, not part of this update.
- With a very slow model or many frames, AI processing can hit its configurable limit and report a clear timeout. It must never silently label that failure a no-detection result.
