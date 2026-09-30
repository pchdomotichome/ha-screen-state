#!/usr/bin/python3
"""
pc-screen-agent — Publica el estado de la pantalla de esta PC a Home Assistant.

El agente NO decide nada: sólo informa. Toda la lógica (qué luz encender, con qué
condiciones, qué automatizaciones) vive en Home Assistant como automatizaciones
sobre la entidad `binary_sensor.<slug>_pantalla`.

Dos modos, independientes y combinables:

  1) PUSH  -> publica el estado en HA con la REST API
              (POST /api/states/binary_sensor.<slug>_pantalla)
              Config: HA_URL, HA_TOKEN, HA_ENTITY_ID

  2) PULL  -> sirve HTTP + SSE para que una integración de HA lo consulte
              GET  /state   -> estado completo en JSON
              GET  /events  -> stream SSE con un evento por cada cambio
              GET  /health  -> para el config flow de la integración
              Config: AGENT_BIND, AGENT_PORT, AGENT_TOKEN

Detección de pantalla: org.gnome.ScreenSaver (GNOME, X11 o Wayland) +
login1 PrepareForSleep (suspensión/reanudación del sistema).

Configuración por archivo .env (ver agent.env.example).
"""
import argparse
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except ImportError:  # pragma: no cover
    sys.exit(
        "Falta PyGObject (gi). Ejecútalo con el Python del sistema: /usr/bin/python3"
    )

VERSION = "1.0.0"
log = logging.getLogger("pc-screen-agent")

DEFAULTS = {
    "HOST_SLUG": "pc",
    "HOST_LABEL": "",
    "AGENT_BIND": "0.0.0.0",
    "AGENT_PORT": "8099",
    "AGENT_TOKEN": "",
    "HA_URL": "",
    "HA_TOKEN": "",
    "HA_ENTITY_ID": "",
    "HA_HEARTBEAT": "300",
    "HA_TIMEOUT": "8",
    "DETECTORS": "gnome,logind",
    "LOG_LEVEL": "INFO",
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config():
    candidates = []
    if os.environ.get("PC_SCREEN_AGENT_CONFIG"):
        candidates.append(os.environ["PC_SCREEN_AGENT_CONFIG"])
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "agent.env"))
    candidates.append(os.path.expanduser("~/.config/pc-screen-agent/agent.env"))
    candidates.append("/etc/pc-screen-agent/agent.env")

    cfg = dict(DEFAULTS)
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    else:
        log.warning("No se encontró agent.env; uso valores por defecto")

    # Las variables de entorno tienen prioridad sobre el archivo
    for key in DEFAULTS:
        if key in os.environ:
            cfg[key] = os.environ[key]

    cfg["PORT"] = int(cfg["AGENT_PORT"])
    cfg["HA_HEARTBEAT"] = max(30, int(cfg["HA_HEARTBEAT"]))
    cfg["HA_TIMEOUT"] = int(cfg["HA_TIMEOUT"])
    cfg["SLUG"] = cfg["HOST_SLUG"].strip().lower().replace(" ", "_")
    cfg["LABEL"] = cfg["HOST_LABEL"] or cfg["SLUG"]
    cfg["ENTITY_ID"] = cfg["HA_ENTITY_ID"].strip() or "binary_sensor.%s_pantalla" % cfg["SLUG"]
    cfg["DETECTORS"] = [d.strip() for d in cfg["DETECTORS"].split(",") if d.strip()]
    cfg["CONFIG_PATH"] = path
    return cfg


# --------------------------------------------------------------------- estado


class State:
    """Estado de la pantalla, compartido entre el hilo de DBus y el HTTP."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.active = True
        self.suspended = False
        self.since = now_iso()
        self.reason = "inicio"
        self.started = time.monotonic()
        self._cond = threading.Condition()

    def snapshot(self):
        return {
            "state": "on" if self.active else "off",
            "active": self.active,
            "suspended": self.suspended,
            "since": self.since,
            "reason": self.reason,
            "host_slug": self.cfg["SLUG"],
            "hostname": socket.gethostname(),
            "version": VERSION,
            "uptime_s": int(time.monotonic() - self.started),
            "detectors": self.cfg["DETECTORS"],
            "friendly_name": "%s Pantalla" % self.cfg["LABEL"],
        }

    def update(self, active=None, suspended=None, reason=""):
        changed = False
        with self._cond:
            if active is not None and active != self.active:
                self.active = active
                changed = True
            if suspended is not None and suspended != self.suspended:
                self.suspended = suspended
                changed = True
            if changed:
                self.since = now_iso()
                self.reason = reason or self.reason
            payload = self.snapshot()
            if changed:
                # Avisar a los clientes SSE (dentro del lock: Condition lo exige)
                self._cond.notify_all()
        if changed:
            log.info("pantalla=%s suspendida=%s (%s)", payload["state"], payload["suspended"],
                     payload["reason"])
        return changed

    def wait_for_change(self, timeout):
        """Bloquea hasta que haya un cambio o venza el timeout (latido SSE)."""
        with self._cond:
            self._cond.wait(timeout)
            return False


# ------------------------------------------------------------------ publicador


class Publisher(threading.Thread):
    """Publica el estado en la REST API de HA (modo push)."""

    daemon = True

    def __init__(self, cfg, state):
        super().__init__(name="publisher", daemon=True)
        self.cfg = cfg
        self.state = state
        self.q = queue.Queue()
        self.backoff = 5.0

    def notify(self, reason):
        if self.enabled:
            self.q.put(reason)

    @property
    def enabled(self):
        return bool(self.cfg["HA_URL"] and self.cfg["HA_TOKEN"])

    def run(self):
        if not self.enabled:
            log.info("Modo push desactivado (falta HA_URL o HA_TOKEN)")
            return
        log.info("Publicando en %s (%s) cada %ss como máximo",
                 self.cfg["HA_URL"], self.cfg["ENTITY_ID"], self.cfg["HA_HEARTBEAT"])
        while True:
            try:
                # Espera hasta HEARTBEAT segundos: si no hay cambios, republica
                # (así la entidad reaparece si Home Assistant se reinició).
                reason = self.q.get(timeout=self.cfg["HA_HEARTBEAT"])
            except queue.Empty:
                reason = "heartbeat"
            if self.publish():
                self.backoff = 5.0
            else:
                self.q.put(reason)
                time.sleep(self.backoff)
                self.backoff = min(120.0, self.backoff * 2)

    def publish(self):
        snap = self.state.snapshot()
        url = "%s/api/states/%s" % (self.cfg["HA_URL"].rstrip("/"), self.cfg["ENTITY_ID"])
        payload = {
            "state": snap["state"],
            "attributes": {
                "friendly_name": snap["friendly_name"],
                "device_class": "running",
                "icon": "mdi:monitor",
                "active": snap["active"],
                "suspended": snap["suspended"],
                "reason": snap["reason"],
                "since": snap["since"],
                "host_slug": snap["host_slug"],
                "hostname": snap["hostname"],
                "agent_version": snap["version"],
                "uptime_s": snap["uptime_s"],
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer %s" % self.cfg["HA_TOKEN"],
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg["HA_TIMEOUT"]):
                log.info("publicado %s = %s", self.cfg["ENTITY_ID"], snap["state"])
                return True
        except Exception as exc:
            log.warning("No se pudo publicar en HA: %s", exc)
            return False


# ---------------------------------------------------------------- servidor HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "pc-screen-agent/" + VERSION
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- utilidades

    @property
    def agent(self):
        return self.server.agent

    def _authorized(self):
        token = self.agent.cfg["AGENT_TOKEN"]
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        return header == "Bearer %s" % token

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silencia el log por petición
        log.debug("http: " + fmt, *args)

    # -------------------------------------------------------------------- GET

    def do_GET(self):
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True, "version": VERSION, "state": self.agent.state.snapshot()})
        elif path in ("/state", "/"):
            self._json(200, self.agent.state.snapshot())
        elif path == "/events":
            self._events()
        else:
            self._json(404, {"error": "not found",
                             "endpoints": ["/state", "/events", "/health"]})

    def _events(self):
        """Server-Sent Events: un evento por cada cambio de estado."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        agent = self.agent
        try:
            self._send_event(agent.state.snapshot())
            while True:
                # Espera bloqueante con timeout: 25 s como máximo entre eventos,
                # el cliente refresca la conexión y nosotros mandamos un latido.
                changed = agent.state.wait_for_change(25.0)
                self._send_event(agent.state.snapshot())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_event(self, snapshot):
        payload = json.dumps(snapshot)
        self.wfile.write(b"data: " + payload.encode() + b"\n\n")
        self.wfile.flush()


# --------------------------------------------------------------------- agente


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg)
        self.publisher = Publisher(cfg, self.state)
        self.http = None
        # Referencias a las conexiones DBus (mantenerlas vivas = mantener suscripciones)
        self.session_bus = None
        self.system_bus = None

    # ------------------------------------------------------------- servidor

    def start_http(self):
        handler = type("BoundHandler", (Handler,), {})
        self.http = ThreadingHTTPServer((self.cfg["AGENT_BIND"], self.cfg["PORT"]), handler)
        self.http.agent = self
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, name="http", daemon=True).start()
        log.info("HTTP escuchando en http://%s:%d (/state, /events, /health)",
                 self.cfg["AGENT_BIND"], self.cfg["PORT"])

    # ------------------------------------------------------------- detectores

    def setup_detectors(self):
        detectors = self.cfg["DETECTORS"]
        try:
            if "gnome" in detectors:
                self._setup_gnome()
            if "logind" in detectors:
                self._setup_logind()
        except Exception as exc:
            log.error("Error preparando detectores: %s", exc)

    def _setup_gnome(self):
        # OJO: hay que conservar la referencia a la conexión; si el objeto Gio
        # se libera, DBus cancela las suscripciones a señales.
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session_bus = bus
        res = bus.call_sync(
            "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
            "GetActive", None, None, Gio.DBusCallFlags.NONE, 5000, None)
        active = not bool(res.unpack()[0])
        bus.signal_subscribe(
            sender="org.gnome.ScreenSaver",
            interface_name="org.gnome.ScreenSaver",
            member="ActiveChanged",
            object_path="/org/gnome/ScreenSaver",
            arg0=None,
            flags=Gio.DBusSignalFlags.NONE,
            callback=self._on_active_changed,
        )
        self.state.update(active=active, reason="arranque")
        log.info("Detector GNOME activo (pantalla=%s)", "on" if active else "off")

    def _on_active_changed(self, _conn, _sender, _path, _iface, _member, params):
        try:
            screensaver = bool(params.unpack()[0])
        except Exception:
            return
        active = not screensaver
        if self.state.update(active=active, reason="pantalla" + (
                " encendida" if active else " apagada/blanqueada")):
            self.publisher.notify("active-changed")

    def _setup_logind(self):
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.system_bus = bus
        bus.signal_subscribe(
            sender="org.freedesktop.login1",
            interface_name="org.freedesktop.login1.Manager",
            member="PrepareForSleep",
            object_path="/org/freedesktop/login1",
            arg0=None,
            flags=Gio.DBusSignalFlags.NONE,
            callback=self._on_prepare_for_sleep,
        )
        log.info("Detector logind (suspensión) activo")

    def _on_prepare_for_sleep(self, _conn, _sender, _path, _iface, _member, params):
        try:
            sleeping = bool(params.unpack()[0])
        except Exception:
            return
        if self.state.update(suspended=sleeping,
                             reason="suspension" if sleeping else "reanudacion"):
            # Al reanudar, la pantalla puede seguir apagada: reconciliamos con GNOME.
            if not sleeping:
                GLib.timeout_add(2000, self._resync_screen)
            self.publisher.notify("sleep")
            if sleeping:
                GLib.timeout_add(300, self._force_off)

    def _force_off(self):
        if self.state.suspended and self.state.active:
            if self.state.update(active=False, reason="suspension"):
                self.publisher.notify("suspended")
        return GLib.SOURCE_REMOVE

    def _resync_screen(self):
        try:
            bus = self.session_bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.session_bus = bus
            res = bus.call_sync(
                "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                "GetActive", None, None, Gio.DBusCallFlags.NONE, 5000, None)
            active = not bool(res.unpack()[0])
            if self.state.update(active=active, reason="reanudacion"):
                self.publisher.notify("resync")
        except Exception as exc:
            log.debug("No se pudo resincronizar la pantalla: %s", exc)
        return GLib.SOURCE_REMOVE


# -------------------------------------------------------------------- comandos


def cmd_check(cfg):
    print("Config: %s" % (cfg["CONFIG_PATH"] or "(valores por defecto)"))
    print("Host slug: %s   entidad: %s   puerto: %d" % (cfg["SLUG"], cfg["ENTITY_ID"], cfg["PORT"]))
    print("Detectores: %s" % ", ".join(cfg["DETECTORS"]))

    agent = Agent(cfg)
    try:
        agent.setup_detectors()
        print("OK   detectores inicializados; estado actual:", json.dumps(agent.state.snapshot()))
    except Exception as exc:
        print("FALLO detectores: %s" % exc)
        return False

    ok = True
    if cfg["HA_URL"] and cfg["HA_TOKEN"]:
        try:
            req = urllib.request.Request(
                "%s/api/" % cfg["HA_URL"].rstrip("/"),
                headers={"Authorization": "Bearer %s" % cfg["HA_TOKEN"]})
            with urllib.request.urlopen(req, timeout=cfg["HA_TIMEOUT"]):
                print("OK   Home Assistant responde (%s)" % cfg["HA_URL"])
        except Exception as exc:
            print("FALLO Home Assistant: %s" % exc)
            ok = False
    else:
        print("AVISO modo push desactivado (falta HA_URL o HA_TOKEN)")

    try:
        with socket.create_connection(("127.0.0.1", cfg["PORT"]), timeout=1):
            print("AVISO el puerto %d ya está ocupado" % cfg["PORT"])
    except OSError:
        pass
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="Publica el estado de la pantalla de esta PC a Home Assistant.")
    parser.add_argument("--check", action="store_true",
                        help="Valida configuración, detectores y conexión a HA")
    parser.add_argument("--print-state", action="store_true",
                        help="Imprime el estado actual y sale")
    args = parser.parse_args()

    cfg = load_config()
    logging.basicConfig(level=cfg["LOG_LEVEL"].upper(),
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)

    if args.check:
        sys.exit(0 if cmd_check(cfg) else 1)

    agent = Agent(cfg)
    agent.setup_detectors()

    if args.print_state:
        print(json.dumps(agent.state.snapshot(), indent=2))
        return

    agent.publisher.notify("arranque")
    agent.publisher.start()
    agent.start_http()
    log.info("pc-screen-agent %s iniciado (slug=%s)", VERSION, cfg["SLUG"])
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()