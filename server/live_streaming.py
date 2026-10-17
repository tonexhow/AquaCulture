"""
AquaCulture - live_streaming.py

Purpose:
- Owns the camera once, caches the latest JPEG frame, and lets all backend code
  read from that cache.
- Provides a safe MJPEG fallback endpoint for local testing.
- Exposes stream metadata for a proper relay setup such as MediaMTX WebRTC/LL-HLS.
- Gives scanner/size_detection.py capture_burst(...) compatibility.

Production direction:
- The browser should prefer the relay URLs from get_stream_info().
- Flask MJPEG is only a fallback. The camera hardware is still opened only once.
"""

from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

# Picamera2 is usually installed by apt into /usr/lib/python3/dist-packages.
# A normal virtual environment does not always see that path, so expose it here
# before falling back to OpenCV. This is important for Raspberry Pi Camera Module 3.
_DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if platform.system().lower() == "linux" and Path(_DIST_PACKAGES).is_dir() and _DIST_PACKAGES not in sys.path:
    sys.path.append(_DIST_PACKAGES)

# V4L2's native select is bounded even when a UVC device stops supplying frames.
# Set before importing OpenCV; an operator can retain a different explicit value.
os.environ.setdefault("OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT", "3")
try:
    import cv2  # type: ignore
    _CV2_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on device install
    cv2 = None  # type: ignore
    _CV2_IMPORT_ERROR = f"{type(error).__name__}: {error}"

try:
    from picamera2 import Picamera2  # type: ignore
    _PICAMERA_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on Raspberry Pi install
    Picamera2 = None  # type: ignore
    _PICAMERA_IMPORT_ERROR = f"{type(error).__name__}: {error}"

try:
    from .camera_devices import discover_usb_cameras
except ImportError:  # Direct execution of server/app.py.
    from camera_devices import discover_usb_cameras

LOGGER = logging.getLogger("aquaculture.camera")


CancelChecker = Callable[[], bool]

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FALLBACK_IMAGE = BASE_DIR / "client" / "static" / "images" / "fallback.png"


@dataclass
class CameraConfig:
    width: int = 1280
    height: int = 720
    fps: int = 15
    opencv_index: int = 0
    jpeg_quality: int = 82
    stale_after_seconds: float = 5.0
    source: str = "auto"
    usb_device: str = ""
    retry_seconds: float = 5.0
    frame_timeout_seconds: float = 4.0
    relay_enabled: bool = field(default_factory=lambda: os.environ.get("AQUACULTURE_RELAY_ENABLED", "0").lower() in {"1", "true", "yes"})
    relay_webrtc_url: str = os.environ.get("AQUACULTURE_STREAM_WEBRTC_URL", "").strip()
    relay_hls_url: str = os.environ.get("AQUACULTURE_STREAM_HLS_URL", "").strip()
    relay_rtmp_url: str = os.environ.get("AQUACULTURE_STREAM_RTMP_URL", "").strip()
    relay_rtsp_url: str = os.environ.get("AQUACULTURE_STREAM_RTSP_URL", "").strip()
    relay_publish_command: str = os.environ.get("AQUACULTURE_STREAM_PUBLISH_COMMAND", "").strip()


class CameraManager:
    """Single camera reader/cache for all API and scanner users."""

    def __init__(self, config: Optional[CameraConfig] = None) -> None:
        self.config = config or CameraConfig(
            width=_safe_int(os.environ.get("AQUACULTURE_CAMERA_WIDTH"), 1280),
            height=_safe_int(os.environ.get("AQUACULTURE_CAMERA_HEIGHT"), 720),
            fps=_safe_int(os.environ.get("AQUACULTURE_CAMERA_FPS"), 15),
            opencv_index=_safe_int(os.environ.get("AQUACULTURE_CAMERA_INDEX"), 0),
            source=os.environ.get("AQUACULTURE_CAMERA_SOURCE", "auto").strip().lower(),
            usb_device=os.environ.get("AQUACULTURE_CAMERA_USB_DEVICE", "").strip(),
            retry_seconds=_safe_float(os.environ.get("AQUACULTURE_CAMERA_RETRY_SECONDS"), 5.0),
            frame_timeout_seconds=_safe_float(os.environ.get("AQUACULTURE_CAMERA_FRAME_TIMEOUT"), 4.0),
        )
        self.config.retry_seconds = max(2.0, self.config.retry_seconds)
        self.config.frame_timeout_seconds = max(1.0, self.config.frame_timeout_seconds)
        self.config.fps = max(1, self.config.fps)
        self.config.source = self.config.source.lower()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._picam: Any = None
        self._cap: Any = None
        self._relay_process: Optional[subprocess.Popen[Any]] = None
        self._latest_frame: Optional[bytes] = None
        self._latest_epoch: Optional[float] = None
        self._latest_at: Optional[str] = None
        self._frame_number = 0
        self._running = False
        self._connected = False
        self._mode = "stopped"
        self._message = "Camera is stopped."
        self._last_error: Optional[str] = None
        self._opencv_index: Optional[int] = None
        self._opencv_backend_label: Optional[str] = None
        self._camera_type: Optional[str] = None
        self._opencv_read_failures = 0
        self._want_running = False
        self._source: Optional[str] = None
        self._device: Optional[str] = None
        self._next_retry = 0.0
        self._next_csi_probe = 0.0
        self._status_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self._frame_changed = threading.Condition(self._lock)
        self._fallback_bytes: Optional[bytes] = None
        self._manually_stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            self._manually_stopped = False
            self._want_running = True
            if self._thread and self._thread.is_alive():
                return self.get_status()
            self._start_worker_locked()
        return self.get_status()

    def _start_worker_locked(self) -> None:
        # Only called after the previous owner released its handles.
        self._stop_event = threading.Event()
        self._next_retry = 0.0
        self._running = True
        self._clear_frame()
        self._mode = "starting"
        self._message = "Camera is starting."
        self._thread = threading.Thread(target=self._capture_loop, name="aquaculture-camera-capture", daemon=True)
        self._thread.start()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._manually_stopped = True
            self._want_running = False
            self._stop_event.set()
            self._running = False
            self._connected = False
            self._latest_frame = None
            self._latest_epoch = None
            self._mode = "stopped"
            self._message = "Camera is stopping."
            self._frame_changed.notify_all()
        self.stop_relay()
        # Capture handles belong exclusively to the worker, including release.
        return self.get_status()

    def restart(self) -> Dict[str, Any]:
        with self._lock:
            self._manually_stopped = False
            self._want_running = True
            self._stop_event.set()
            self._connected = False
            self._latest_frame = None
            self._latest_epoch = None
            self._message = "Camera restart queued."
            if not self._thread or not self._thread.is_alive():
                self._start_worker_locked()
            self._frame_changed.notify_all()
        return self.get_status()

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                LOGGER.warning("Camera worker is still waiting for a native driver; no second owner will be opened.")

    def set_status_callback(self, callback: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._status_callback = callback

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            age = None
            if self._latest_epoch:
                age = round(time.time() - self._latest_epoch, 2)
            return {
                "ok": bool(self._running and self._connected and self._latest_frame and age is not None and age <= self.config.stale_after_seconds),
                "running": self._running,
                "connected": self._connected,
                "mode": self._mode,
                "message": self._message,
                "last_error": self._last_error,
                "frame_number": self._frame_number,
                "last_frame_at": self._latest_at,
                "age_seconds": age,
                "fresh": bool(self._connected and age is not None and age <= self.config.stale_after_seconds),
                "source": self._source if self._connected else None,
                "source_label": "CSI" if self._connected and self._source == "csi" else "USB Webcam" if self._connected and self._source == "usb" else "Unavailable",
                "device": self._device if self._connected else None,
                "camera_type": self._camera_type,
                "backend": self._opencv_backend_label if self._source == "usb" else "Picamera2" if self._source == "csi" else None,
                "configured_source": self.config.source,
                "retry_in_seconds": round(max(0.0, self._next_retry - time.monotonic()), 1) if self._running and not self._connected else 0,
                "width": self.config.width,
                "height": self.config.height,
                "fps": self.config.fps,
                "relay": self.get_stream_info(include_status=False),
            }

    # ------------------------------------------------------------------
    # Relay metadata/process helpers
    # ------------------------------------------------------------------
    def get_stream_info(self, include_status: bool = True) -> Dict[str, Any]:
        relay_running = self._relay_process is not None and self._relay_process.poll() is None
        payload = {
            "relay_enabled": self.config.relay_enabled,
            "relay_running": relay_running,
            "preferred_mode": "webrtc" if self.config.relay_webrtc_url else "mjpeg_fallback",
            "webrtc_url": self.config.relay_webrtc_url,
            "hls_url": self.config.relay_hls_url,
            "rtmp_url": self.config.relay_rtmp_url,
            "rtsp_url": self.config.relay_rtsp_url,
            "mjpeg_url": "/api/camera/stream",
            "mjpeg_fallback_url": "/api/camera/stream",
            "snapshot_url": "/api/camera/snapshot",
            "message": "Use WebRTC/LL-HLS relay for multiple viewers. MJPEG is fallback only.",
        }
        if include_status:
            payload["camera"] = self.get_status_without_relay()
        return payload

    def get_status_without_relay(self) -> Dict[str, Any]:
        status = self.get_status()
        status.pop("relay", None)
        return status

    def start_relay(self) -> Dict[str, Any]:
        if not self.config.relay_publish_command:
            return {
                "ok": False,
                "message": "No relay publish command configured. Set AQUACULTURE_STREAM_PUBLISH_COMMAND.",
                "stream": self.get_stream_info(),
            }
        if self._relay_process is not None and self._relay_process.poll() is None:
            return {"ok": True, "message": "Relay publisher is already running.", "stream": self.get_stream_info()}
        try:
            self._relay_process = subprocess.Popen(  # noqa: S603 - admin-configured local command
                self.config.relay_publish_command,
                shell=True,  # noqa: S602 - admin-configured command for Pi deployment
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "message": "Relay publisher started.", "stream": self.get_stream_info()}
        except Exception as error:
            return {"ok": False, "message": "Relay publisher failed to start.", "error": _safe_error(error), "stream": self.get_stream_info()}

    def stop_relay(self) -> Dict[str, Any]:
        process = self._relay_process
        self._relay_process = None
        if process is None:
            return {"ok": True, "message": "Relay publisher is not running.", "stream": self.get_stream_info()}
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    process.kill()
            return {"ok": True, "message": "Relay publisher stopped.", "stream": self.get_stream_info()}
        except Exception as error:
            return {"ok": False, "message": "Relay publisher stop failed.", "error": _safe_error(error), "stream": self.get_stream_info()}

    # ------------------------------------------------------------------
    # Frame output used by API and scanner
    # ------------------------------------------------------------------
    def snapshot_jpeg(self, allow_fallback: bool = True) -> Optional[bytes]:
        return self.snapshot_frame(allow_fallback=allow_fallback)["image_bytes"]

    def snapshot_frame(self, allow_fallback: bool = True) -> Dict[str, Any]:
        """Return image bytes and their provenance from the same cache read."""
        self._ensure_started()
        record = self._current_frame_record()
        if record:
            return {**record, "placeholder": False}
        return {"image_bytes": self._fallback_image_bytes() if allow_fallback else None,
                "placeholder": True, "camera_source": None}

    def _ensure_started(self) -> None:
        with self._lock:
            if not self._manually_stopped and not self._want_running:
                self.start()

    def _current_frame_record(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if (not self._connected or not self._latest_frame or self._latest_epoch is None
                    or time.time() - self._latest_epoch > self.config.stale_after_seconds):
                return None
            return {"image_bytes": bytes(self._latest_frame), "frame_number": self._frame_number,
                    "timestamp": self._latest_at, "captured_epoch": self._latest_epoch,
                    "camera_mode": self._mode, "camera_source": self._source}

    def snapshot_data_url(self) -> Dict[str, Any]:
        packet = self.snapshot_frame(allow_fallback=True)
        image_bytes = packet["image_bytes"]
        if not image_bytes:
            return {"ok": False, "message": "No camera frame available.", "data_url": None, "status": self.get_status()}
        encoded = base64.b64encode(image_bytes).decode("ascii")
        status = self.get_status()
        return {"ok": True, "message": "Camera unavailable; placeholder returned." if packet["placeholder"] else "Snapshot returned.",
                "placeholder": packet["placeholder"], "source": packet["camera_source"],
                "data_url": f"data:image/jpeg;base64,{encoded}", "status": status}

    def mjpeg_frames(self) -> Generator[bytes, None, None]:
        self._ensure_started()
        while True:
            frame = self.snapshot_jpeg(allow_fallback=True)
            if not frame:
                time.sleep(0.2)
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\n\r\n" + frame + b"\r\n"
            with self._frame_changed:
                self._frame_changed.wait(timeout=max(0.05, 1.0 / self.config.fps))

    def capture_burst(
        self,
        duration_seconds: float = 5.0,
        target_fps: float = 2.0,
        sensor_manager: Any = None,
        cancel_checker: Optional[CancelChecker] = None,
    ) -> List[Dict[str, Any]]:
        self.start()
        duration_seconds = max(1.0, float(duration_seconds or 5.0))
        target_fps = max(0.5, min(10.0, float(target_fps or 2.0)))
        gap = 1.0 / target_fps
        deadline = time.monotonic() + duration_seconds
        records: List[Dict[str, Any]] = []
        index = 0

        last_frame = -1
        while time.monotonic() < deadline and not self._stop_event.is_set():
            if cancel_checker and cancel_checker():
                break
            record = self._current_frame_record()
            if record and record["frame_number"] != last_frame:
                last_frame = record["frame_number"]
                distance_cm = None
                ultrasonic_status = "unavailable"
                if sensor_manager is not None:
                    try:
                        distance_result = sensor_manager.read_ultrasonic_distance()
                        distance_cm = distance_result.get("distance_cm")
                        ultrasonic_status = "available" if distance_cm is not None else "unavailable"
                    except Exception:
                        ultrasonic_status = "error"
                records.append(
                    {
                        "image_id": f"frame-{index + 1}",
                        **record,
                        "distance_cm": distance_cm,
                        "ultrasonic_status": ultrasonic_status,
                    }
                )
                index += 1
            self._stop_event.wait(min(gap, max(0.0, deadline - time.monotonic())))
        return records

    # ------------------------------------------------------------------
    # Capture internals
    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        stop_event = self._stop_event
        read_failures = 0
        try:
            while not stop_event.is_set():
                try:
                    if self._picam is None and self._cap is None:
                        remaining = self._next_retry - time.monotonic()
                        if remaining > 0:
                            stop_event.wait(remaining)
                            continue
                        if not self._select_camera():
                            self._next_retry = time.monotonic() + self.config.retry_seconds
                            self._set_status(True, False, "fallback", "Camera: Unavailable — Retrying", self._last_error)
                            continue
                        self._next_retry = 0.0
                        self._next_csi_probe = time.monotonic() + 60.0
                        read_failures = 0
                    # Detect a newly connected CSI camera without repeatedly opening it.
                    if self._source == "usb" and self.config.source == "auto" and time.monotonic() >= self._next_csi_probe:
                        self._next_csi_probe = time.monotonic() + 60.0
                        if self._csi_candidates():
                            self._release_camera()
                            self._clear_frame()
                            if not self._select_camera():
                                self._next_retry = time.monotonic() + self.config.retry_seconds
                                self._set_status(True, False, "fallback", "Camera: Unavailable — Retrying", self._last_error)
                                continue
                    frame_bytes = self._read_camera_frame()
                    if frame_bytes:
                        self._save_frame(frame_bytes)
                        read_failures = 0
                    else:
                        read_failures += 1
                        if read_failures >= 3:
                            raise RuntimeError("Selected camera stopped supplying valid frames.")
                    stop_event.wait(1.0 / self.config.fps)
                except Exception as error:
                    self._clear_frame()
                    self._release_camera()
                    self._next_retry = time.monotonic() + self.config.retry_seconds
                    self._set_status(True, False, "fallback", "Camera: Unavailable — Retrying", _safe_error(error))
        finally:
            self._release_camera()
            with self._lock:
                self._running = False
                self._connected = False
                self._thread = None
                self._mode = "stopped"
                self._message = "Camera is stopped."
                self._frame_changed.notify_all()
                if self._want_running:
                    self._start_worker_locked()

    def _select_camera(self) -> bool:
        source = self.config.source
        if source not in {"auto", "csi", "usb"}:
            self._last_error = "AQUACULTURE_CAMERA_SOURCE must be auto, csi, or usb."
            return False
        self._last_error = None
        if source in {"auto", "csi"} and self._start_picamera2():
            return True
        csi_error = self._last_error
        if source in {"auto", "usb"} and self._start_opencv():
            return True
        self._last_error = "; ".join(dict.fromkeys(error for error in (csi_error, self._last_error) if error)) or "No supported capture devices detected."
        return False

    def _csi_candidates(self) -> List[Tuple[int, Dict[str, Any]]]:
        if Picamera2 is None or platform.system().lower() != "linux":
            return []
        try:
            info = Picamera2.global_camera_info()
            return [(index, item) for index, item in enumerate(info)
                    if item.get("Id") and "usb" not in str(item["Id"]).lower()]
        except Exception as error:
            self._last_error = f"CSI enumeration failed: {_safe_error(error)}"
            return []

    def _start_picamera2(self) -> bool:
        candidates = self._csi_candidates()
        if not candidates:
            self._last_error = self._last_error or (f"Picamera2 is not available ({_PICAMERA_IMPORT_ERROR})." if Picamera2 is None else "No CSI camera reported by libcamera.")
        for camera_number, info in candidates:
          picam = None
          try:
            picam = Picamera2(camera_number)
            # Use a video configuration and an explicit RGB format for continuous
            # browser streaming. Camera Module 3 is a libcamera/Picamera2 device;
            # OpenCV index 0 is not reliable for it.
            config = picam.create_video_configuration(
                main={"size": (self.config.width, self.config.height), "format": "RGB888"},
                buffer_count=4,
            )
            picam.configure(config)
            try:
                picam.set_controls({"FrameRate": float(max(1, self.config.fps))})
            except Exception:
                pass
            picam.start()
            if self._stop_event.wait(0.3):
                raise RuntimeError("Camera initialization cancelled.")

            # Do not claim success until at least one real frame is captured.
            job = picam.capture_array("main", wait=False)
            test_frame = picam.wait(job, timeout=self.config.frame_timeout_seconds)
            encoded = self._encode_image(test_frame, source="picamera2")
            if not encoded:
                raise RuntimeError("Picamera2 opened but did not return an encodable frame.")

            self._picam = picam
            picam = None
            self._source = "csi"
            self._device = str(info["Id"])
            self._camera_type = str(info.get("Model") or "Raspberry Pi CSI camera")
            self._save_frame(encoded)
            self._set_status(True, True, "picamera2", "Camera: CSI — Active")
            return True
          except Exception as error:
            self._last_error = f"CSI initialization failed: {_safe_error(error)}"
            LOGGER.warning("%s", self._last_error)
          finally:
            if picam is not None:
                self._close_picamera(picam)
        return False

    def _start_opencv(self) -> bool:
        if cv2 is None:
            self._last_error = f"OpenCV is not installed or cannot be imported ({_CV2_IMPORT_ERROR})."
            return False
        diagnostics: List[str] = []
        linux = platform.system().lower() == "linux"
        if linux:
            devices = discover_usb_cameras(self.config.usb_device,
                self.config.opencv_index if "AQUACULTURE_CAMERA_INDEX" in os.environ else None,
                diagnostics=diagnostics)
            candidates = [(device.path, device.name) for device in devices]
        else:
            indexes = list(dict.fromkeys([self.config.opencv_index, 0, 1, 2, 3]))
            candidates = [(index, "Desktop webcam") for index in indexes]
            if self.config.usb_device:
                candidates.insert(0, (self.config.usb_device, "Configured webcam"))
        backend_pairs = self._opencv_backend_pairs()
        for device, name in candidates:
            if self._stop_event.is_set():
                return False
            for label, backend in backend_pairs:
                if self._stop_event.is_set():
                    return False
                cap = None
                try:
                    cap = self._open_opencv_capture(device, backend)
                    if not cap or not cap.isOpened():
                        diagnostics.append(f"{device}: could not open using {label}")
                        continue
                    encoded = self._configure_and_validate_opencv_capture(cap, device, label)
                    if not encoded:
                        diagnostics.append(f"{device}: opened but supplied no valid frame")
                        continue
                    if self._stop_event.is_set():
                        return False
                    self._cap = cap
                    cap = None
                    self._source = "usb"
                    self._device = str(device)
                    self._opencv_index = device if isinstance(device, int) else None
                    self._opencv_backend_label = label
                    self._camera_type = name
                    self._save_frame(encoded)
                    self._set_status(True, True, "opencv", "Camera: USB Webcam — Active")
                    return True
                except Exception as error:
                    diagnostics.append(f"{device}: {_safe_error(error)}")
                finally:
                    self._safe_release_cap(cap)
        self._last_error = "; ".join(diagnostics[:6]) or "No USB V4L2 capture devices detected."
        return False

    # ------------------------------------------------------------------
    # OpenCV discovery and validation helpers
    # ------------------------------------------------------------------
    def _opencv_backend_pairs(self) -> List[Tuple[str, Optional[int]]]:
        if cv2 is None:
            return []

        pairs: List[Tuple[str, Optional[int]]] = []
        system_name = platform.system().lower()

        def add_backend(label: str, constant_name: Optional[str]) -> None:
            if constant_name is None:
                pairs.append((label, None))
                return
            backend = getattr(cv2, constant_name, None)
            if backend is not None:
                pairs.append((label, backend))

        if system_name.startswith("win"):
            add_backend("MSMF", "CAP_MSMF")
            add_backend("DSHOW", "CAP_DSHOW")
            add_backend("ANY", "CAP_ANY")
            add_backend("default", None)
        elif system_name.startswith("linux"):
            add_backend("V4L2", "CAP_V4L2")
        else:
            add_backend("ANY", "CAP_ANY")
            add_backend("default", None)

        return pairs

    def _open_opencv_capture(self, index: Any, backend: Optional[int]) -> Any:
        if cv2 is None:
            return None
        if backend is None:
            return cv2.VideoCapture(index)
        return cv2.VideoCapture(index, backend)

    def _configure_and_validate_opencv_capture(self, cap: Any, index: Any, backend_label: str) -> Optional[bytes]:
        if cv2 is None or cap is None:
            return None
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            cap.set(cv2.CAP_PROP_FPS, self.config.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        # Many USB webcams need a short warmup after width/height/fps changes.
        deadline = time.monotonic() + self.config.frame_timeout_seconds
        for _attempt in range(8):
            if self._stop_event.is_set() or time.monotonic() >= deadline:
                break
            try:
                ok, image = cap.read()
            except Exception:
                ok, image = False, None
            if not ok or image is None:
                self._stop_event.wait(0.1)
                continue
            if not self._valid_image_array(image):
                self._stop_event.wait(0.1)
                continue
            encoded = self._encode_image(image, source="opencv")
            if encoded:
                return encoded
            self._stop_event.wait(0.1)

        return None

    def _valid_image_array(self, image: Any) -> bool:
        try:
            if image is None:
                return False
            if getattr(image, "size", 0) <= 0:
                return False
            shape = getattr(image, "shape", None)
            if not shape or len(shape) < 2:
                return False
            if int(shape[0]) <= 0 or int(shape[1]) <= 0:
                return False
            return True
        except Exception:
            return False

    def _safe_release_cap(self, cap: Any) -> None:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass

    def _read_camera_frame(self) -> Optional[bytes]:
        if self._picam is not None:
            job = self._picam.capture_array("main", wait=False)
            image = self._picam.wait(job, timeout=self.config.frame_timeout_seconds)
            return self._encode_image(image, source="picamera2")
        if self._cap is not None:
            ok, image = self._cap.read()
            return self._encode_image(image) if ok else None
        return None

    def _encode_image(self, image: Any, source: str = "opencv") -> Optional[bytes]:
        if cv2 is None:
            return None
        try:
            if not self._valid_image_array(image):
                return None
            # Picamera2's RGB888 array is [B,G,R] (manual section 4.2.2),
            # already the byte order OpenCV expects. Swapping it corrupts colors.
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)])
            if not ok:
                return None
            return encoded.tobytes()
        except Exception:
            return None

    def _save_frame(self, frame: bytes) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._latest_frame = frame
            self._latest_epoch = time.time()
            self._latest_at = _now_iso()
            self._frame_number += 1
            self._connected = True
            self._last_error = None
            self._frame_changed.notify_all()

    def _clear_frame(self) -> None:
        with self._lock:
            self._connected = False
            self._source = None
            self._device = None
            self._latest_frame = None
            self._latest_epoch = None
            self._frame_changed.notify_all()

    def _set_status(self, running: bool, connected: bool, mode: str, message: str, error: Optional[str] = None) -> None:
        with self._lock:
            changed = (self._connected, self._mode, self._message, self._last_error) != (connected, mode, message, error)
            self._running = running
            self._connected = connected
            self._mode = mode
            self._message = message
            self._last_error = error
            # A stop/restart request cannot be undone by a late native open.
            if self._stop_event.is_set():
                self._running = self._want_running
                self._connected = False
        if changed:
            if error:
                LOGGER.warning("%s: %s (retry %.1fs)", message, error, self.config.retry_seconds)
            else:
                LOGGER.info("%s%s", message, f" [{self._device}]" if connected and self._device else "")
            if self._status_callback:
                try:
                    self._status_callback(self.get_status())
                except Exception:
                    LOGGER.debug("Camera status listener failed", exc_info=False)

    def _release_camera(self) -> None:
        self._release_picamera2()
        self._release_opencv()

    def _release_picamera2(self) -> None:
        picam, self._picam = self._picam, None
        if picam is not None:
            self._close_picamera(picam)

    @staticmethod
    def _close_picamera(picam: Any) -> None:
        try:
            picam.stop()
        except Exception:
            pass
        finally:
            try:
                picam.close()
            except Exception:
                pass

    def _release_opencv(self) -> None:
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None
        self._opencv_index = None
        self._opencv_backend_label = None
        self._opencv_read_failures = 0

    def _fallback_image_bytes(self) -> Optional[bytes]:
        if self._fallback_bytes is not None:
            return self._fallback_bytes
        try:
            if cv2 is not None and DEFAULT_FALLBACK_IMAGE.exists():
                image = cv2.imread(str(DEFAULT_FALLBACK_IMAGE))
                encoded = self._encode_image(image)
                if encoded:
                    self._fallback_bytes = encoded
                    return encoded
        except Exception:
            pass
        self._fallback_bytes = _tiny_jpeg()
        return self._fallback_bytes


# ----------------------------------------------------------------------
# Helpers and module-level scanner/API wrappers
# ----------------------------------------------------------------------
def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        number = float(value)
        return number if 0 < number < float("inf") else default
    except (ValueError, TypeError):
        return default


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tiny_jpeg() -> bytes:
    # 1x1 JPEG fallback; enough to keep MJPEG/snapshot responses valid.
    return base64.b64decode(
        b"/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/ASP/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/ASP/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/Aqf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgADAAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z"
    )


camera_manager = CameraManager()


def start_camera() -> Dict[str, Any]:
    return camera_manager.start()


def stop_camera() -> Dict[str, Any]:
    return camera_manager.stop()


def shutdown_camera() -> None:
    camera_manager.shutdown()


def restart_camera() -> Dict[str, Any]:
    return camera_manager.restart()


def get_status() -> Dict[str, Any]:
    return camera_manager.get_status()


def get_stream_info() -> Dict[str, Any]:
    return camera_manager.get_stream_info()


def start_relay() -> Dict[str, Any]:
    return camera_manager.start_relay()


def stop_relay() -> Dict[str, Any]:
    return camera_manager.stop_relay()


def snapshot_jpeg(allow_fallback: bool = True) -> Optional[bytes]:
    return camera_manager.snapshot_jpeg(allow_fallback=allow_fallback)


def snapshot_frame(allow_fallback: bool = True) -> Dict[str, Any]:
    return camera_manager.snapshot_frame(allow_fallback=allow_fallback)


def snapshot_data_url() -> Dict[str, Any]:
    return camera_manager.snapshot_data_url()


def mjpeg_frames() -> Generator[bytes, None, None]:
    return camera_manager.mjpeg_frames()


def capture_burst(
    duration_seconds: float = 5.0,
    target_fps: float = 2.0,
    sensor_manager: Any = None,
    cancel_checker: Optional[CancelChecker] = None,
) -> List[Dict[str, Any]]:
    return camera_manager.capture_burst(
        duration_seconds=duration_seconds,
        target_fps=target_fps,
        sensor_manager=sensor_manager,
        cancel_checker=cancel_checker,
    )
