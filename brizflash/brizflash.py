#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22"]
# ///
"""brizflash: put ESPHome on a BrizLabs string-light controller over the air, using the
update command its original firmware already has. No serial cable, no opening the case.

  uv run brizflash.py                        # flashes the ESPHome bootstrap image (downloaded)
  uv run brizflash.py --ssid MyHotspot       # skip the WiFi name question
  uv run brizflash.py firmware.ota.bin       # or your own ESP32-C2 app image

What happens: this computer acts as a throwaway MQTT broker and a tiny HTTP server.
Over Bluetooth the controller is told to join a WiFi network this computer is on, and to use
this computer as its broker. Any 2.4 GHz network works, a phone hotspot included; it is only used
for the two minutes of flashing. When the controller checks in we send the `ota` command; it
downloads the image over HTTP, checks the MD5, writes the spare flash slot and reboots into it.
The bootstrap image then opens its own setup hotspot, where you pick the lights' real WiFi.
Needs: python3 and uv (or pip install bleak). See README.md.
"""
import argparse, asyncio, getpass, hashlib, io, json, socket, socketserver, struct, sys
import tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ADV_SERVICE = "0000a101-0000-1000-8000-00805f9b34fb"  # controllers advertise the name "RY" and this service
WRITE_CHR = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHR = "00002b10-0000-1000-8000-00805f9b34fb"
APP_SLOT = 0x1E0000  # size of each app partition on the controller
MQTT_PORT, HTTP_PORT = 1883, 8080  # preferred; the next free port is used if taken
OTA_WAIT = 400  # seconds to wait for the download + flash
# ESPHome Web-style bootstrap for 26 MHz ESP32-C2 boards: captive portal + dashboard adoption.
BOOTSTRAP = "https://github.com/TFenby/esp32c2-26mhz/releases/latest/download/esphome-web-26mhz-esp32c2.ota.bin"
RESMSG = {"升级成功": "upgrade successful", "重试过于频繁": "retry too soon (wait 10 s)",
          "开始下载": "download started", "烧录中": "writing flash", "下载中": "downloading",
          "设置启动分区失败": "set boot partition failed", "OTA结束失败": "esp_ota_end failed",
          "OTA失败": "OTA failed", "下载总时间超时": "download timeout (5 min)",
          "下载进度超时": "download stalled", "重连后获取内容长度失败": "reconnect failed"}


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def die(msg):
    sys.exit("\nERROR: " + msg)


# ---------- firmware image ----------
def fetch_bootstrap() -> Path:
    import urllib.request
    say("downloading the ESPHome bootstrap image")
    try:
        data = urllib.request.urlopen(BOOTSTRAP, timeout=60).read()
        want = urllib.request.urlopen(BOOTSTRAP + ".sha256", timeout=60).read().split()[0].decode()
    except OSError as e:
        die(f"download failed ({e}). Download {BOOTSTRAP} by hand and pass the file.")
    if hashlib.sha256(data).hexdigest() != want:
        die("downloaded image does not match its published checksum; try again")
    path = Path(tempfile.mkdtemp(prefix="brizflash-")) / BOOTSTRAP.rsplit("/", 1)[1]
    path.write_bytes(data)
    return path


def load_image(path: Path) -> tuple[bytes, str, str]:
    data = path.read_bytes()
    # ESP image header: magic 0xE9, chip id (LE16) at 12; esp_app_desc_t at 0x20 with magic 0xABCD5432,
    # version[32] at 0x30, project_name[32] at 0x50. A merged/factory image starts with the
    # bootloader, which has no app descriptor, so this check also rejects firmware.factory.bin.
    if len(data) < 0x100 or data[0] != 0xE9:
        die(f"{path.name} is not an ESP32 image (bad magic)")
    chip = struct.unpack_from("<H", data, 12)[0]
    if chip != 12:
        die(f"{path.name} is built for chip id {chip}, the BrizLabs controller is an ESP32-C2 (id 12)")
    if struct.unpack_from("<I", data, 0x20)[0] != 0xABCD5432:
        die(f"{path.name} has no app descriptor: pass the app-only image (firmware.ota.bin), "
            "not firmware.factory.bin")
    if len(data) > APP_SLOT:
        die(f"{path.name} is {len(data)} bytes, the OTA slot holds {APP_SLOT}")
    cstr = lambda off: data[off:off + 32].split(b"\0")[0].decode(errors="replace")
    version, project = cstr(0x30), cstr(0x50)
    say(f"image OK: {project} {version}, {len(data)} bytes, md5 {hashlib.md5(data).hexdigest()}")
    return data, version or "0.0.0", project


# ---------- servers on this computer ----------
def lan_ip() -> str:
    """The address the controller should use to reach us: the default-route address."""
    import ipaddress
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # no packet is sent; picks the interface with the default route
            ip = s.getsockname()[0]
        except OSError:
            die("this computer has no network address. Connect it to the WiFi you will give the lights, "
                "or pass --ip.")
    if not ipaddress.ip_address(ip).is_private:  # CGNAT/Tailscale 100.64/10 is already not "private"
        say(f"WARNING: {ip} does not look like a home-network address (VPN active?). If the lights never "
            "connect, turn the VPN off or pass --ip with this computer's WiFi address.")
    return ip


def free_port(prefer: int) -> int:
    for p in (prefer, prefer + 1, prefer + 2, 0):
        with socket.socket() as s:
            try:
                s.bind(("", p))
                return s.getsockname()[1]
            except OSError:
                pass  # port 0 (any free port) always binds


def unvarint(f) -> int:
    """MQTT variable-length integer from a file-like object."""
    n = shift = 0
    while True:
        b = f.read(1)[0]
        n |= (b & 127) << shift
        shift += 7
        if b < 128:
            return n


class MqttConn(socketserver.StreamRequestHandler):
    """One client of our broker: just enough MQTT for one controller. Any login is accepted,
    everything is acknowledged, and what it publishes lands in server.inbox as (topic, json, this connection)."""

    def send(self, kind: int, body: bytes = b""):
        n, size = len(body), b""
        while True:
            size += bytes([n % 128 | (0x80 if n > 127 else 0)])
            n //= 128
            if not n:
                break
        with self.server.lock:  # acks come from this thread, publish() from the main one
            self.wfile.write(bytes([kind]) + size + body)

    def publish(self, topic: str, payload: bytes):
        # ponytail: sent at QoS 0 straight to this client, whatever it subscribed to; fine for one
        # command on a LAN. Track subscriptions + QoS 1 retries if a command ever goes missing.
        t = topic.encode()
        self.send(0x30, struct.pack(">H", len(t)) + t + self.props + payload)

    def handle(self):
        self.props = b""  # MQTT 5 adds a properties field (ours is empty) to most packets
        try:
            while head := self.rfile.read(1):
                kind, qos = head[0] >> 4, head[0] >> 1 & 3
                b = io.BytesIO(self.rfile.read(unvarint(self.rfile)))
                if kind == 1:  # CONNECT: protocol name, then level (3 = 3.1, 4 = 3.1.1, 5 = 5.0)
                    level = b.read(struct.unpack(">H", b.read(2))[0] + 1)[-1]
                    self.props = b"\0" if level >= 5 else b""
                    self.send(0x20, b"\0\0" + self.props)
                    say(f"a device connected to our broker from {self.client_address[0]} (MQTT level {level})")
                elif kind == 3:  # PUBLISH
                    topic = b.read(struct.unpack(">H", b.read(2))[0]).decode(errors="replace")
                    pid = b.read(2) if qos else b""
                    if self.props:
                        b.read(unvarint(b))
                    if qos:
                        self.send(0x40 if qos == 1 else 0x50, pid)  # PUBACK / PUBREC
                    try:
                        self.server.inbox.append((topic, json.loads(b.read()), self))
                    except ValueError:
                        pass
                elif kind == 6:  # PUBREL -> PUBCOMP
                    self.send(0x70, b.read(2))
                elif kind == 8:  # SUBSCRIBE -> SUBACK granting QoS 0 for each topic
                    pid, count = b.read(2), 0
                    if self.props:
                        b.read(unvarint(b))
                    while t := b.read(2):
                        b.read(struct.unpack(">H", t)[0] + 1)
                        count += 1
                    self.send(0x90, pid + self.props + bytes(count))
                elif kind == 12:  # PINGREQ
                    self.send(0xD0)
                elif kind == 14:  # DISCONNECT
                    return
        except (OSError, IndexError, struct.error):
            pass  # dropped or garbled connection; the controller reconnects by itself


def start_broker(port: int) -> socketserver.ThreadingTCPServer:
    socketserver.ThreadingTCPServer.allow_reuse_address = socketserver.ThreadingTCPServer.daemon_threads = True
    srv = socketserver.ThreadingTCPServer(("", port), MqttConn)
    srv.inbox, srv.lock = [], threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_http(data: bytes, port: int, on_get) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # any path: the controller appends ?version=... to the URL we give it
            on_get()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        log_message = lambda *a: None  # noqa: E731
    # ponytail: no Range support; the controller only asks for one after a stalled download, rare on a LAN.
    srv = ThreadingHTTPServer(("", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------- Bluetooth: tell the controller to join our WiFi + broker ----------
def ble_frames(payload: bytes, chunk=112):
    """Frame format: FF | type=1 | index(BE16) | total(BE16) | total_len(BE16) | len(8) | data | sum8."""
    parts = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
    for i, part in enumerate(parts):
        body = struct.pack(">BBHHHB", 0xFF, 1, i, len(parts), len(payload), len(part)) + part
        yield body + bytes([sum(body[1:]) & 0xFF])


async def ble_send(client, obj: dict):
    for frame in ble_frames(json.dumps(obj).encode()):
        await client.write_gatt_char(WRITE_CHR, frame, response=False)
        await asyncio.sleep(0.15)


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip().lower()  # keeps the BLE link serviced


async def ble_join(ssid, password, broker_ip, broker_port, mac, yes=False):
    from bleak import BleakClient, BleakScanner
    say("scanning for the controller over Bluetooth (10 s)...")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    cands = sorted(((d, adv) for d, adv in found.values()
                    if (mac and d.address.upper() == mac.upper())
                    or (not mac and (ADV_SERVICE in adv.service_uuids or (adv.local_name or "").startswith("RY")))),
                   key=lambda p: -(p[1].rssi or -999))  # strongest signal first
    if not cands:
        die("no BrizLabs controller found over Bluetooth.\n"
            "  - Close the BrizLabs phone app (or put the phone in airplane mode): the controller only\n"
            "    talks to one device at a time.\n"
            "  - If the lights were ever set up in the app, Bluetooth is off. Unplug the controller and\n"
            "    plug it in three times, a few seconds apart (3 power-ups within 15 s): it returns to\n"
            "    setup mode with Bluetooth on.\n"
            "  - Still nothing? Run with --mac AA:BB:CC:DD:EE:FF (address from a BLE scanner app).")
    if len(cands) > 1:
        if yes:
            die("several controllers found; pick one with --mac:\n  " +
                "\n  ".join(f"{d.address}  signal {adv.rssi} dBm" for d, adv in cands))
        print("Several controllers are in range (1 = strongest signal, probably the nearest):")
        for i, (d, adv) in enumerate(cands, 1):
            print(f"  {i}. {d.address}  signal {adv.rssi} dBm")
        pick = await ask("Number of the one to flash [1]: ")
        if pick and not (pick.isdigit() and 1 <= int(pick) <= len(cands)):
            die("not a number from the list")
        dev = cands[int(pick or 1) - 1][0]
    else:
        dev = cands[0][0]
    say(f"found {dev.name} at {dev.address}, connecting")

    ok = asyncio.Event()

    def on_notify(_, d):
        text = d.decode("utf8", "replace")
        if "set wifi success" in text:
            ok.set()
        elif "error" in text.lower():
            say("controller replied: " + text)

    async with BleakClient(dev, timeout=20) as c:
        await c.start_notify(NOTIFY_CHR, on_notify)
        if not yes:
            # Physical confirmation: toggle the lights until the user sees the right string react.
            # The controller takes on/off commands over BLE, so this needs nothing else.
            print("Connected. Before anything is changed, check it is the right string of lights.")
            lit = False
            while True:
                answer = await ask("Enter = toggle the lights   y = these are the lights, flash them   n = stop: ")
                if answer == "y":
                    break
                if answer == "n":
                    die("stopped before changing anything. Unplug the other controllers or use --mac.")
                lit = not lit
                await ble_send(c, {"type": "thing.property.set", "ts": int(time.time()), "data": {"switch_led": lit}})
                say("lights " + ("on" if lit else "off"))
        say(f"sending WiFi '{ssid}' and this computer as broker")
        await ble_send(c, {"type": "thing.network.set", "ts": int(time.time()), "data": {
            "sid": ssid, "pw": password, "mq": broker_ip, "port": str(broker_port),
            "bid": "local", "userId": "local"}})  # all six fields are required, as strings
        try:
            await asyncio.wait_for(ok.wait(), 10)
            say("controller accepted the WiFi settings and is rebooting to join")
        except asyncio.TimeoutError:
            say("no confirmation over Bluetooth; waiting to see if it joins anyway")
    return dev.address


# ---------- MQTT: wait for the controller, send the ota command, watch progress ----------
def flash(data, version, ip, mqtt_port, http_port, inbox, args):
    conn = None

    def wait_report(seconds):
        nonlocal conn
        end = time.time() + seconds
        while time.time() < end:
            for topic, j, conn in inbox:
                if topic.endswith("/report"):
                    return topic.rsplit("/", 1)[0]
            time.sleep(0.5)

    # Already on our broker from an earlier attempt (it keeps reconnecting)? Then skip Bluetooth.
    # Checked again after the WiFi questions, which give a retrying controller time to show up.
    say("waiting 5 s to see if a controller is already connecting to this computer")
    base = wait_report(5)
    if not base:
        print("The lights need a 2.4 GHz WiFi network that this computer is connected to, just for flashing.\n"
              "Your home WiFi is fine; a phone hotspot works too. You choose the lights' real WiFi afterwards.")
        ssid = args.ssid or input("WiFi network name: ").strip()
        password = getpass.getpass("WiFi password: ")
        if not ssid or not password:
            die("WiFi name and password are required")
        base = wait_report(1)
    if not base:
        args.mac = asyncio.run(ble_join(ssid, password, ip, mqtt_port, args.mac, args.yes))
        say("waiting for the controller to join WiFi and connect to this computer (up to 2 min)")
        base = wait_report(120)
    if not base:
        die("the controller never connected to our broker.\n"
            "  - Wrong WiFi password? Unplug the controller 3 times quickly to reset it and retry.\n"
            "  - The controller is 2.4 GHz only and cannot do WPA3-only networks.\n"
            f"  - This computer must be reachable from the lights at {ip}:{mqtt_port}. A firewall prompt\n"
            "    (Windows) must be allowed; guest/IoT WiFi often blocks devices from talking to laptops.\n"
            "  - Run again with --ip if this computer has several network addresses.\n"
            "  - Fixed it? Just run again: a controller that is still trying is picked up without Bluetooth.")
    pid, uuid = base.split("/")[2:4]
    say(f"controller online: product {pid}, device {uuid}")
    url = f"http://{ip}:{http_port}/firmware.bin"
    cmd = {"id": "brizflash", "ts": int(time.time()), "code": "ota", "data": {
        "url": url, "version": version, "md5sum": hashlib.md5(data).hexdigest(), "fileSize": len(data)}}

    def send():
        try:
            conn.publish(base + "/issue", json.dumps(cmd).encode())
        except (OSError, ValueError):
            die("the controller dropped its connection just now. Run again; it reconnects by itself.")
    send()
    say(f"sent the update command, image at {url}")
    seen, last, end = len(inbox), None, time.time() + OTA_WAIT
    while time.time() < end:
        for topic, j, _ in inbox[seen:]:
            seen += 1
            if j.get("code") != "ota_progress":
                continue
            d = j.get("data", {})
            kind, pct, rc = d.get("type"), d.get("percent"), d.get("resCode", 0)
            msg = RESMSG.get(d.get("resMsg", ""), d.get("resMsg", ""))
            if (kind, pct) != last:
                say(f"{kind} {pct}%  {msg}")
                last = (kind, pct)
            if kind == "done":
                say("SUCCESS: the controller flashed the new firmware and is rebooting into it.")
                return
            if kind == "fail" and d.get("resMsg") == "重试过于频繁":
                # The controller refuses an update within 10 s of the previous one, and of its own boot.
                say("controller is not ready yet (it refuses updates for 10 s); waiting 11 s, then sending again")
                time.sleep(11)
                send()
            elif kind == "fail":
                die(f"the controller reported a failure (code {rc}: {msg}). Fix the cause and run again; "
                    "the lights still run the old firmware.")
        time.sleep(0.5)
    die("timed out waiting for the update to finish; the old firmware is still in place")


# ---------- after flashing: help the user find the new ESPHome device ----------
def esphome_name(project: str, ble_mac: str | None) -> str:
    """Node name the new firmware will use. The bootstrap appends the last 6 hex digits of the
    WiFi MAC (name_add_mac_suffix); on the ESP32-C2 the BLE MAC is the WiFi MAC + 2."""
    if project != "esphome-web-26mhz":
        return project
    if not ble_mac:
        return project + "-xxxxxx"
    return f"{project}-{(int(ble_mac.replace(':', ''), 16) - 2) & 0xFFFFFF:06x}"


def after_flash(project: str, ble_mac: str | None):
    name = esphome_name(project, ble_mac)
    if project == "esphome-web-26mhz":
        print(f"""
Next: give the lights their WiFi (the network you just used is forgotten).
  1. In about 30 seconds a new WiFi network appears: {name}
  2. Join it with your phone (no password). A setup page opens by itself;
     if not, open http://192.168.4.1 in the phone's browser.
  3. Pick the WiFi the lights should live on, type its password, Save.
""", flush=True)
    print(f"Then look for {name} in Home Assistant (Settings > Devices > Discovered).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", type=Path, nargs="?",
                    help="app image (firmware.ota.bin); default: the bootstrap image")
    ap.add_argument("--ssid", help="2.4 GHz WiFi this computer is on, used only while flashing (asked if missing)")
    ap.add_argument("--ip", help="this computer's address as the lights see it (auto-detected)")
    ap.add_argument("--mac", help="Bluetooth address of the controller if several are nearby")
    ap.add_argument("--yes", action="store_true", help="no questions: skip the blink test and flash the controller found")
    args = ap.parse_args()
    if args.firmware and not args.firmware.exists():
        die(f"{args.firmware} does not exist")
    data, version, project = load_image(args.firmware or fetch_bootstrap())
    ip = args.ip or lan_ip()
    mqtt_port, http_port = free_port(MQTT_PORT), free_port(HTTP_PORT)
    broker = start_broker(mqtt_port)
    say(f"broker on {ip}:{mqtt_port}, firmware served on {ip}:{http_port}")
    got = []
    start_http(data, http_port, lambda: got or (got.append(1), say("controller is downloading the image")))
    flash(data, version, ip, mqtt_port, http_port, broker.inbox, args)
    after_flash(project, args.mac)


if __name__ == "__main__":
    main()
