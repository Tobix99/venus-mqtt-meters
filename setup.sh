#!/bin/bash
# Install/uninstall venus-mqtt-meters on Venus OS; expects the repo at /data/venus-mqtt-meters.
set -e
DIR=/data/venus-mqtt-meters
NAME=venus-mqtt-meters
RCLOCAL=/data/rc.local
LINE="ln -sfn $DIR/service /service/$NAME"

case "$1" in
  install)
    [ -f "$DIR/config.ini" ] || { echo "Create $DIR/config.ini from config.sample.ini first"; exit 1; }
    chmod +x "$DIR/venus_mqtt_meters.py" "$DIR/service/run" "$DIR/service/log/run"
    [ -f "$RCLOCAL" ] || { echo '#!/bin/bash' > "$RCLOCAL"; chmod +x "$RCLOCAL"; }
    grep -qxF "$LINE" "$RCLOCAL" || echo "$LINE" >> "$RCLOCAL"
    $LINE
    echo "Installed; logs: tail -F /var/log/$NAME/current | tai64nlocal"
    ;;
  restart)
    svc -t /service/$NAME
    ;;
  uninstall)
    sed -i "\|^$LINE\$|d" "$RCLOCAL"
    if [ -e /service/$NAME ]; then
      svc -dx /service/$NAME /service/$NAME/log || true
      rm -f /service/$NAME
    fi
    echo "Uninstalled; D-Bus devices disappear, settings under /Settings/Devices/mqtt_* remain"
    ;;
  *)
    echo "Usage: $0 install|restart|uninstall"; exit 1
    ;;
esac
