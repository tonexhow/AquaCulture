"""Future BLE Wi-Fi provisioning service boundary.

This phase deliberately does not start BLE advertisements or accept credentials.
A secure GATT service, pairing/authentication and rollback must be tested on a Pi.
Do not claim Bluetooth setup is operational until hardware integration.
"""


def provisioning_status():
    return {"enabled": False, "supported": False, "status": "not_implemented",
            "message": "Bluetooth Wi-Fi setup is planned for a later hardware phase."}
