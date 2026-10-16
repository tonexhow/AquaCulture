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
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - depends on device install
    cv2 = None  # type: ignore

# Picamera2 is usually installed by apt into /usr/lib/python3/dist-packages.
# A normal virtual environment does not always see that path, so expose it here
# before falling back to OpenCV. This is important for Raspberry Pi Camera Module 3.
_DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if _DIST_PACKAGES not in sys.path:
    sys.path.append(_DIST_PACKAGES)

try:
    from picamera2 import Picamera2  # type: ignore
except Exception:  # pragma: no cover - depends on Raspberry Pi install
    Picamera2 = None  # type: ignore


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
    relay_enabled: bool = bool(int(os.environ.get("AQUACULTURE_RELAY_ENABLED", "0") or "0"))
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
        )
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._running = True
                self._message = "Camera is already running."
                return self.get_status()
            self._stop_event.clear()
            self._running = True
            self._connected = False
            self._mode = "starting"
            self._message = "Camera is starting."
            self._thread = threading.Thread(target=self._capture_loop, name="aquaculture-camera-capture", daemon=True)
            self._thread.start()
        return self.get_status()

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            self._running = False
            self._connected = False
            self._message = "Camera is stopping."
        self.stop_relay()
        self._release_camera()
        return self.get_status()

    def restart(self) -> Dict[str, Any]:
        self.stop()
        time.sleep(0.3)
        return self.start()

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            age = None
            if self._latest_epoch:
                age = round(time.time() - self._latest_epoch, 2)
            return {
                "ok": self._running and self._latest_frame is not None,
                "running": self._running,
                "connected": self._connected,
                "mode": self._mode,
                "message": self._message,
                "last_error": self._last_error,
                "frame_number": self._frame_number,
                "last_frame_at": self._latest_at,
                "age_seconds": age,
                "fresh": bool(age is not None and age <= self.config.stale_after_seconds),
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
        with self._lock:
            return {
                "running": self._running,
                "connected": self._connected,
                "mode": self._mode,
                "frame_number": self._frame_number,
                "last_frame_at": self._latest_at,
                "message": self._message,
                "last_error": self._last_error,
            }

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
        if not self._running:
            self.start()
            time.sleep(0.4)
        with self._lock:
            if self._latest_frame:
                return bytes(self._latest_frame)
        if allow_fallback:
            return self._fallback_image_bytes()
        return None

    def snapshot_data_url(self) -> Dict[str, Any]:
        image_bytes = self.snapshot_jpeg(allow_fallback=True)
        if not image_bytes:
            return {"ok": False, "message": "No camera frame available.", "data_url": None, "status": self.get_status()}
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return {"ok": True, "message": "Snapshot returned.", "data_url": f"data:image/jpeg;base64,{encoded}", "status": self.get_status()}

    def mjpeg_frames(self) -> Generator[bytes, None, None]:
        self.start()
        while True:
            frame = self.snapshot_jpeg(allow_fallback=True)
            if not frame:
                time.sleep(0.2)
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\n\r\n" + frame + b"\r\n"
            time.sleep(max(0.03, 1.0 / max(1, self.config.fps)))

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
        deadline = time.time() + duration_seconds
        records: List[Dict[str, Any]] = []
        index = 0

        while time.time() < deadline:
            if cancel_checker and cancel_checker():
                break
            image_bytes = self.snapshot_jpeg(allow_fallback=False)
            if image_bytes:
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
                        "frame_number": self._frame_number,
                        "timestamp": _now_iso(),
                        "captured_epoch": time.time(),
                        "image_bytes": image_bytes,
                        "distance_cm": distance_cm,
                        "ultrasonic_status": ultrasonic_status,
                        "camera_mode": self._mode,
                    }
                )
                index += 1
            time.sleep(gap)
        return records

    # ------------------------------------------------------------------
    # Capture internals
    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        backend_started = False
        try:
            backend_started = self._start_picamera2() or self._start_opencv()
            if not backend_started:
                self._set_status(False, False, "fallback", "No camera backend available. Using fallback frame.")
                while not self._stop_event.is_set():
                    fallback = self._fallback_image_bytes()
                    if fallback:
                        self._save_frame(fallback)
                    time.sleep(1.0)
                return

            while not self._stop_event.is_set():
                frame_bytes = self._read_camera_frame()
                if frame_bytes:
                    self._save_frame(frame_bytes)
                else:
                    time.sleep(0.1)
        except Exception as error:
            self._set_status(False, False, "error", "Camera loop failed safely.", _safe_error(error))
        finally:
            self._release_camera()
            with self._lock:
                self._running = False
                self._connected = False
                if self._mode != "error":
                    self._mode = "stopped"
                    self._message = "Camera is stopped."

    def _start_picamera2(self) -> bool:
        if Picamera2 is None or not platform.system().lower().startswith("linux"):
            return False
        try:
            picam = Picamera2()
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
            time.sleep(0.8)

            # Do not claim success until at least one real frame is captured.
            test_frame = picam.capture_array("main")
            encoded = self._encode_image(test_frame, source="picamera2")
            if not encoded:
                raise RuntimeError("Picamera2 opened but did not return an encodable frame.")

            self._picam = picam
            self._camera_type = "Raspberry Pi Camera Module"
            self._set_status(True, True, "picamera2", "Raspberry Pi camera is running with Picamera2.")
            self._save_frame(encoded)
            return True
        except Exception as error:
            try:
                picam.stop()  # type: ignore[name-defined]
            except Exception:
                pass
            self._release_picamera2()
            self._set_status(True, False, "picamera2", "Picamera2 failed. Falling back to OpenCV.", _safe_error(error))
            return False

    def _start_opencv(self) -> bool:
        if cv2 is None:
            self._set_status(True, False, "opencv", "OpenCV is not installed.")
            return False

        indexes = self._discover_opencv_indexes()
        backend_pairs = self._opencv_backend_pairs()
        tried: List[str] = []

        if not indexes:
            self._set_status(True, False, "opencv", "No OpenCV camera indexes discovered.")
            return False

        self._set_status(True, False, "opencv", "Falling back to OpenCV. Searching for another camera...")

        for index in indexes:
            if self._stop_event.is_set():
                return False
            for label, backend in backend_pairs:
                if self._stop_event.is_set():
                    return False
                try:
                    cap = self._open_opencv_capture(index, backend)
                    if not cap or not cap.isOpened():
                        tried.append(f"{label}:{index}")
                        self._safe_release_cap(cap)
                        continue

                    encoded = self._configure_and_validate_opencv_capture(cap, index, label)
                    if not encoded:
                        tried.append(f"{label}:{index}:opened-but-no-valid-frame")
                        self._safe_release_cap(cap)
                        continue

                    self._cap = cap
                    self._picam = None
                    self._opencv_index = index
                    self._opencv_backend_label = label
                    self._opencv_read_failures = 0
                    self._camera_type = self._detect_opencv_camera_type(index)
                    camera_note = "USB camera detected." if self._camera_type == "USB camera" else f"{self._camera_type} detected."
                    self._set_status(
                        True,
                        True,
                        "opencv",
                        f"Using OpenCV backend {label} on camera index {index}. {camera_note}",
                    )
                    self._save_frame(encoded)
                    return True
                except Exception as error:
                    tried.append(f"{label}:{index}:{_safe_error(error)}")
                    try:
                        self._safe_release_cap(cap)  # type: ignore[possibly-undefined]
                    except Exception:
                        pass
        self._set_status(True, False, "opencv", f"No OpenCV camera opened. Tried {', '.join(tried[:12])}.")
        return False

    # ------------------------------------------------------------------
    # OpenCV discovery and validation helpers
    # ------------------------------------------------------------------
    def _discover_opencv_indexes(self) -> List[int]:
        indexes: List[int] = []

        def add_index(value: Any) -> None:
            try:
                number = int(value)
            except Exception:
                return
            if number < 0:
                return
            if number not in indexes:
                indexes.append(number)

        # Preferred configured index first, then a wider scan range for USB
        # webcams, laptop cameras, and camera-number changes after replugging.
        add_index(self.config.opencv_index)
        for index in range(0, 32):
            add_index(index)

        # Linux USB/UVC cameras usually appear as /dev/video*. Convert those
        # device names to integer OpenCV indexes and append new indexes only.
        if platform.system().lower().startswith("linux"):
            for device in sorted(Path("/dev").glob("video*"), key=lambda item: _video_device_sort_key(item.name)):
                name = device.name.replace("video", "", 1)
                if name.isdigit():
                    add_index(int(name))

        return indexes

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
            add_backend("GSTREAMER", "CAP_GSTREAMER")
            add_backend("FFMPEG", "CAP_FFMPEG")
            add_backend("ANY", "CAP_ANY")
            add_backend("default", None)
        else:
            add_backend("ANY", "CAP_ANY")
            add_backend("default", None)

        return pairs

    def _open_opencv_capture(self, index: int, backend: Optional[int]) -> Any:
        if cv2 is None:
            return None
        if backend is None:
            return cv2.VideoCapture(index)
        return cv2.VideoCapture(index, backend)

    def _configure_and_validate_opencv_capture(self, cap: Any, index: int, backend_label: str) -> Optional[bytes]:
        if cv2 is None or cap is None:
            return None
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            cap.set(cv2.CAP_PROP_FPS, self.config.fps)
        except Exception:
            pass

        # Many USB webcams need a short warmup after width/height/fps changes.
        time.sleep(1.0)

        for _attempt in range(12):
            try:
                ok, image = cap.read()
            except Exception:
                ok, image = False, None
            if not ok or image is None:
                time.sleep(0.1)
                continue
            if not self._valid_image_array(image):
                time.sleep(0.1)
                continue
            encoded = self._encode_image(image, source="opencv")
            if encoded:
                return encoded
            time.sleep(0.1)

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

    def _detect_opencv_camera_type(self, index: int) -> str:
        system_name = platform.system().lower()
        if system_name.startswith("linux"):
            if self._linux_video_device_exists(index):
                name = self._linux_video_device_name(index).lower()
                usb_terms = ["usb", "uvc", "logitech", "elp", "arducam", "webcam", "camera"]
                if any(term in name for term in usb_terms):
                    return "USB camera"
                # Most extra Linux /dev/video* entries are external or UVC
                # devices. Keep the message friendly even when v4l2 metadata is
                # unavailable.
                return "USB camera"
        if index == self.config.opencv_index:
            return "configured OpenCV camera"
        if index == 0:
            return "laptop webcam"
        return "OpenCV camera"

    def _linux_video_device_exists(self, index: int) -> bool:
        try:
            return Path(f"/dev/video{int(index)}").exists()
        except Exception:
            return False

    def _linux_video_device_name(self, index: int) -> str:
        device = f"/dev/video{int(index)}"
        try:
            result = subprocess.run(  # noqa: S603 - local diagnostic command with fixed executable
                ["v4l2-ctl", "--device", device, "--info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                check=False,
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    if "Card type" in line or "Driver name" in line:
                        return line.split(":", 1)[-1].strip()
        except Exception:
            pass
        return device

    def _safe_release_cap(self, cap: Any) -> None:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass

    def _reconnect_opencv(self, reason: str = "Camera read failed repeatedly.") -> bool:
        self._set_status(True, False, "opencv", f"{reason} Searching for another camera...")
        self._release_opencv()
        self._opencv_read_failures = 0
        connected = self._start_opencv()
        if connected:
            self._set_status(
                True,
                True,
                "opencv",
                f"Camera reconnected. Using OpenCV backend {self._opencv_backend_label or 'default'} on camera index {self._opencv_index}.",
            )
        return connected

    def _read_camera_frame(self) -> Optional[bytes]:
        try:
            if self._picam is not None:
                try:
                    image = self._picam.capture_array("main")
                    encoded = self._encode_image(image, source="picamera2")
                    if encoded:
                        return encoded
                    raise RuntimeError("Picamera2 returned a frame that could not be encoded.")
                except Exception as error:
                    self._set_status(True, False, "picamera2", "Picamera2 read failed. Falling back to OpenCV.", _safe_error(error))
                    self._release_picamera2()
                    self._start_opencv()
                    return None

            if self._cap is not None:
                ok, image = self._cap.read()
                if not ok or image is None or not self._valid_image_array(image):
                    self._opencv_read_failures += 1
                    if self._opencv_read_failures >= 8:
                        self._reconnect_opencv("Camera read failed repeatedly.")
                    return None
                encoded = self._encode_image(image, source="opencv")
                if not encoded:
                    self._opencv_read_failures += 1
                    if self._opencv_read_failures >= 8:
                        self._reconnect_opencv("Camera frame encoding failed repeatedly.")
                    return None
                self._opencv_read_failures = 0
                return encoded

            if self._running and not self._stop_event.is_set():
                self._reconnect_opencv("No active camera handle.")
        except Exception as error:
            self._set_status(True, False, self._mode, "Camera read failed.", _safe_error(error))
            try:
                self._reconnect_opencv("Camera failure recovered safely.")
            except Exception:
                pass
        return None

    def _encode_image(self, image: Any, source: str = "opencv") -> Optional[bytes]:
        if cv2 is None:
            return None
        try:
            if not self._valid_image_array(image):
                return None
            # Picamera2 returns RGB/RGBA when we request RGB888. OpenCV JPEG
            # encoding expects BGR/BGRA, so convert only Picamera2 frames.
            if source == "picamera2" and hasattr(image, "ndim") and image.ndim == 3:
                channels = image.shape[2] if len(image.shape) > 2 else 0
                if channels == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                elif channels == 4:
                    image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)])
            if not ok:
                return None
            return encoded.tobytes()
        except Exception:
            return None

    def _save_frame(self, frame: bytes) -> None:
        with self._lock:
            self._latest_frame = frame
            self._latest_epoch = time.time()
            self._latest_at = _now_iso()
            self._frame_number += 1
            self._connected = True
            if self._mode in {"starting", "error"}:
                self._mode = "camera"
            if self._mode == "opencv" and self._opencv_index is not None:
                self._message = (
                    f"Camera frame is available from OpenCV backend "
                    f"{self._opencv_backend_label or 'default'} on camera index {self._opencv_index}."
                )
            elif self._mode == "picamera2":
                self._message = "Camera frame is available from Picamera2."
            else:
                self._message = "Camera frame is available."
            self._last_error = None

    def _set_status(self, running: bool, connected: bool, mode: str, message: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._running = running
            self._connected = connected
            self._mode = mode
            self._message = message
            self._last_error = error

    def _release_camera(self) -> None:
        self._release_picamera2()
        self._release_opencv()

    def _release_picamera2(self) -> None:
        try:
            if self._picam is not None:
                self._picam.stop()
        except Exception:
            pass
        self._picam = None

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
        try:
            if DEFAULT_FALLBACK_IMAGE.exists():
                return DEFAULT_FALLBACK_IMAGE.read_bytes()
        except Exception:
            pass
        return _tiny_jpeg()


# ----------------------------------------------------------------------
# Helpers and module-level scanner/API wrappers
# ----------------------------------------------------------------------
def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _video_device_sort_key(name: str) -> int:
    try:
        return int(name.replace("video", "", 1))
    except Exception:
        return 9999


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
