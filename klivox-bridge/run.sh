#!/bin/sh
# Start der Klivox-Brücke als Home-Assistant-Add-on. Alle Daten liegen dauerhaft in /data.
export KLIVOX_BRIDGE_HOME=/data
export KLIVOX_CLOUD="${KLIVOX_CLOUD:-https://cloud.klivox.de}"
BR=/opt/klivox-bridge/klivox-bridge.py
CODE=$(jq -r '.kopplungscode // ""' /data/options.json 2>/dev/null | tr -d ' -' | tr 'a-z' 'A-Z')
LAST=$(cat /data/last_code 2>/dev/null)

if [ -n "$CODE" ] && [ "$CODE" != "$LAST" ]; then
  echo "Kopple mit Klivox (Code $CODE) …"
  if python3 "$BR" pair "$CODE"; then
    echo "$CODE" > /data/last_code
  else
    echo "Kopplung fehlgeschlagen. Bitte in Klivox unter Geräte › Klivox-Brücke einen neuen Code erzeugen und in der Konfiguration dieses Add-ons eintragen."
    sleep 3600; exit 1
  fi
fi

if ! python3 "$BR" status | grep -q '"gekoppelt": true'; then
  echo "Noch nicht gekoppelt. Bitte in Klivox unter Geräte › Klivox-Brücke einen Kopplungscode erzeugen,"
  echo "ihn hier unter Konfiguration › kopplungscode eintragen, speichern und das Add-on neu starten."
  sleep 3600; exit 1
fi

exec python3 "$BR" run
