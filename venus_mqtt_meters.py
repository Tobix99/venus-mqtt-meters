#!/usr/bin/env python3
"""Expose a Shelly Pro 3EM and OpenDTU inverters from MQTT as Venus OS D-Bus devices."""

import configparser
import json
import logging
import os
import sys
import threading
import time

import dbus
import paho.mqtt.client as mqtt
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

VELIB_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext", "velib_python"),
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
]
for p in VELIB_PATHS:
    if os.path.isdir(p):
        sys.path.insert(1, p)
        break
from settingsdevice import SettingsDevice  # noqa: E402
from vedbus import VeDbusService  # noqa: E402

VERSION = "0.2.0"
PRODUCT_ID_GENERIC = 0xFFFF
PHASES = ("L1", "L2", "L3")
STATUS_RUNNING, STATUS_STANDBY = 7, 8

log = logging.getLogger("venus-mqtt-meters")


def _kwh(wh):
    return None if wh is None else round(wh / 1000, 2)


class Device:
    """One D-Bus service fed by MQTT; values are staged by the MQTT thread and flushed in the GLib loop."""

    role = None
    default_instance = None
    ignore_retained = False

    def __init__(self, settings_bus, ident, name, timeout, connection):
        self.ident = ident
        self.name = name
        self.timeout = timeout
        self.last_seen = 0.0
        self._lock = threading.Lock()
        self._staged = {}

        settings = SettingsDevice(
            settings_bus,
            {"instance": [f"/Settings/Devices/mqtt_{ident}/ClassAndVrmInstance",
                          f"{self.role}:{self.default_instance}", 0, 0]},
            None,
        )
        instance = int(settings["instance"].split(":")[1])

        # Private bus connection per service, otherwise object paths collide
        self.service = VeDbusService(f"com.victronenergy.{self.role}.mqtt_{ident}",
                                     bus=dbus.SystemBus(private=True), register=False)
        self.service.add_mandatory_paths(
            os.path.basename(__file__), VERSION, connection, instance,
            PRODUCT_ID_GENERIC, name, VERSION, 0, 0)
        self.service.add_path("/CustomName", name)
        self.add_paths()
        self.service.register()
        log.info("Registered %s (instance %d)", self.service.name, instance)

    def add_paths(self):
        raise NotImplementedError

    def value_paths(self):
        return [p for p in self.service._dbusobjects
                if p.startswith(("/Ac/", "/Dc/")) or p in ("/StatusCode",)]

    def stage(self, values):
        with self._lock:
            self._staged.update(values)
            self.last_seen = time.monotonic()

    def flush(self):
        with self._lock:
            staged, self._staged = self._staged, {}
            fresh = time.monotonic() - self.last_seen < self.timeout

        with self.service as s:
            if not fresh:
                if self.service["/Connected"] == 1:
                    log.warning("%s: no data for %ds, marking disconnected", self.ident, self.timeout)
                    s["/Connected"] = 0
                    for p in self.value_paths():
                        s[p] = None
                return
            if self.service["/Connected"] != 1:
                log.info("%s: data received, connected", self.ident)
                s["/Connected"] = 1
            for path, value in staged.items():
                s[path] = value


class ShellyGrid(Device):
    role = "grid"
    default_instance = 40

    def __init__(self, settings_bus, prefix, timeout):
        self.prefix = prefix
        mac = prefix.rsplit("-", 1)[-1]
        super().__init__(settings_bus, f"shelly_{mac}", "Shelly Pro 3EM", timeout, f"MQTT {prefix}")

    def add_paths(self):
        for p in ("/Ac/Power", "/Ac/Energy/Forward", "/Ac/Energy/Reverse"):
            self.service.add_path(p, None)
        for ph in PHASES:
            for q in ("Voltage", "Current", "Power", "Energy/Forward", "Energy/Reverse"):
                self.service.add_path(f"/Ac/{ph}/{q}", None)

    def topics(self):
        return [f"{self.prefix}/status/em:0", f"{self.prefix}/status/emdata:0"]

    def on_message(self, topic, payload):
        d = json.loads(payload)
        if topic.endswith("/em:0"):
            v = {"/Ac/Power": d.get("total_act_power")}
            for ph, k in zip(PHASES, "abc"):
                v[f"/Ac/{ph}/Voltage"] = d.get(f"{k}_voltage")
                v[f"/Ac/{ph}/Current"] = d.get(f"{k}_current")
                v[f"/Ac/{ph}/Power"] = d.get(f"{k}_act_power")
            self.stage(v)
        else:
            v = {"/Ac/Energy/Forward": _kwh(d.get("total_act")),
                 "/Ac/Energy/Reverse": _kwh(d.get("total_act_ret"))}
            for ph, k in zip(PHASES, "abc"):
                v[f"/Ac/{ph}/Energy/Forward"] = _kwh(d.get(f"{k}_total_act_energy"))
                v[f"/Ac/{ph}/Energy/Reverse"] = _kwh(d.get(f"{k}_total_act_ret_energy"))
            # Energy-only updates must not keep a dead power feed alive
            with self._lock:
                self._staged.update(v)


class OpenDtuInverter(Device):
    role = "pvinverter"
    default_instance = 20

    def __init__(self, settings_bus, prefix, serial, name, phase, max_power, timeout):
        self.base = f"{prefix.rstrip('/')}/{serial}"
        self.phase = phase
        self.max_power = max_power
        self._reachable = 1
        self._producing = 0
        super().__init__(settings_bus, f"opendtu_{serial}", name, timeout, f"MQTT {self.base}")

    def add_paths(self):
        self.service.add_path("/Position", 0)  # AC input (grid side)
        self.service.add_path("/ErrorCode", 0)
        self.service.add_path("/StatusCode", None)
        self.service.add_path("/Ac/MaxPower", self.max_power)
        for p in ("/Ac/Power", "/Ac/Energy/Forward"):
            self.service.add_path(p, None)
        for ph in PHASES:
            for q in ("Voltage", "Current", "Power", "Energy/Forward"):
                self.service.add_path(f"/Ac/{ph}/{q}", None)

    def value_paths(self):
        return [p for p in super().value_paths() if p != "/Ac/MaxPower"]

    def topics(self):
        return [f"{self.base}/0/{k}" for k in ("power", "voltage", "current", "yieldtotal")] + \
               [f"{self.base}/status/{k}" for k in ("reachable", "producing")]

    def on_message(self, topic, payload):
        key = topic.rsplit("/", 1)[-1]
        value = float(payload)
        ph = f"/Ac/{self.phase}"
        if key == "power":
            self.stage({"/Ac/Power": value, f"{ph}/Power": value})
        elif key == "voltage":
            self.stage({f"{ph}/Voltage": value})
        elif key == "current":
            self.stage({f"{ph}/Current": value})
        elif key == "yieldtotal":
            self.stage({"/Ac/Energy/Forward": value, f"{ph}/Energy/Forward": value})
        elif key in ("reachable", "producing"):
            setattr(self, f"_{key}", int(value))
            status = STATUS_RUNNING if self._reachable and self._producing else STATUS_STANDBY
            self.stage({"/StatusCode": status})


class OpenDtuDcLoad(Device):
    """DC input of a battery-fed inverter as dcsystem, so DVCC adds it to the MPPT charge limit.

    Fail-safe: retained, stale or implausible readings clear the values, which DVCC treats as 0 A.
    """
    role = "dcsystem"
    default_instance = 30
    ignore_retained = True  # a retained value may be hours old

    def __init__(self, settings_bus, prefix, serial, name, max_power, factor, timeout):
        self.base = f"{prefix.rstrip('/')}/{serial}"
        self.factor = factor
        self.max_dc_power = max_power * 1.15  # DC input incl. conversion losses
        self._voltage = None
        super().__init__(settings_bus, f"opendtu_{serial}_dc", f"{name} DC input", timeout,
                         f"MQTT {self.base}")

    def add_paths(self):
        for p in ("/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power"):
            self.service.add_path(p, None)

    def topics(self):
        return [f"{self.base}/0/powerdc", f"{self.base}/1/voltage", f"{self.base}/status/reachable"]

    def on_message(self, topic, payload):
        key = topic.rsplit("/", 1)[-1]
        value = float(payload)
        if key == "voltage":
            self._voltage = value if value > 0 else None
            return
        if key == "reachable":
            if not int(value):
                self.expire()
            return
        # powerdc: positive = consumed from the DC bus
        if not 0 <= value <= self.max_dc_power:
            log.warning("%s: implausible DC power %.1f W, ignored", self.ident, value)
            return
        power = round(value * self.factor, 1)
        v = self._voltage
        self.stage({"/Dc/0/Power": power, "/Dc/0/Voltage": v,
                    "/Dc/0/Current": round(power / v, 2) if v else None})

    def expire(self):
        """Mark stale now instead of waiting for the timeout."""
        with self._lock:
            self._staged = {}
            self.last_seen = 0.0


def load_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit(f"Config file not found: {path}")
    return cfg


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
    timeout = cfg.getint("general", "timeout", fallback=30)

    DBusGMainLoop(set_as_default=True)
    settings_bus = dbus.SystemBus()

    devices = []
    if cfg.has_section("shelly"):
        devices.append(ShellyGrid(settings_bus, cfg["shelly"]["topic_prefix"], timeout))
    dtu_prefix = cfg.get("opendtu", "topic_prefix", fallback="solar")
    dc_load = cfg.getboolean("opendtu", "dc_load", fallback=False)
    dc_factor = cfg.getfloat("opendtu", "dc_load_factor", fallback=1.0)
    dc_timeout = cfg.getint("opendtu", "dc_load_timeout", fallback=15)
    if not 0 < dc_factor <= 1:
        sys.exit("[opendtu] dc_load_factor must be in (0, 1]")
    for section in cfg.sections():
        if section.startswith("inverter:"):
            s = cfg[section]
            phase = s.get("phase", "L1").upper()
            if phase not in PHASES:
                sys.exit(f"[{section}] phase must be one of {PHASES}")
            serial, name = section.split(":", 1)[1], s.get("name", "Hoymiles")
            max_power = s.getint("max_power", fallback=800)
            devices.append(OpenDtuInverter(
                settings_bus, dtu_prefix, serial, name, phase, max_power, timeout))
            if s.getboolean("dc_load", fallback=dc_load):
                devices.append(OpenDtuDcLoad(
                    settings_bus, dtu_prefix, serial, name, max_power, dc_factor, dc_timeout))
    if not devices:
        sys.exit("No devices configured")

    routes = {}
    for d in devices:
        for t in d.topics():
            routes.setdefault(t, []).append(d)

    def on_connect(client, _userdata, _flags, reason, _props):
        if reason.is_failure:
            log.error("MQTT connect failed: %s", reason)
            return
        log.info("MQTT connected, subscribing to %d topics", len(routes))
        client.subscribe([(t, 0) for t in routes])

    def on_message(_client, _userdata, msg):
        for device in routes.get(msg.topic, ()):
            if msg.retain and device.ignore_retained:
                continue
            try:
                device.on_message(msg.topic, msg.payload)
            except (ValueError, TypeError) as e:
                log.warning("Bad payload on %s: %s", msg.topic, e)

    def on_disconnect(_client, _userdata, _flags, reason, _props):
        log.warning("MQTT disconnected: %s", reason)
        for d in devices:
            if isinstance(d, OpenDtuDcLoad):
                d.expire()

    m = cfg["mqtt"]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=m.get("client_id", "venus-mqtt-meters"))
    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password"))
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(1, 30)
    client.connect_async(m["host"], m.getint("port", fallback=1883), keepalive=30)
    client.loop_start()

    def tick():
        for d in devices:
            d.flush()
        return True

    GLib.timeout_add(1000, tick)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
