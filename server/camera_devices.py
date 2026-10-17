"""Linux USB capture discovery. Device nodes alone are not camera evidence."""
from __future__ import annotations

import logging
import platform
import re
import struct
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("aquaculture.camera")
VIDEO_ROOT = Path("/dev")
BY_ID_ROOT = VIDEO_ROOT / "v4l/by-id"
BY_PATH_ROOT = VIDEO_ROOT / "v4l/by-path"
SYS_VIDEO_ROOT = Path("/sys/class/video4linux")
VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability[104 bytes])
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_VIDEO_M2M_MPLANE = 0x00004000
V4L2_CAP_VIDEO_M2M = 0x00008000
V4L2_CAP_READWRITE = 0x01000000
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000


@dataclass(frozen=True)
class UsbCameraDevice:
    path: str
    name: str
    driver: str
    bus_info: str


def query_v4l2_device(path: str | Path) -> UsbCameraDevice | None:
    """Query the node's own capabilities, excluding codecs/ISP/metadata nodes.

    Errors deliberately reach the caller so permission failures can be logged.
    fcntl is imported only on Linux; laptop imports require no V4L2 packages.
    """
    if platform.system().lower() != "linux":
        return None
    import fcntl

    node = Path(path)
    resolved = node.resolve(strict=True)
    if resolved.parent != VIDEO_ROOT or not re.fullmatch(r"video\d+", resolved.name):
        return None
    data = bytearray(104)
    with node.open("rb", buffering=0) as handle:
        fcntl.ioctl(handle.fileno(), VIDIOC_QUERYCAP, data, True)
    driver, card, bus, _version, capabilities, device_caps = struct.unpack_from("=16s32s32sIII", data)
    flags = device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities
    if not flags & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE):
        return None
    if flags & (V4L2_CAP_VIDEO_M2M | V4L2_CAP_VIDEO_M2M_MPLANE):
        return None
    if not flags & (V4L2_CAP_STREAMING | V4L2_CAP_READWRITE):
        return None
    decode = lambda value: value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    driver_name, card_name, bus_name = decode(driver), decode(card), decode(bus)
    try:
        sys_device = str((SYS_VIDEO_ROOT / resolved.name / "device").resolve())
    except OSError:
        sys_device = ""
    # A generic 'camera' card name does not prove USB: CSI/ISP nodes use it too.
    if not (bus_name.lower().startswith("usb") or driver_name == "uvcvideo"
            or any(part.startswith("usb") for part in Path(sys_device).parts)):
        return None
    return UsbCameraDevice(str(node), card_name or resolved.name, driver_name, bus_name)


def discover_usb_cameras(
    preferred_device: str = "", preferred_index: int | None = None,
    diagnostics: list[str] | None = None,
) -> list[UsbCameraDevice]:
    """Prefer stable symlinks and deduplicate every physical capture node.

    An explicit path/index is a preference; other working USB devices remain
    eligible if it is unplugged, unsupported, or unable to supply frames.
    """
    candidates: list[Path] = []
    if preferred_device:
        candidates.append(Path(preferred_device))
    stable = sorted(BY_ID_ROOT.glob("*")) + sorted(BY_PATH_ROOT.glob("*"))
    if preferred_index is not None:
        preferred = VIDEO_ROOT / f"video{int(preferred_index)}"
        alias = next((path for path in stable if path.resolve() == preferred.resolve()), preferred)
        candidates.append(alias)
    candidates.extend(stable)
    candidates.extend(sorted(VIDEO_ROOT.glob("video[0-9]*"), key=lambda path: int(path.name[5:]) if path.name[5:].isdigit() else 100000))
    devices: list[UsbCameraDevice] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            real_path = str(candidate.resolve(strict=True))
            if real_path in seen:
                continue
            seen.add(real_path)
            device = query_v4l2_device(candidate)
            if device is not None:
                devices.append(device)
        except (OSError, ValueError) as error:
            reason = f"{candidate}: {type(error).__name__}"
            if diagnostics is not None:
                diagnostics.append(reason)
            LOGGER.debug("USB probe skipped %s", reason)
    return devices
