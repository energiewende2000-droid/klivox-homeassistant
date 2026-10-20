#!/usr/bin/env python3
"""
Klivox-Brücke – verbindet Geräte im Heimnetz mit der Klivox Cloud.

* Baut nur ausgehende HTTPS-Verbindungen auf (keine Portfreigabe am Router nötig).
* Bosch Smart Home Controller (lokale API, Ports 8443/8444) – Kopplung per Taste am Controller.
* Holt Befehle aus der Cloud ab (Long-Poll) und meldet Zustandsänderungen sofort zurück.

Aufruf:
  klivox-bridge.py pair CODE        # einmalig: mit dem Code aus Klivox koppeln
  klivox-bridge.py run              # Dauerbetrieb (läuft als systemd-Dienst)
  klivox-bridge.py status           # Kurzinfo
"""
import base64, datetime, json, logging, os, socket, sys, tempfile, threading, time, uuid

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:  # pragma: no cover
    print("Bitte zuerst installieren: sudo apt install python3-requests python3-cryptography")
    sys.exit(1)

VERSION = "1.0.0"
CLOUD = os.environ.get("KLIVOX_CLOUD", "https://cloud.klivox.de")
HOME = os.environ.get("KLIVOX_BRIDGE_HOME", os.path.expanduser("~/.klivox-bridge"))
CONF = os.path.join(HOME, "config.json")
log = logging.getLogger("klivox-bridge")


# ---------------------------------------------------------------- Konfiguration
def load_conf():
    try:
        with open(CONF) as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(c):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


def info():
    return {"version": VERSION, "host": socket.gethostname(), "python": sys.version.split()[0],
            "bosch": bool(load_conf().get("bosch", {}).get("cert"))}


# ---------------------------------------------------------------- Cloud
class Cloud:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": "Bearer " + token, "User-Agent": "KlivoxBridge/" + VERSION})

    def call(self, action, timeout=40, **data):
        data["action"] = action
        r = self.s.post(CLOUD + "/api/bridge.php", json=data, timeout=timeout)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(j.get("error") or ("HTTP %s" % r.status_code))
        return j


def pair(code):
    r = requests.post(CLOUD + "/api/bridge.php", json={"action": "pair", "code": code, "info": info()}, timeout=30)
    j = r.json()
    if not j.get("ok"):
        print("Kopplung fehlgeschlagen:", j.get("error"))
        sys.exit(2)
    c = load_conf()
    c["token"] = j["token"]
    c["bridge_id"] = j.get("bridge_id")
    save_conf(c)
    print("Gekoppelt. Die Brücke erscheint jetzt in Klivox unter Geräte › Klivox-Brücke.")


# ---------------------------------------------------------------- Bosch Smart Home Controller (lokal)
STATE_TYPE = {"PowerSwitch": "powerSwitchState", "RoomClimateControl": "climateControlState",
              "ShutterControl": "shutterControlState", "MultiLevelSwitch": "multiLevelSwitchState",
              "BinarySwitch": "binarySwitchState"}


def make_cert(dirpath):
    """Selbst signiertes Client-Zertifikat für den Controller erzeugen."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oss_klivox_bridge")])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650)).sign(key, hashes.SHA256()))
    cp, kp = os.path.join(dirpath, "bosch-cert.pem"), os.path.join(dirpath, "bosch-key.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    os.chmod(kp, 0o600)
    return cp, kp, cert.public_bytes(serialization.Encoding.PEM).decode()


def discover_shc():
    """Controller im Netz suchen (mDNS, falls 'zeroconf' installiert ist)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return None
    found = []

    class L:
        def add_service(self, zc, t, n):
            if "bosch" in n.lower() or "shc" in n.lower():
                i = zc.get_service_info(t, n)
                if i and i.parsed_addresses():
                    found.append(i.parsed_addresses()[0])

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, "_http._tcp.local.", L())
    time.sleep(4)
    zc.close()
    return found[0] if found else None


class Bosch:
    def __init__(self, conf):
        self.ip = conf["ip"]
        self.cert = (conf["cert"], conf["key"])
        self.s = requests.Session()
        self.s.cert = self.cert
        self.s.verify = False
        self.s.headers.update({"api-version": "3.2", "Content-Type": "application/json"})
        self.base = "https://%s:8444/smarthome" % self.ip
        self.rcc_by_room = {}
        self.devices = {}

    @staticmethod
    def register(ip, password):
        if not ip:
            ip = discover_shc()
            if not ip:
                raise RuntimeError("Controller nicht gefunden – bitte die IP-Adresse des Controllers in Klivox angeben.")
        cp, kp, pem = make_cert(HOME)
        body = {"@type": "client", "id": "oss_klivox_" + uuid.uuid4().hex[:8], "name": "OSS Klivox Bridge",
                "primaryRole": "ROLE_RESTRICTED_CLIENT", "certificate": pem}
        r = requests.post("https://%s:8443/smarthome/clients" % ip, json=body, verify=False, timeout=20,
                          headers={"Systempassword": base64.b64encode(password.encode()).decode(), "api-version": "3.2"})
        if r.status_code == 401:
            raise RuntimeError("Systempasswort falsch.")
        if r.status_code >= 300:
            raise RuntimeError("Controller lehnt die Kopplung ab (HTTP %s). Wurde die Taste am Controller gedrückt, bis sie blinkt?" % r.status_code)
        return {"ip": ip, "cert": cp, "key": kp}

    def get(self, path):
        r = self.s.get(self.base + path, timeout=20)
        r.raise_for_status()
        return r.json()

    def snapshot(self):
        rooms = {r["id"]: r.get("name", "") for r in self.get("/rooms")}
        devs = self.get("/devices")
        svcs = self.get("/services")
        by_dev = {}
        for s in svcs:
            by_dev.setdefault(s.get("deviceId"), {})[s.get("id")] = s.get("state") or {}
        # Raumklima-Steuerung (Soll-Temperatur) liegt beim virtuellen Gerät „roomClimateControl_…“ des Raums
        self.rcc_by_room = {}
        for d in devs:
            if d.get("deviceModel") == "ROOM_CLIMATE_CONTROL" or str(d.get("id", "")).startswith("roomClimateControl"):
                self.rcc_by_room[d.get("roomId")] = d["id"]
        out = []
        for d in devs:
            did = d.get("id")
            if did in self.rcc_by_room.values() or d.get("deviceModel") in ("SHC", "VENTILATION_SERVICE", "PRESENCE_SIMULATION_SERVICE", "INTRUSION_DETECTION_SYSTEM"):
                continue
            services = dict(by_dev.get(did, {}))
            target = {}
            rcc = self.rcc_by_room.get(d.get("roomId"))
            if rcc and ("TemperatureLevel" in services or "ValveTappet" in services) and "RoomClimateControl" in by_dev.get(rcc, {}):
                services["RoomClimateControl"] = by_dev[rcc]["RoomClimateControl"]
                target["RoomClimateControl"] = rcc
            self.devices[did] = {"target": target, "roomId": d.get("roomId")}
            out.append({"id": did, "name": d.get("name") or did, "room": rooms.get(d.get("roomId"), ""), "type": d.get("deviceModel", ""),
                        "online": d.get("status", "AVAILABLE") == "AVAILABLE", "services": services})
        return out

    def command(self, device, service, prop, value):
        target = self.devices.get(device, {}).get("target", {}).get(service, device)
        body = {"@type": STATE_TYPE.get(service, service[0].lower() + service[1:] + "State"), prop: value}
        r = self.s.put("%s/devices/%s/services/%s/state" % (self.base, requests.utils.quote(target, safe=""), service), json=body, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError("HTTP %s %s" % (r.status_code, r.text[:200]))

    def long_poll(self, on_event, stop):
        url = "https://%s:8444/remote/json-rpc" % self.ip
        while not stop.is_set():
            try:
                r = self.s.post(url, json=[{"jsonrpc": "2.0", "method": "RE/subscribe", "params": ["com/bosch/sh/remote/*", None]}], timeout=20)
                poll_id = r.json()[0]["result"]
                while not stop.is_set():
                    r = self.s.post(url, json=[{"jsonrpc": "2.0", "method": "RE/longPoll", "params": [poll_id, 30]}], timeout=40)
                    res = r.json()[0]
                    if "error" in res:
                        break
                    if res.get("result"):
                        on_event(res["result"])
            except Exception as e:
                log.warning("Bosch-Ereignisse unterbrochen: %s", e)
                stop.wait(10)


# ---------------------------------------------------------------- Dauerbetrieb
def run():
    c = load_conf()
    if not c.get("token"):
        print("Noch nicht gekoppelt. Zuerst: klivox-bridge.py pair CODE")
        sys.exit(2)
    cloud = Cloud(c["token"])
    stop = threading.Event()
    state = {"bosch": None, "last_full": 0, "dirty": set(), "lock": threading.Lock()}

    def start_bosch():
        bc = load_conf().get("bosch")
        if not bc or not bc.get("cert"):
            return
        b = Bosch(bc)
        state["bosch"] = b
        push_full()

        def on_event(events):
            changed = {e.get("deviceId") for e in events if isinstance(e, dict) and e.get("deviceId")}
            if changed:
                with state["lock"]:
                    state["dirty"] |= changed
        threading.Thread(target=b.long_poll, args=(on_event, stop), daemon=True).start()

    def push_full():
        b = state["bosch"]
        if not b:
            return
        try:
            devs = b.snapshot()
            cloud.call("devices", provider="bosch", full=True, devices=devs)
            state["last_full"] = time.time()
            log.info("Bosch: %d Geräte übertragen", len(devs))
        except Exception as e:
            log.warning("Bosch-Abgleich fehlgeschlagen: %s", e)

    def pusher():
        # Änderungen sammeln und gebündelt melden (höchstens jede Sekunde)
        while not stop.is_set():
            stop.wait(1)
            with state["lock"]:
                dirty, state["dirty"] = state["dirty"], set()
            b = state["bosch"]
            if b and (dirty or time.time() - state["last_full"] > 300):
                try:
                    devs = b.snapshot()
                    if time.time() - state["last_full"] > 300:
                        cloud.call("devices", provider="bosch", full=True, devices=devs)
                        state["last_full"] = time.time()
                    else:
                        part = [d for d in devs if d["id"] in dirty or any(t in dirty for t in b.devices.get(d["id"], {}).get("target", {}).values())]
                        if part:
                            cloud.call("devices", provider="bosch", full=False, devices=part)
                except Exception as e:
                    log.warning("Zustände melden fehlgeschlagen: %s", e)

    def handle(job):
        kind, p = job["kind"], job.get("payload") or {}
        if kind == "bosch_command":
            b = state["bosch"]
            if not b:
                raise RuntimeError("Kein Bosch Controller gekoppelt.")
            b.command(p["device"], p["service"], p["property"], p["value"])
            with state["lock"]:
                state["dirty"].add(p["device"])
            return {}
        if kind == "bosch_pair":
            bc = Bosch.register((p.get("ip") or "").strip(), p.get("password") or "")
            conf = load_conf()
            conf["bosch"] = bc
            save_conf(conf)
            start_bosch()
            return {"ip": bc["ip"]}
        if kind == "sync":
            push_full()
            return {}
        if kind == "ping":
            return {"pong": True, "info": info()}
        raise RuntimeError("Unbekannter Auftrag: " + kind)

    start_bosch()
    threading.Thread(target=pusher, daemon=True).start()
    results, backoff = [], 2
    log.info("Klivox-Brücke %s läuft (Cloud %s)", VERSION, CLOUD)
    while True:
        try:
            j = cloud.call("poll", timeout=45, wait=20, info=info(), results=results)
            results, backoff = [], 2
            for job in j.get("jobs", []):
                try:
                    results.append({"id": job["id"], "ok": True, "result": handle(job)})
                except Exception as e:
                    log.warning("Auftrag %s fehlgeschlagen: %s", job.get("kind"), e)
                    results.append({"id": job["id"], "ok": False, "result": {"error": str(e)}})
            if results:  # Ergebnisse sofort melden
                cloud.call("poll", timeout=45, wait=0, results=results)
                results = []
        except Exception as e:
            log.warning("Verbindung zur Cloud: %s – neuer Versuch in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(60, backoff * 2)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "pair" and len(sys.argv) > 2:
        pair(sys.argv[2])
    elif cmd == "run":
        run()
    elif cmd == "status":
        c = load_conf()
        print(json.dumps({"gekoppelt": bool(c.get("token")), "bosch": c.get("bosch", {}).get("ip"), "version": VERSION}, indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
