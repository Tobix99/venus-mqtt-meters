# venus-mqtt-meters

A small Venus OS driver that exposes a **Shelly Pro 3EM** as a grid meter and
**OpenDTU / OpenDTU-OnBattery** inverters (Hoymiles) as PV inverters on D-Bus,
using data that is already published to an MQTT broker.

The devices then show up natively in the Venus OS GUI, the VRM portal and everything
else that reads D-Bus: the grid tile, PV inverter tile and calculated AC consumption.

```
                         ┌──────────────────────────── Venus OS ───────────────────────────┐
Shelly Pro 3EM ─┐        │                                                                 │
                ├─ MQTT ─┼─▶ venus_mqtt_meters.py ──D-Bus──▶ com.victronenergy.grid.*       │
OpenDTU ────────┘ broker │                                ├─▶ com.victronenergy.pvinverter.*│──▶ GUI / VRM
                         │                                                                 │
                         └─────────────────────────────────────────────────────────────────┘
```

## Why

Venus OS has a native Shelly integration and community drivers exist for OpenDTU, but both
poll the devices over HTTP/WebSocket. This driver only subscribes to MQTT topics that are
already there, so:

- the ESP32s in the Shelly and the DTU see no extra requests,
- a password-protected Shelly works (the native Venus integration cannot authenticate),
- the devices can live in a different network/VLAN than the Venus device, as long as the broker is reachable.

## Features

- Shelly Pro 3EM as `grid` meter: total and per-phase power, voltage, current, import/export energy.
- Any number of OpenDTU inverters as `pvinverter`: power, voltage, current, total yield, running/standby status.
- Pushes values to D-Bus once per second.
- Watchdog: a device that sends no data for `timeout` seconds is marked disconnected and its
  values are cleared, instead of silently freezing on the last value.
- Device instances are allocated through Venus localsettings and can be changed in the GUI.
- No extra dependencies: uses Python 3, paho-mqtt, dbus-python and velib_python that ship with Venus OS.
- Survives reboots and firmware updates (installed in `/data`, started via `/data/rc.local`).

## Requirements

- Venus OS v3.x (tested on v3.81, Raspberry Pi)
- Root SSH access to the Venus device
- An MQTT broker that receives:
  - the Shelly Pro 3EM status (Shelly → Settings → MQTT, with "Generic status update over MQTT" enabled)
  - OpenDTU or OpenDTU-OnBattery inverter data (OpenDTU → Settings → MQTT)

## MQTT topics

| Device  | Topic                                                  | Used for                                      |
|---------|--------------------------------------------------------|-----------------------------------------------|
| Shelly  | `<prefix>/status/em:0`                                 | total/per-phase power, voltage, current       |
| Shelly  | `<prefix>/status/emdata:0`                             | import/export energy totals (roughly every 60 s) |
| OpenDTU | `<dtu-prefix>/<serial>/0/power`                        | AC power                                      |
| OpenDTU | `<dtu-prefix>/<serial>/0/voltage`, `.../0/current`     | AC voltage and current                        |
| OpenDTU | `<dtu-prefix>/<serial>/0/yieldtotal`                   | total energy (kWh)                            |
| OpenDTU | `<dtu-prefix>/<serial>/status/reachable`, `.../producing` | running / standby status                   |

### Steady Shelly updates

The Shelly only publishes `status/em:0` when values change, which can leave gaps of 10 s and more.
A small Shelly script (Shelly web UI → Scripts) republishes the full EM status every second into
the same topic:

```js
// Publishes EM status every second into the Shelly's own status topic
let TOPIC = Shelly.getComponentConfig("mqtt").topic_prefix + "/status/em:0";
Timer.set(1000, true, function () {
  if (!MQTT.isConnected()) return;
  let em = Shelly.getComponentStatus("em", 0);
  if (!em || typeof em.total_act_power !== "number") return;
  MQTT.publish(TOPIC, JSON.stringify(em), 0, false);
});
```

This is optional for this driver. It is also useful for zero-export control (for example in
OpenDTU-OnBattery's dynamic power limiter).

## D-Bus services

| Service                                              | Role         | Default instance |
|------------------------------------------------------|--------------|------------------|
| `com.victronenergy.grid.mqtt_shelly_<mac>`           | grid meter   | 40               |
| `com.victronenergy.pvinverter.mqtt_opendtu_<serial>` | PV inverter  | 20, 21, ...      |

Published paths:

- Grid: `/Ac/Power`, `/Ac/Energy/{Forward,Reverse}`, `/Ac/L{1,2,3}/{Voltage,Current,Power}`,
  `/Ac/L{1,2,3}/Energy/{Forward,Reverse}`
- PV inverter: `/Ac/Power`, `/Ac/Energy/Forward`, `/Ac/L<n>/{Voltage,Current,Power,Energy/Forward}`,
  `/Ac/MaxPower`, `/Position` (0 = AC input), `/StatusCode` (7 = running, 8 = standby), `/ErrorCode`
- Both: the mandatory paths (`/Connected`, `/DeviceInstance`, `/ProductName`, ...) and `/CustomName`

## Configuration

Copy `config.sample.ini` to `config.ini` and adjust it.

| Section          | Key            | Description                                                            |
|------------------|----------------|------------------------------------------------------------------------|
| `general`        | `timeout`      | Seconds without data before a device is marked disconnected (default 30) |
| `mqtt`           | `host`, `port` | Broker address (default port 1883)                                     |
| `mqtt`           | `username`, `password` | Broker credentials, leave empty for anonymous access          |
| `mqtt`           | `client_id`    | MQTT client ID (default `venus-mqtt-meters`)                           |
| `shelly`         | `topic_prefix` | Shelly MQTT prefix, e.g. `shellypro3em-aabbccddeeff`. Omit the section to disable |
| `opendtu`        | `topic_prefix` | OpenDTU MQTT base topic (default `solar`)                              |
| `inverter:<serial>` | `name`      | Name shown in the GUI                                                  |
| `inverter:<serial>` | `phase`     | Grid phase the inverter is connected to: `L1`, `L2` or `L3` (default `L1`) |
| `inverter:<serial>` | `max_power` | Rated AC power in W (default 800)                                      |

Use one dedicated MQTT user for the driver; it only needs to subscribe.

### Finding the inverter phase

The phase only affects the per-phase breakdown of AC consumption; all totals are correct either way.
To find it, let the inverters produce a noticeable amount of power and watch the Shelly's
per-phase values: the phase whose power drops by roughly the inverter output is the right one.

```sh
mosquitto_sub -h <broker> -t '<shelly-prefix>/status/em:0' -t 'solar/+/0/power' -v
```

## Installation

From a machine with this repository checked out:

```sh
rsync -a --exclude .git ./ root@<venus-ip>:/data/venus-mqtt-meters/
ssh root@<venus-ip>
```

On the Venus device:

```sh
cd /data/venus-mqtt-meters
cp config.sample.ini config.ini
chmod 600 config.ini
vi config.ini
./setup.sh install
```

`setup.sh install` makes the scripts executable, adds one line to `/data/rc.local` that links
`service/` into `/service` on every boot, and starts the service. daemontools then supervises the
process and restarts it if it exits.

Check that it is running:

```sh
tail -F /var/log/venus-mqtt-meters/current | tai64nlocal
dbus -y | grep mqtt_
dbus -y com.victronenergy.system /Ac/Consumption/L1/Power GetValue
```

The new devices appear in the GUI under Settings → Devices (Device list), and the
overview shows grid, PV inverter and AC loads.

## Updating

```sh
rsync -a --exclude .git --exclude config.ini ./ root@<venus-ip>:/data/venus-mqtt-meters/
ssh root@<venus-ip> /data/venus-mqtt-meters/setup.sh restart
```

Run `./setup.sh restart` after any change to `config.ini` as well.

## Uninstalling

```sh
/data/venus-mqtt-meters/setup.sh uninstall
rm -rf /data/venus-mqtt-meters
```

This removes the `rc.local` line and stops the service. The instance settings under
`/Settings/Devices/mqtt_*` remain in localsettings and are reused if you install again.

## Caveat: battery-fed inverters (OpenDTU-OnBattery)

With OpenDTU-OnBattery, the Hoymiles inverters are usually fed from a battery that is charged by
Victron MPPTs. Venus OS has no device type for a battery-fed micro-inverter, so they are registered
as PV inverters. Live power flows and the calculated AC consumption are correct, but **solar yield
totals in the GUI and in VRM count that energy twice**: once when the MPPT harvests it and again
when the inverter feeds it into the house.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Service keeps restarting | `tail /var/log/venus-mqtt-meters/current`; usually a missing `config.ini` or a config error |
| `MQTT connect failed: Not authorized` | Username/password in `config.ini` and on the broker |
| Device shows as disconnected | No data on its topics for `timeout` seconds; check with `mosquitto_sub` from the Venus device |
| Grid energy values empty | `emdata:0` only arrives about once a minute; wait or check the Shelly's MQTT settings |
| AC consumption on one phase looks wrong | `phase` of the inverters, see [Finding the inverter phase](#finding-the-inverter-phase) |

## Project layout

```
venus_mqtt_meters.py   the driver
config.sample.ini      example configuration
setup.sh               install / restart / uninstall on Venus OS
service/run            daemontools run script
service/log/run        daemontools log script (multilog → /var/log/venus-mqtt-meters)
```

## License

[MIT](LICENSE)
