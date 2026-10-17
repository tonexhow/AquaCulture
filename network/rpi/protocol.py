"""Bounded minimum-MTU framing shared by the BLE service and unit tests."""
import struct
import time

HEADER = struct.Struct("!BHHH")
MAX_MESSAGE = 32768
CHUNK_SIZE = 13
SERVICE_UUID = "7a1c0001-9e67-4a21-bde4-33c5eab07a01"
BOOTSTRAP_UUID = SERVICE_UUID.replace("0001-", "0002-")
COMMAND_UUID = SERVICE_UUID.replace("0001-", "0003-")
EVENT_UUID = SERVICE_UUID.replace("0001-", "0004-")


def frames(data, message_id):
    if not 0 < len(data) <= MAX_MESSAGE:
        raise ValueError("Message length out of bounds.")
    return [HEADER.pack(0xA1, message_id, index, len(data)) + data[offset:offset + CHUNK_SIZE]
            for index, offset in enumerate(range(0, len(data), CHUNK_SIZE))]


class Reassembler:
    def __init__(self):
        self.reset()

    def reset(self):
        self.message_id = None
        self.total = 0
        self.index = 0
        self.buffer = bytearray()
        self.updated = 0

    def feed(self, frame, now=None):
        now = time.monotonic() if now is None else now
        try:
            if not 7 < len(frame) <= 20:
                raise ValueError("Invalid frame length.")
            version, message_id, index, total = HEADER.unpack(frame[:7])
            if version != 0xA1 or not 0 < total <= MAX_MESSAGE:
                raise ValueError("Invalid frame header.")
            if self.message_id is not None and now - self.updated > 30:
                self.reset()
            if self.message_id is None:
                if index != 0:
                    raise ValueError("Incomplete transfer; restart the message.")
                self.message_id, self.total = message_id, total
            if (message_id, total, index) != (self.message_id, self.total, self.index):
                raise ValueError("Out-of-order or duplicate frame.")
            if len(frame) - 7 != min(CHUNK_SIZE, total - len(self.buffer)):
                raise ValueError("Invalid chunk size.")
            self.buffer.extend(frame[7:])
            self.index += 1
            self.updated = now
            if len(self.buffer) == total:
                result = bytes(self.buffer)
                self.reset()
                return result
            return None
        except Exception:
            self.reset()
            raise
