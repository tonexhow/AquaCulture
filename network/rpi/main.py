"""Run directly with the service venv; never imported by Flask."""
import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parents[2])]


async def serve(state_dir):
    from dbus_next import Message, MessageType
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType
    from rpi.authentication import Authorization
    from rpi.ble_service import BleService
    from rpi.connection_history import ConnectionHistory
    from rpi.connection_monitor import ConnectionMonitor
    from rpi.controller import Controller
    from rpi.wifi_manager import NetworkManager

    from aquaculture_auth import CredentialStore, DurableLimiter
    auth = Authorization(CredentialStore(), DurableLimiter(Path(state_dir) / "auth_limits.sqlite3"))
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    manager = NetworkManager(bus, ConnectionHistory(Path(state_dir) / "history.sqlite3"))
    controller = Controller(auth, manager, None)
    ble = BleService(bus, controller)
    controller.send = ble.send
    stop = asyncio.Event()
    lost = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def on_message(message):
        if message.message_type != MessageType.SIGNAL:
            return
        if message.interface == "org.freedesktop.DBus.Properties" and len(message.body) >= 2:
            interface, changed = message.body[:2]
            if interface == "org.bluez.Device1" and "Connected" in changed and not changed["Connected"].value:
                ble.disconnect(message.path)
            if interface == "org.bluez.Adapter1" and "Powered" in changed and not changed["Powered"].value:
                lost.set()
            if interface.startswith("org.freedesktop.NetworkManager"):
                manager.changed.set()
        if message.interface == "org.freedesktop.DBus" and message.member == "NameOwnerChanged" and message.body[0] in {"org.bluez", "org.freedesktop.NetworkManager"} and not message.body[2]:
            lost.set()
        if message.interface == "org.freedesktop.DBus.ObjectManager" and message.member == "InterfacesRemoved" and "org.bluez.Adapter1" in message.body[1]:
            lost.set()

    bus.add_message_handler(on_message)
    for rule in ("type='signal',interface='org.freedesktop.DBus.Properties'", "type='signal',interface='org.freedesktop.DBus.ObjectManager'", "type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged'"):
        reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule]))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError("D-Bus signal subscription denied.")
    tasks = []
    try:
        from rpi.wifi_manager import NM, ROOT
        await manager.iface(ROOT, NM)  # Keep BLE available even when the Wi-Fi adapter is absent.
        await ble.start()
        logging.info("BLE provisioning advertising as AquaCulture-Pi (protocol 2, Admin verification required).")
        tasks = [asyncio.create_task(ConnectionMonitor(manager, controller.publish).run()), asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait()), asyncio.create_task(bus.wait_for_disconnect())]
        await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            raise RuntimeError("Bluetooth/NetworkManager disappeared; systemd will restart provisioning.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ble.close()
        bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description="AquaCulture standalone BLE provisioning")
    parser.add_argument("--state-dir", default="/var/lib/aquaculture-ble")
    parser.add_argument("--open-pairing-window", action="store_true")
    parser.add_argument("--reset-auth-limits", action="store_true", help="Physical administrator recovery only")
    args = parser.parse_args()
    if args.open_pairing_window:
        from rpi.pairing import PairingWindow
        PairingWindow.open_local()
        print("Pairing open for 120 seconds. Connect the phone, then privately read /run/aquaculture-ble/pairing-code on this console.")
        return
    if args.reset_auth_limits:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise PermissionError("Use sudo from a trusted local console.")
        from aquaculture_auth import DurableLimiter
        DurableLimiter(Path(args.state_dir) / "auth_limits.sqlite3").clear_local("ble_admin")
        print("Local BLE authentication limit cleared. Restart the BLE service to clear in-process delays.")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve(args.state_dir))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # DBus errors may contain connection settings; never print raw exceptions.
        logging.error("Provisioning stopped (%s). Check Bluetooth, local Admin store, NetworkManager and permissions.", type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
