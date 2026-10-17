"""D-Bus change wakeups plus a nonaggressive 30-second fallback."""
import asyncio
import logging


class ConnectionMonitor:
    def __init__(self, manager, publish):
        self.manager, self.publish = manager, publish
        self.last = None

    async def run(self):
        while True:
            try:
                # Leave history ownership to the manual operation until it completes.
                if not self.manager.operation.locked():
                    current = await self.manager.status()
                    old = self.last
                    keys = ("state", "ssid", "ip_address", "internet_available")
                    if old is None or any(old.get(k) != current.get(k) for k in keys):
                        if old and old.get("connected") and (not current.get("connected") or old.get("ssid") != current.get("ssid")):
                            self.manager.history.disconnect(old["ssid"])
                        if current.get("connected") and (not old or not old.get("connected") or old.get("ssid") != current.get("ssid")):
                            recent = self.manager.history.recent(1)
                            if not recent or recent[0]["ssid"] != current["ssid"] or not recent[0]["connected_at"] or recent[0]["disconnected_at"]:
                                self.manager.history.add(current["ssid"], current["state"])
                        self.last = current
                        await self.publish(current)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("Network observation unavailable; retrying in 30 seconds.")
            try:
                await asyncio.wait_for(self.manager.changed.wait(), 30)
                self.manager.changed.clear()
                await asyncio.sleep(1)  # Coalesce bursts of property changes.
            except asyncio.TimeoutError:
                pass
