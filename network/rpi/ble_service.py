"""BlueZ GATT peripheral. Linux imports stay within this standalone component."""
import asyncio
import json
from collections import defaultdict, deque
from typing import Any, Callable, TypeVar

from dbus_next.constants import PropertyAccess
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method
from dbus_next.signature import Variant

from .authentication import encode
from .authentication import AuthorizationRequired
from .pairing import PairingWindow
from .protocol import BOOTSTRAP_UUID, COMMAND_UUID, EVENT_UUID, SERVICE_UUID, frames

BASE = "/com/aquaculture/provisioning"
SERVICE = BASE + "/service0"
GATT = "org.bluez.GattCharacteristic1"

_Function = TypeVar("_Function", bound=Callable[..., Any])


def _dbus_signature(result: str = "", **parameters: str) -> Callable[[_Function], _Function]:
    """Give dbus-next wire signatures without using them as Python types.

    Apply below @method/@dbus_property so dbus-next receives its required
    annotations, while editors can check the ordinary Python annotations.
    The original callable is retained, including its async/sync behavior.
    """
    def decorate(function: _Function) -> _Function:
        function.__annotations__ = {**parameters, "return": result}
        return function

    return decorate


class ManagedObjects(ServiceInterface):
    def __init__(self, objects):
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self.objects = objects

    @method()
    @_dbus_signature("a{oa{sa{sv}}}")
    def GetManagedObjects(self) -> dict[str, dict[str, dict[str, Variant]]]:
        return {path: {obj.name: obj.properties()} for path, obj in self.objects.items()}


class GattService(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("s")
    def UUID(self) -> str:
        return SERVICE_UUID

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("b")
    def Primary(self) -> bool:
        return True

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("ao")
    def Includes(self) -> list[str]:
        return []

    def properties(self):
        return {"UUID": Variant("s", self.UUID), "Primary": Variant("b", True), "Includes": Variant("ao", [])}


class Characteristic(ServiceInterface):
    def __init__(self, uuid, flags, controller):
        super().__init__(GATT)
        self.uuid, self.flags, self.controller = uuid, flags, controller
        self.value = b""
        self.notifying = False
        self.confirmed = asyncio.Event()
        self.bootstrap_values = {}
        self.read_event: Callable[[str], bytes] | None = None
        self.clear_peer: Callable[[str], object] | None = None

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("s")
    def UUID(self) -> str:
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("o")
    def Service(self) -> str:
        return SERVICE

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("as")
    def Flags(self) -> list[str]:
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("ay")
    def Value(self) -> bytes:
        return self.value

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("b")
    def Notifying(self) -> bool:
        return self.notifying

    def properties(self):
        return {"UUID": Variant("s", self.UUID), "Service": Variant("o", SERVICE), "Flags": Variant("as", self.flags)}

    @method()
    @_dbus_signature("ay", options="a{sv}")
    def ReadValue(self, options: dict[str, Variant]) -> bytes:
        if "device" not in options:
            raise DBusError("org.bluez.Error.NotPermitted", "Read the bootstrap characteristic.")
        peer = options["device"].value
        offset = options.get("offset", Variant("q", 0)).value
        if self.uuid == EVENT_UUID:
            if offset:
                return b""
            if self.read_event is None:
                raise DBusError("org.bluez.Error.Failed", "Event reader unavailable.")
            return self.read_event(peer)
        if self.uuid != BOOTSTRAP_UUID:
            raise DBusError("org.bluez.Error.NotPermitted", "Unsupported read.")
        try:
            if offset == 0:
                if self.clear_peer:
                    self.clear_peer(peer)
                self.bootstrap_values[peer] = encode(self.controller.auth.bootstrap(peer))
            value = self.bootstrap_values.get(peer, b"")
            if offset > len(value):
                raise ValueError()
            return value[offset:]
        except ValueError:
            raise DBusError("org.bluez.Error.NotAuthorized", "Setup challenge unavailable; wait and retry.") from None

    @method()
    @_dbus_signature(value="ay", options="a{sv}")
    def WriteValue(self, value: bytes, options: dict[str, Variant]) -> None:
        if self.uuid != COMMAND_UUID or "device" not in options or options.get("offset", Variant("q", 0)).value:
            raise DBusError("org.bluez.Error.NotPermitted", "Unsupported write.")
        try:
            self.controller.write(options["device"].value, bytes(value))
        except ValueError:
            raise DBusError("org.bluez.Error.Failed", "Invalid transfer or session; reconnect.") from None

    @method()
    @_dbus_signature()
    def StartNotify(self) -> None:
        if self.uuid != EVENT_UUID:
            raise DBusError("org.bluez.Error.NotSupported", "Subscribe to events.")
        self.notifying = True
        self.emit_properties_changed({"Notifying": True})

    @method()
    @_dbus_signature()
    def StopNotify(self) -> None:
        self.notifying = False
        self.confirmed.set()
        self.emit_properties_changed({"Notifying": False})

    @method()
    @_dbus_signature()
    def Confirm(self) -> None:
        self.confirmed.set()


class Advertisement(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("s")
    def Type(self) -> str:
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("as")
    def ServiceUUIDs(self) -> list[str]:
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    @_dbus_signature("s")
    def LocalName(self) -> str:
        return "AquaCulture-Pi"

    @method()
    @_dbus_signature()
    def Release(self) -> None:
        pass


class PairingAgent(ServiceInterface):
    """DisplayOnly passkey pairing. No automatic Just Works/numeric confirmation."""
    def __init__(self, window=None):
        super().__init__("org.bluez.Agent1")
        self.window = window or PairingWindow()

    @method()
    @_dbus_signature()
    def Release(self) -> None:
        self.window.clear()

    @method()
    @_dbus_signature()
    def Cancel(self) -> None:
        self.window.clear()

    @method()
    @_dbus_signature(device="o")
    def RequestAuthorization(self, device: str) -> None:
        raise DBusError("org.bluez.Error.Rejected", "Unauthenticated pairing is not permitted.")

    @method()
    @_dbus_signature(device="o", passkey="u")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        raise DBusError("org.bluez.Error.Rejected", "Use passkey entry from a trusted local console.")

    @method()
    @_dbus_signature(device="o", passkey="u", entered="q")
    def DisplayPasskey(self, device: str, passkey: int, entered: int) -> None:
        try:
            self.window.display(device, passkey)
        except ValueError:
            raise DBusError("org.bluez.Error.Rejected", "Physical pairing window is closed.") from None

    @method()
    @_dbus_signature(device="o", uuid="s")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        if uuid != SERVICE_UUID:
            raise DBusError("org.bluez.Error.Rejected", "Only provisioning is supported.")


class BleService:
    def __init__(self, bus, controller):
        self.bus, self.controller = bus, controller
        self.objects: dict[str, GattService | Characteristic] = {SERVICE: GattService()}
        self.bootstrap = Characteristic(BOOTSTRAP_UUID, ["read"], controller)
        self.command = Characteristic(COMMAND_UUID, ["encrypt-authenticated-write"], controller)
        self.event = Characteristic(EVENT_UUID, ["encrypt-authenticated-read"], controller)
        self.queues = defaultdict(deque)
        self.event.read_event = self.read_event
        self.bootstrap.clear_peer = lambda peer: self.queues.pop(peer, None)
        for index, char in enumerate((self.bootstrap, self.command, self.event)):
            self.objects[SERVICE + f"/char{index}"] = char
        self.lock = asyncio.Lock()
        self.message_id = 0
        self.registered = []

    async def interface(self, path, name):
        obj = self.bus.get_proxy_object("org.bluez", path, await self.bus.introspect("org.bluez", path))
        return obj.get_interface(name)

    async def start(self):
        manager = await self.interface("/", "org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        adapter = next((p for p, interfaces in objects.items() if "org.bluez.GattManager1" in interfaces and "org.bluez.LEAdvertisingManager1" in interfaces), None)
        if adapter is None or not objects[adapter].get("org.bluez.Adapter1", {}).get("Powered", Variant("b", False)).value:
            raise RuntimeError("Powered BLE peripheral adapter unavailable. Check rfkill and bluetoothctl power on.")
        self.bus.export(BASE, ManagedObjects(self.objects))
        for path, obj in self.objects.items():
            self.bus.export(path, obj)
        self.bus.export(BASE + "/advertisement", Advertisement())
        self.bus.export(BASE + "/agent", PairingAgent())
        agent_manager = await self.interface("/org/bluez", "org.bluez.AgentManager1")
        await agent_manager.call_register_agent(BASE + "/agent", "DisplayOnly")
        self.registered.append((agent_manager.call_unregister_agent, BASE + "/agent"))
        # First-time pairing requires a physical/local passkey, never automatic confirmation.
        await agent_manager.call_request_default_agent(BASE + "/agent")
        gatt = await self.interface(adapter, "org.bluez.GattManager1")
        advertising = await self.interface(adapter, "org.bluez.LEAdvertisingManager1")
        await gatt.call_register_application(BASE, {})
        self.registered.append((gatt.call_unregister_application, BASE))
        await advertising.call_register_advertisement(BASE + "/advertisement", {})
        self.registered.append((advertising.call_unregister_advertisement, BASE + "/advertisement"))

    async def send(self, peer, data, *, sensitive=False, revision=None):
        # Private responses are READ per BlueZ peer. Broadcasting GATT notifications
        # would expose them to another paired client before Admin verification.
        queue = self.queues[peer]
        self.message_id = (self.message_id + 1) % 65536
        packets = frames(data, self.message_id)
        if len(queue) + len(packets) > 6000:
            queue.clear()
            raise ValueError("Client response buffer is full; reconnect.")
        queue.extend((frame, revision if sensitive else None) for frame in packets)

    def read_event(self, peer):
        session = self.controller.auth.sessions.get(peer)
        if session and session.revision:
            try:
                self.controller.auth.get(peer)
            except AuthorizationRequired:
                self.queues[peer].clear()
                self.message_id = (self.message_id + 1) % 65536
                self.queues[peer].extend((f, None) for f in frames(encode({"type": "authorization_expired", "session": session.sid,
                    "message": "Admin authorization expired or changed. Verify again."}), self.message_id))
        queue = self.queues.get(peer)
        if not queue:
            return b""
        frame, revision = queue[0]
        if revision:
            try:
                if self.controller.auth.get(peer).revision != revision:
                    raise AuthorizationRequired()
            except AuthorizationRequired:
                queue.clear()
                self.message_id = (self.message_id + 1) % 65536
                queue.extend((f, None) for f in frames(encode({"type": "authorization_expired", "message": "Admin authorization expired or changed."}), self.message_id))
        return queue.popleft()[0]

    def disconnect(self, peer):
        self.controller.disconnect(peer)
        self.queues.pop(peer, None)
        self.bootstrap.bootstrap_values.pop(peer, None)

    async def close(self):
        await self.controller.close()
        for unregister, path in reversed(self.registered):
            try:
                await asyncio.wait_for(unregister(path), 3)
            except Exception:
                pass
        for path in [*self.objects, BASE, BASE + "/advertisement", BASE + "/agent"]:
            self.bus.unexport(path)
