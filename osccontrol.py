# Copyright (c) 2026 Ximmer's Creations <ximmer@ximmer.dev>.
# Licensed under the source-available proprietary terms in LICENSE.
# Personal noncommercial use and private modifications only; see LICENSE.

import asyncio
import ipaddress
import json
import logging
import queue
import sys
from logging.handlers import RotatingFileHandler
import os
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, X, Y, StringVar, Tk, messagebox
from tkinter import ttk
from urllib.parse import quote

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient


OSCQUERY_URL_OVERRIDE = os.getenv("VRCHAT_OSCQUERY_URL")
OSCQUERY_URL = OSCQUERY_URL_OVERRIDE or "http://127.0.0.1:9001/"
OSC_SEND_HOST_OVERRIDE = os.getenv("VRCHAT_OSC_HOST")
OSC_SEND_HOST = OSC_SEND_HOST_OVERRIDE or "127.0.0.1"
OSC_SEND_PORT_OVERRIDE = os.getenv("VRCHAT_OSC_PORT")
OSC_SEND_PORT = int(OSC_SEND_PORT_OVERRIDE or "9000")
OSC_RECEIVE_HOST = os.getenv("VRCHAT_OSC_LISTEN_HOST", "127.0.0.1")
OSC_RECEIVE_PORT = int(os.getenv("VRCHAT_OSC_LISTEN_PORT", "9001"))
OSCQUERY_SERVICE_TYPE = "_oscjson._tcp.local."
SHARED_PARAMETERS_FILE = Path(
    os.getenv(
        "VRCHAT_OSC_SHARED_PARAMETERS_FILE",
        str(Path(os.getenv("LOCALAPPDATA", Path.home())) / "VRChatOSCControl" / "shared_parameters.json"),
    )
).expanduser()
API_WS_URL = os.getenv("OSC_API_WS_URL", "wss://osccontrol.app/ws")
CONTROL_URL_TEMPLATE = os.getenv("OSC_API_CONTROL_URL", "https://osccontrol.app/?token={token}")

OSC_TYPE_NAMES = {"i": "int", "f": "float", "s": "string", "T": "bool", "F": "bool"}
SUPPORTED_TYPES = ("bool", "int", "float", "string")
logger = logging.getLogger(__name__)


def configure_logging():
    configured_level = os.getenv("VRCHAT_OSC_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, configured_level, logging.INFO)
    log_path = Path(
        os.getenv(
            "VRCHAT_OSC_LOG_FILE",
            str(Path(os.getenv("LOCALAPPDATA", Path.home())) / "VRChatOSCControl" / "app.log"),
        )
    ).expanduser()
    handlers = [logging.StreamHandler()]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(
            0,
            RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
        )
    except OSError as error:
        print(f"Could not create log file {log_path}: {error}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if len(handlers) > 1:
        logger.info("Logging initialized at %s", log_path)
    else:
        logger.warning("File logging unavailable; writing logs to stderr")


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def set_application_icon(root):
    icon_path = resource_path("resource/icon.ico")
    try:
        root.iconbitmap(str(icon_path))
        logger.info("Loaded application icon from %s", icon_path)
    except Exception:
        logger.exception("Could not load application icon from %s", icon_path)


@dataclass(frozen=True)
class Parameter:
    path: str
    name: str
    type: str


@dataclass(frozen=True)
class OscQueryConnection:
    url: str
    osc_host: str
    osc_port: int


def load_shared_parameters(file_path, avatar_id):
    try:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        raise ValueError("Shared parameter file must contain an object keyed by avatar ID")
    entries = data.get(avatar_id, [])
    if not isinstance(entries, list):
        raise ValueError(f"Saved parameters for avatar {avatar_id!r} must be a list")

    parameters = []
    names = set()
    paths = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path, name, parameter_type = (
            entry.get("path"),
            entry.get("name"),
            entry.get("type"),
        )
        if (
            not isinstance(path, str)
            or not path.startswith("/avatar/parameters/")
            or path.endswith("/")
            or not isinstance(name, str)
            or not name.strip()
            or parameter_type not in SUPPORTED_TYPES
            or path in paths
            or name.casefold() in names
        ):
            continue
        parameter = Parameter(path=path, name=name, type=parameter_type)
        parameters.append(parameter)
        paths.add(path)
        names.add(name.casefold())
    return parameters


def save_shared_parameters(file_path, avatar_id, parameters):
    if not isinstance(avatar_id, str) or not avatar_id or avatar_id == "Waiting for VRChat":
        raise ValueError("Cannot save shared parameters without an active avatar ID")
    file_path = Path(file_path)
    if file_path.exists():
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Shared parameter file must contain an object keyed by avatar ID")
    else:
        data = {}
    data[avatar_id] = [
        {"path": item.path, "name": item.name, "type": item.type}
        for item in parameters
    ]
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = file_path.with_name(f"{file_path.name}.tmp")
    temporary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary_path.replace(file_path)


def build_oscquery_connection(server, query_port, properties=None, addresses=()):
    properties = properties or {}
    decoded_properties = {
        key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key):
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        for key, value in properties.items()
    }
    hosts = [str(address) for address in addresses]
    ipv4_host = next(
        (host for host in hosts if isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)),
        None,
    )
    query_host = ipv4_host or server.rstrip(".")
    if not query_host:
        raise ValueError("OSCQuery service did not advertise a hostname or address")
    query_port = int(query_port)
    if not 1 <= query_port <= 65535:
        raise ValueError("OSCQuery service advertised an invalid TCP port")
    url_host = f"[{query_host}]" if ":" in query_host else query_host
    osc_host = decoded_properties.get("OSC_IP") or OSC_SEND_HOST
    if osc_host in ("0.0.0.0", "::"):
        osc_host = ipv4_host or OSC_SEND_HOST
    try:
        osc_port = int(decoded_properties.get("OSC_PORT", OSC_SEND_PORT))
        if not 1 <= osc_port <= 65535:
            raise ValueError
    except ValueError:
        logger.warning("Invalid OSC_PORT in OSCQuery service advertisement; using %s", OSC_SEND_PORT)
        osc_port = OSC_SEND_PORT
    return OscQueryConnection(
        url=f"http://{url_host}:{query_port}/",
        osc_host=osc_host,
        osc_port=osc_port,
    )


def flatten_oscquery_parameters(document):
    """Return supported VRChat avatar parameters from an OSCQuery tree."""
    found = {}

    def visit(node, parent_path=""):
        if not isinstance(node, dict):
            return
        path = node.get("FULL_PATH") or parent_path
        if path.startswith("/avatar/parameters/") and node.get("TYPE"):
            type_tag = node["TYPE"]
            parameter_type = OSC_TYPE_NAMES.get(type_tag[0]) if type_tag else None
            if parameter_type:
                found[path] = parameter_type
        contents = node.get("CONTENTS", {})
        if isinstance(contents, dict):
            for child_name, child in contents.items():
                child_path = child.get("FULL_PATH") if isinstance(child, dict) else None
                if not child_path:
                    child_path = f"{path.rstrip('/')}/{child_name}"
                visit(child, child_path)

    visit(document, "/")
    return sorted(found.items())


def filter_oscquery_parameters(parameters, query):
    query = query.strip().casefold()
    if not query:
        return list(parameters)
    return [(path, parameter_type) for path, parameter_type in parameters if query in path.casefold()]


def oscquery_value(document):
    if not isinstance(document, dict) or "VALUE" not in document:
        return None
    value = document["VALUE"]
    if isinstance(value, list):
        if len(value) == 1:
            return value[0]
        if not value:
            return None
    return value


def extract_oscquery_values(document):
    values = {}

    def visit(node, parent_path=""):
        if not isinstance(node, dict):
            return
        path = node.get("FULL_PATH") or parent_path
        if "VALUE" in node:
            value = oscquery_value(node)
            if value is not None:
                values[path] = value
        contents = node.get("CONTENTS", {})
        if isinstance(contents, dict):
            for child_name, child in contents.items():
                child_path = child.get("FULL_PATH") if isinstance(child, dict) else None
                if not child_path:
                    child_path = f"{path.rstrip('/')}/{child_name}"
                visit(child, child_path)

    visit(document, "/")
    return values


def query_oscquery_value(base_url, path, timeout=2):
    url = f"{base_url.rstrip('/')}{quote(path, safe='/')}?VALUE"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return oscquery_value(json.loads(response.read().decode("utf-8")))


def serialize_parameters(parameters, values):
    missing = [
        parameter.path
        for parameter in parameters
        if parameter.path not in values or values[parameter.path] is None
    ]
    if missing:
        raise ValueError(f"Missing current values for: {', '.join(missing)}")
    return [
        {
            "path": parameter.path,
            "name": parameter.name,
            "type": parameter.type,
            "value": values[parameter.path],
        }
        for parameter in parameters
    ]


def build_registration_payload(parameters, values):
    return {
        "type": "register",
        "version": 1,
        "parameters": serialize_parameters(parameters, values),
    }


def build_add_parameters(token, parameters, values):
    return {
        "type": "add_parameters",
        "token": token,
        "parameters": serialize_parameters(parameters, values),
    }


def build_remove_parameters(token, paths):
    return {"type": "remove_parameters", "token": token, "paths": list(paths)}


def build_clear_parameters(token):
    return {"type": "clear_parameters", "token": token}


def build_parameter_update(token, path, value):
    return {"type": "update_parameter", "token": token, "path": path, "value": value}


def coerce_control_value(value, parameter_type):
    if parameter_type == "bool":
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ValueError("Expected a boolean value")
    if parameter_type == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Expected a numeric integer value")
        if int(value) != value:
            raise ValueError("Expected a whole number")
        return int(value)
    if parameter_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Expected a numeric value")
        return float(value)
    if parameter_type == "string" and isinstance(value, str):
        return value
    raise ValueError(f"Expected a {parameter_type} value")


def parse_parameter_changed(message, registered_parameters):
    if not isinstance(message, dict) or message.get("type") != "parameter_changed":
        raise ValueError("Expected a parameter_changed websocket message")
    path = message.get("path")
    parameter = registered_parameters.get(path)
    if parameter is None:
        raise ValueError(f"Parameter path is not registered: {path!r}")
    if message.get("name") != parameter.name:
        raise ValueError(f"Parameter name did not match registered path {path!r}")
    if message.get("parameter_type") != parameter.type:
        raise ValueError(f"Parameter type did not match registered path {path!r}")
    value = coerce_control_value(message.get("value"), parameter.type)
    return parameter, value


class UnsupportedProtocolVersion(RuntimeError):
    pass


def registration_token_from_acknowledgement(acknowledgement):
    if not isinstance(acknowledgement, dict):
        raise RuntimeError("Service registration acknowledgement was not a JSON object")
    if acknowledgement.get("type") == "error":
        message = acknowledgement.get("message", "Registration was rejected")
        if acknowledgement.get("code") == "unsupported_version":
            raise UnsupportedProtocolVersion(
                "The API rejected this client's protocol version. This client likely needs an "
                f"update. API response: {message}"
            )
        raise RuntimeError(message)
    if acknowledgement.get("type") != "registered":
        raise RuntimeError("Service did not acknowledge registration")
    token = acknowledgement.get("token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Service registration response did not include a valid token")
    return token


class OscControlApp:
    def __init__(self, root):
        self.root = root
        self.root.title("VRChat OSC Control")
        self.root.geometry("780x650")
        self.root.minsize(660, 850)

        self.parameters = {}
        self.parameter_values = {}
        self.parameter_update_queue = queue.Queue()
        self.websocket_ready = threading.Event()
        self.available_parameters = []
        self.active_avatar = "Waiting for VRChat"
        self.oscquery_url = OSCQUERY_URL
        self.osc_send_host = OSC_SEND_HOST
        self.osc_send_port = OSC_SEND_PORT
        self.session_stop = threading.Event()
        self.session_token = None
        self.discovery_stop = threading.Event()
        self.session_thread = None
        self.osc_server = None
        self.zeroconf = None
        self.service_browser = None

        self.avatar_var = StringVar(value=self.active_avatar)
        self.status_var = StringVar(value="Starting VRChat OSC listener...")
        self.path_var = StringVar()
        self.custom_name_var = StringVar()
        self.type_var = StringVar(value="float")
        self.share_link_var = StringVar()
        self.parameter_filter_var = StringVar()
        self.parameter_count_var = StringVar(value="0 parameters")

        self._build_ui()
        self._start_osc_listener()
        self._start_service_discovery()
        self.root.after(250, self.refresh_parameters)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill=X, pady=(0, 14))
        ttk.Label(heading, text="VRChat OSC Control", font=("Segoe UI", 18, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            heading,
            text="Choose avatar parameters, name them, and publish a control session.",
        ).pack(anchor="w", pady=(3, 0))

        avatar_row = ttk.Frame(outer)
        avatar_row.pack(fill=X, pady=(0, 12))
        ttk.Label(avatar_row, text="Active avatar", width=15).pack(side=LEFT)
        ttk.Label(avatar_row, textvariable=self.avatar_var).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(avatar_row, text="Refresh parameters", command=self.refresh_parameters).pack(
            side=RIGHT
        )

        body = ttk.Panedwindow(outer, orient="vertical")
        body.pack(fill=BOTH, expand=True)

        browser = ttk.Labelframe(body, text="Avatar parameters", padding=10)
        body.add(browser, weight=1)
        filter_row = ttk.Frame(browser)
        filter_row.pack(fill=X, pady=(0, 8))
        ttk.Label(filter_row, text="Search").pack(side=LEFT)
        ttk.Entry(filter_row, textvariable=self.parameter_filter_var).pack(
            side=LEFT, fill=X, expand=True, padx=(8, 8)
        )
        ttk.Button(filter_row, text="Clear", command=lambda: self.parameter_filter_var.set("")).pack(
            side=LEFT
        )
        ttk.Label(filter_row, textvariable=self.parameter_count_var, width=18, anchor="e").pack(
            side=RIGHT, padx=(8, 0)
        )
        self.parameter_filter_var.trace_add("write", self._filter_available)

        list_frame = ttk.Frame(browser)
        list_frame.pack(fill=BOTH, expand=True)
        self.available_list = ttk.Treeview(
            list_frame, columns=("path", "type"), show="headings", height=7
        )
        self.available_list.heading("path", text="OSC parameter")
        self.available_list.heading("type", text="Type")
        self.available_list.column("path", width=500, minwidth=260)
        self.available_list.column("type", width=90, anchor="center")
        available_scroll = ttk.Scrollbar(
            list_frame, orient=VERTICAL, command=self.available_list.yview
        )
        self.available_list.configure(yscrollcommand=available_scroll.set)
        self.available_list.pack(side=LEFT, fill=BOTH, expand=True)
        available_scroll.pack(side=RIGHT, fill=Y)
        self.available_list.bind("<<TreeviewSelect>>", self._select_available)

        add_row = ttk.Frame(browser)
        add_row.pack(fill=X, pady=(10, 0))
        ttk.Label(add_row, text="Custom name").pack(side=LEFT)
        ttk.Entry(add_row, textvariable=self.custom_name_var, width=22).pack(
            side=LEFT, padx=(6, 12)
        )

        manual = ttk.Frame(browser)
        manual.pack(fill=X, pady=(8, 0))
        ttk.Label(manual, text="OSC path").pack(side=LEFT)
        ttk.Entry(manual, textvariable=self.path_var).pack(
            side=LEFT, fill=X, expand=True, padx=(6, 8)
        )
        ttk.Combobox(
            manual, textvariable=self.type_var, values=SUPPORTED_TYPES, state="readonly", width=8
        ).pack(side=LEFT, padx=(0, 8))
        ttk.Button(manual, text="Add", command=self.add_manual).pack(side=LEFT)

        registered = ttk.Labelframe(body, text="Shared parameters", padding=10)
        body.add(registered, weight=1)
        self.registered_list = ttk.Treeview(
            registered, columns=("name", "path", "type"), show="headings", height=6
        )
        for column, label in (("name", "Shared name"), ("path", "OSC path"), ("type", "Type")):
            self.registered_list.heading(column, text=label)
        self.registered_list.column("name", width=150)
        self.registered_list.column("path", width=390, minwidth=200)
        self.registered_list.column("type", width=80, anchor="center")
        self.registered_list.pack(fill=BOTH, expand=True)
        ttk.Button(registered, text="Remove selected", command=self.remove_selected).pack(
            anchor="e", pady=(8, 0)
        )

        session = ttk.Labelframe(outer, text="Control session", padding=10)
        session.pack(fill=X, pady=(12, 0))
        self.register_button = ttk.Button(
            session, text="Connect and create share link", command=self.register_session
        )
        self.register_button.pack(anchor="w")
        link_row = ttk.Frame(session)
        link_row.pack(fill=X, pady=(8, 0))
        ttk.Entry(link_row, textvariable=self.share_link_var, state="readonly").pack(
            side=LEFT, fill=X, expand=True
        )
        self.copy_button = ttk.Button(link_row, text="Copy", command=self.copy_link, state="disabled")
        self.copy_button.pack(side=LEFT, padx=(8, 0))
        self.open_button = ttk.Button(link_row, text="Open", command=self.open_link, state="disabled")
        self.open_button.pack(side=LEFT, padx=(6, 0))

        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(fill=X, pady=(9, 0))

    def _start_osc_listener(self):
        logger.info("Starting VRChat OSC listener on %s:%s", OSC_RECEIVE_HOST, OSC_RECEIVE_PORT)
        dispatcher = Dispatcher()
        dispatcher.map("/avatar/change", self._on_avatar_change)
        dispatcher.set_default_handler(self._on_osc_parameter)
        try:
            self.osc_server = ThreadingOSCUDPServer(
                (OSC_RECEIVE_HOST, OSC_RECEIVE_PORT), dispatcher
            )
        except OSError as error:
            logger.exception("Could not start VRChat OSC listener")
            self.status_var.set(f"Could not listen for VRChat avatar changes: {error}")
            return
        threading.Thread(target=self.osc_server.serve_forever, daemon=True).start()
        logger.info("VRChat OSC listener started")

    def _start_service_discovery(self):
        threading.Thread(target=self._discover_oscquery_service, daemon=True).start()

    def _discover_oscquery_service(self):
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

            app = self

            class OscQueryListener(ServiceListener):
                def add_service(self, zeroconf, service_type, name):
                    self.update_service(zeroconf, service_type, name)

                def update_service(self, zeroconf, service_type, name):
                    service_info = zeroconf.get_service_info(service_type, name, timeout=3000)
                    if service_info is None:
                        logger.warning("Could not resolve OSCQuery service %s", name)
                        return
                    try:
                        connection = build_oscquery_connection(
                            service_info.server,
                            service_info.port,
                            service_info.properties,
                            service_info.parsed_addresses(),
                        )
                    except (TypeError, ValueError) as error:
                        logger.exception("Invalid OSCQuery service advertisement for %s", name)
                        app.root.after(
                            0,
                            lambda error=error: app.status_var.set(
                                f"Invalid VRChat OSCQuery advertisement: {error}"
                            ),
                        )
                        return
                    logger.info("Discovered OSCQuery service %s at %s", name, connection.url)
                    app.root.after(0, lambda: app._apply_discovered_connection(connection))

                def remove_service(self, _zeroconf, _service_type, name):
                    logger.info("OSCQuery service removed: %s", name)

            self.zeroconf = Zeroconf()
            self.service_browser = ServiceBrowser(
                self.zeroconf, OSCQUERY_SERVICE_TYPE, OscQueryListener()
            )
            logger.info("Searching for VRChat OSCQuery services (%s)", OSCQUERY_SERVICE_TYPE)
        except Exception:
            logger.exception("Could not start OSCQuery service discovery")
            self.root.after(
                0,
                lambda: self.status_var.set(
                    "OSCQuery discovery unavailable; using configured connection defaults."
                ),
            )

    def _apply_discovered_connection(self, connection):
        if not OSCQUERY_URL_OVERRIDE:
            self.oscquery_url = connection.url
        if not OSC_SEND_HOST_OVERRIDE:
            self.osc_send_host = connection.osc_host
        if not OSC_SEND_PORT_OVERRIDE:
            self.osc_send_port = connection.osc_port
        logger.info(
            "Using OSCQuery URL %s and OSC destination %s:%s",
            self.oscquery_url,
            self.osc_send_host,
            self.osc_send_port,
        )
        self.status_var.set(f"VRChat OSCQuery discovered at {self.oscquery_url}")
        self.refresh_parameters()

    def _on_avatar_change(self, _address, avatar_id, *args):
        if not isinstance(avatar_id, str):
            logger.warning("Ignored /avatar/change message without a string avatar ID")
            return
        logger.debug("VRChat avatar changed")
        self.root.after(0, lambda: self._set_active_avatar(avatar_id))

    def _set_active_avatar(self, avatar_id):
        if not isinstance(avatar_id, str) or not avatar_id or avatar_id == self.active_avatar:
            return
        previous_avatar = self.active_avatar
        self.active_avatar = avatar_id
        self.avatar_var.set(avatar_id)
        self.parameter_values.clear()
        logger.info("Active avatar changed from %s to %s", previous_avatar, avatar_id)
        try:
            parameters = load_shared_parameters(SHARED_PARAMETERS_FILE, avatar_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not load shared parameters for avatar %s", avatar_id)
            self.status_var.set(f"Could not load saved parameters: {error}")
            parameters = []
        self._replace_shared_parameters(parameters)
        if self.session_thread and self.session_thread.is_alive():
            if self.websocket_ready.is_set():
                logger.info("Replacing active API parameters for avatar %s", avatar_id)
                self.parameter_update_queue.put(
                    ("replace_parameters", avatar_id, list(parameters))
                )
                self.status_var.set("Synchronizing saved parameters with the active session...")
            else:
                logger.info("Stopping session setup because the avatar changed")
                self.session_stop.set()
        if parameters:
            self.status_var.set(f"Loaded {len(parameters)} saved parameters for this avatar.")
        else:
            self.status_var.set("No saved shared parameters for this avatar.")

    def _replace_shared_parameters(self, parameters):
        self.parameters = {parameter.path: parameter for parameter in parameters}
        self.registered_list.delete(*self.registered_list.get_children())
        for parameter in parameters:
            self.registered_list.insert(
                "",
                END,
                iid=parameter.path,
                values=(parameter.name, parameter.path, parameter.type),
            )

    def _on_osc_parameter(self, address, *args):
        parameter = self.parameters.get(address)
        if parameter is None or not args:
            return
        raw_value = args[0] if len(args) == 1 else list(args)
        try:
            value = coerce_control_value(raw_value, parameter.type)
        except (TypeError, ValueError) as error:
            logger.warning("Ignored invalid OSC value for %s: %s", address, error)
            return
        self.parameter_values[address] = value
        if self.websocket_ready.is_set():
            self.parameter_update_queue.put(("update_parameter", address, value))
        logger.debug("Observed OSC parameter path=%r type=%s", address, parameter.type)

    def refresh_parameters(self):
        logger.info("Refreshing avatar parameters from %s", self.oscquery_url)
        self.status_var.set("Reading avatar parameters from VRChat OSCQuery...")
        threading.Thread(target=self._load_parameters, daemon=True).start()

    def _load_parameters(self):
        try:
            request = urllib.request.Request(
                self.oscquery_url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                document = json.loads(response.read().decode("utf-8"))
            parameters = flatten_oscquery_parameters(document)
            values = extract_oscquery_values(document)
            avatar_id = values.get("/avatar/change")
            if not isinstance(avatar_id, str) or not avatar_id:
                try:
                    avatar_id = query_oscquery_value(self.oscquery_url, "/avatar/change")
                except Exception:
                    logger.debug("OSCQuery did not return the current avatar ID", exc_info=True)
        except Exception as error:
            logger.exception("OSCQuery parameter discovery failed")
            self.root.after(
                0,
                lambda error=error: self.status_var.set(
                    f"OSCQuery unavailable; add a parameter manually. ({error})"
                ),
            )
            return
        self.root.after(0, lambda: self._show_available(parameters, values, avatar_id))

    def _show_available(self, parameters, values=None, avatar_id=None):
        self.available_parameters = list(parameters)
        if isinstance(avatar_id, str) and avatar_id:
            self._set_active_avatar(avatar_id)
        parameter_types = dict(parameters)
        for path, value in (values or {}).items():
            if path not in parameter_types:
                continue
            try:
                self.parameter_values[path] = coerce_control_value(value, parameter_types[path])
            except (TypeError, ValueError):
                logger.debug("Ignoring invalid OSCQuery value for %s", path, exc_info=True)
        self._filter_available()
        if parameters:
            logger.info("Discovered %s supported avatar parameters", len(parameters))
            self.status_var.set(f"Found {len(parameters)} avatar parameters.")
        else:
            logger.warning("OSCQuery returned no supported avatar parameters")
            self.status_var.set("No supported avatar parameters found in OSCQuery.")

    def _filter_available(self, *_args):
        selected = self.available_list.selection()
        selected_path = selected[0] if selected else None
        filtered = filter_oscquery_parameters(
            self.available_parameters, self.parameter_filter_var.get()
        )
        self.available_list.delete(*self.available_list.get_children())
        for path, parameter_type in filtered:
            self.available_list.insert("", END, iid=path, values=(path, parameter_type))
        if selected_path in {path for path, _ in filtered}:
            self.available_list.selection_set(selected_path)
            self.available_list.focus(selected_path)
        self.parameter_count_var.set(f"{len(filtered)} of {len(self.available_parameters)}")

    def _select_available(self, _event=None):
        selection = self.available_list.selection()
        if not selection:
            return
        path = selection[0]
        _, parameter_type = self.available_list.item(path, "values")
        self.path_var.set(path)
        self.type_var.set(parameter_type)
        #if not self.custom_name_var.get().strip():
        self.custom_name_var.set(path.rsplit("/", 1)[-1])

    def add_manual(self):
        self._add_parameter(self.path_var.get(), self.custom_name_var.get(), self.type_var.get())

    def _add_parameter(self, path, name, parameter_type):
        path = path.strip()
        name = name.strip()
        if not path.startswith("/avatar/parameters/") or path.endswith("/"):
            messagebox.showerror("Invalid OSC path", "Use /avatar/parameters/ followed by a name.")
            return
        if not name:
            messagebox.showerror("Name required", "Enter a custom name for this parameter.")
            return
        if parameter_type not in SUPPORTED_TYPES:
            messagebox.showerror("Invalid type", "Choose bool, int, float, or string.")
            return
        if any(parameter.path == path for parameter in self.parameters.values()):
            messagebox.showerror("Already added", "That OSC parameter is already registered.")
            return
        if any(parameter.name.casefold() == name.casefold() for parameter in self.parameters.values()):
            messagebox.showerror("Name already used", "Each shared name must be unique.")
            return
        parameter = Parameter(path=path, name=name, type=parameter_type)
        self.parameters[path] = parameter
        self.registered_list.insert("", END, iid=path, values=(name, path, parameter_type))
        logger.info("Added shared parameter name=%r path=%r type=%s", name, path, parameter_type)
        self._persist_current_shared_parameters()
        if self.websocket_ready.is_set():
            self.parameter_update_queue.put(("add_parameters", [parameter]))
        self.path_var.set("")
        self.custom_name_var.set("")

    def _persist_current_shared_parameters(self):
        if not self.active_avatar or self.active_avatar == "Waiting for VRChat":
            return
        try:
            save_shared_parameters(
                SHARED_PARAMETERS_FILE, self.active_avatar, list(self.parameters.values())
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not save shared parameters for avatar %s", self.active_avatar)
            self.status_var.set(f"Could not save shared parameters: {error}")

    def remove_selected(self):
        paths = list(self.registered_list.selection())
        for path in paths:
            self.parameters.pop(path, None)
            self.registered_list.delete(path)
            logger.info("Removed shared parameter path=%r", path)
        if paths:
            self._persist_current_shared_parameters()
            if self.websocket_ready.is_set():
                self.parameter_update_queue.put(("remove_parameters", paths))

    def register_session(self):
        if not self.parameters:
            logger.warning("Session registration requested with no configured parameters")
            messagebox.showinfo("No parameters", "Add at least one parameter before creating a session.")
            return
        try:
            save_shared_parameters(
                SHARED_PARAMETERS_FILE, self.active_avatar, list(self.parameters.values())
            )
            logger.info(
                "Saved %s shared parameters for avatar %s",
                len(self.parameters),
                self.active_avatar,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not save shared parameters")
            messagebox.showerror("Could not save parameters", str(error))
            return
        if self.session_thread and self.session_thread.is_alive():
            messagebox.showinfo("Session active", "A websocket session is already running.")
            return
        logger.info("Starting session registration for %s parameters", len(self.parameters))
        parameters = list(self.parameters.values())
        avatar_id = self.active_avatar
        self.websocket_ready.clear()
        while True:
            try:
                self.parameter_update_queue.get_nowait()
            except queue.Empty:
                break
        self.session_stop.clear()
        self.session_token = None
        self.register_button.configure(state="disabled")
        self.share_link_var.set("")
        self.copy_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.status_var.set("Connecting to osccontrol.app and registering parameters...")
        self.session_thread = threading.Thread(
            target=self._run_session, args=(parameters, avatar_id), daemon=True
        )
        self.session_thread.start()

    def _run_session(self, parameters, avatar_id):
        asyncio.run(self._websocket_session(parameters, avatar_id))

    def _read_registration_state(self, parameters, fallback_avatar_id):
        values = dict(self.parameter_values)
        avatar_id = None
        try:
            request = urllib.request.Request(
                self.oscquery_url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                state = json.loads(response.read().decode("utf-8"))
            values.update(extract_oscquery_values(state))
            avatar_id = values.get("/avatar/change")
        except Exception:
            logger.debug("Could not read OSCQuery root state before registration", exc_info=True)

        if not isinstance(avatar_id, str) or not avatar_id:
            try:
                avatar_id = query_oscquery_value(self.oscquery_url, "/avatar/change")
            except Exception:
                logger.debug("Could not query current avatar ID", exc_info=True)
        if not isinstance(avatar_id, str) or not avatar_id:
            avatar_id = fallback_avatar_id
        if not isinstance(avatar_id, str) or not avatar_id or avatar_id == "Waiting for VRChat":
            raise RuntimeError("Could not determine the current avatar ID from OSCQuery or /avatar/change")

        for parameter in parameters:
            if parameter.path not in values:
                try:
                    values[parameter.path] = query_oscquery_value(
                        self.oscquery_url, parameter.path
                    )
                except Exception:
                    logger.debug("Could not query current value for %s", parameter.path, exc_info=True)
            if parameter.path not in values:
                raise RuntimeError(f"No current OSC value is available for {parameter.name}")
            values[parameter.path] = coerce_control_value(
                values[parameter.path], parameter.type
            )
        return avatar_id, values

    def _read_parameter_values(self, parameters):
        values = {}
        for parameter in parameters:
            try:
                value = query_oscquery_value(self.oscquery_url, parameter.path)
            except Exception:
                logger.debug("Could not query current value for %s", parameter.path, exc_info=True)
                value = self.parameter_values.get(parameter.path)
            if value is None:
                raise RuntimeError(f"No current OSC value is available for {parameter.name}")
            values[parameter.path] = coerce_control_value(value, parameter.type)
        return values

    def _queue_current_parameter_updates(self, parameters, initial_values):
        for parameter in parameters:
            current_value = self.parameter_values.get(parameter.path)
            if current_value is not None and current_value != initial_values[parameter.path]:
                self.parameter_update_queue.put(
                    ("update_parameter", parameter.path, current_value)
                )

    async def _websocket_session(self, parameters, fallback_avatar_id):
        import websockets

        osc_client = SimpleUDPClient(self.osc_send_host, self.osc_send_port)
        try:
            avatar_id, values = await asyncio.to_thread(
                self._read_registration_state, parameters, fallback_avatar_id
            )
            payload = build_registration_payload(parameters, values)
            logger.info("Registering %s parameters for avatar %s", len(parameters), avatar_id)
            logger.info("Connecting to control websocket")
            async with websockets.connect(API_WS_URL, open_timeout=10) as websocket:
                await websocket.send(json.dumps(payload))
                logger.debug("Sent registration message for %s parameters", len(payload["parameters"]))
                raw_ack = await asyncio.wait_for(websocket.recv(), timeout=10)
                token = registration_token_from_acknowledgement(json.loads(raw_ack))
                self.session_token = token
                logger.info("Control session registered successfully")
                link = CONTROL_URL_TEMPLATE.format(token=quote(token))
                active_parameters = {parameter.path: parameter for parameter in parameters}
                self.websocket_ready.set()
                self._queue_current_parameter_updates(parameters, values)
                self.root.after(0, lambda: self._session_ready(link))

                while not self.session_stop.is_set():
                    try:
                        event = self.parameter_update_queue.get_nowait()
                    except queue.Empty:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_parameter_changed(message, active_parameters, osc_client)
                    else:
                        event_type = event[0]
                        if event_type == "update_parameter":
                            _, path, value = event
                            if path in active_parameters:
                                await websocket.send(
                                    json.dumps(build_parameter_update(token, path, value))
                                )
                                logger.debug("Sent update_parameter for path=%r", path)
                        elif event_type == "add_parameters":
                            add_parameters = event[1]
                            try:
                                add_values = await asyncio.to_thread(
                                    self._read_parameter_values, add_parameters
                                )
                                await websocket.send(
                                    json.dumps(
                                        build_add_parameters(token, add_parameters, add_values)
                                    )
                                )
                            except (OSError, RuntimeError, ValueError, TypeError) as error:
                                logger.exception("Could not add parameters to active API session")
                                self.root.after(
                                    0,
                                    lambda error=error: self.status_var.set(
                                        f"Could not add parameter to active session: {error}"
                                    ),
                                )
                                continue
                            active_parameters.update(
                                {parameter.path: parameter for parameter in add_parameters}
                            )
                            self._queue_current_parameter_updates(add_parameters, add_values)
                            logger.info("Added %s parameters to active API session", len(add_parameters))
                        elif event_type == "remove_parameters":
                            paths = event[1]
                            await websocket.send(
                                json.dumps(build_remove_parameters(token, paths))
                            )
                            for path in paths:
                                active_parameters.pop(path, None)
                            logger.info("Removed %s parameters from active API session", len(paths))
                        elif event_type == "replace_parameters":
                            _, avatar_id, replacement_parameters = event
                            await websocket.send(json.dumps(build_clear_parameters(token)))
                            active_parameters.clear()
                            if replacement_parameters:
                                try:
                                    replacement_values = await asyncio.to_thread(
                                        self._read_parameter_values, replacement_parameters
                                    )
                                    await websocket.send(
                                        json.dumps(
                                            build_add_parameters(
                                                token, replacement_parameters, replacement_values
                                            )
                                        )
                                    )
                                except (OSError, RuntimeError, ValueError, TypeError) as error:
                                    logger.exception(
                                        "Could not load saved parameters into the active API session"
                                    )
                                    self.root.after(
                                        0,
                                        lambda error=error: self.status_var.set(
                                            f"Could not sync avatar parameters: {error}"
                                        ),
                                    )
                                    continue
                                active_parameters.update(
                                    {parameter.path: parameter for parameter in replacement_parameters}
                                )
                                self._queue_current_parameter_updates(
                                    replacement_parameters, replacement_values
                                )
                            logger.info(
                                "Replaced API parameter set for avatar %s with %s parameters",
                                avatar_id,
                                len(replacement_parameters),
                            )
        except Exception as error:
            logger.exception("Websocket session failed")
            if not self.session_stop.is_set():
                self.root.after(0, lambda error=error: self._session_failed(error))
        finally:
            self.websocket_ready.clear()
            self.session_token = None
            logger.info("Websocket session closed")
            self.root.after(0, self._session_closed)

    def _session_ready(self, link):
        self.share_link_var.set(link)
        self.copy_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.register_button.configure(state="normal", text="Session active")
        self.status_var.set("Session registered. Incoming websocket controls will be sent to VRChat.")

    def _session_failed(self, error):
        if isinstance(error, UnsupportedProtocolVersion):
            message = str(error)
            self.status_var.set(message)
            messagebox.showerror("Client update required", message)
            return
        self.status_var.set(f"Websocket session ended: {error}")

    def _session_closed(self):
        if not self.session_stop.is_set():
            self.register_button.configure(state="normal", text="Connect and create share link")

    def _handle_parameter_changed(self, raw_message, registered_parameters, osc_client):
        try:
            message = json.loads(raw_message)
            parameter, value = parse_parameter_changed(message, registered_parameters)
            try:
                osc_client.send_message(parameter.path, value)
            except Exception:
                logger.exception("Failed to send OSC control for parameter %r", parameter.name)
                self.root.after(
                    0,
                    lambda name=parameter.name: self.status_var.set(
                        f"Failed to send OSC update for {name}. See the log for details."
                    ),
                )
                return
            self.parameter_values[parameter.path] = value
            logger.debug(
                "Applied remote parameter change name=%r path=%r type=%s",
                parameter.name,
                parameter.path,
                parameter.type,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            logger.warning("Ignored invalid parameter_changed message: %s", error)
            self.root.after(
                0,
                lambda error=error: self.status_var.set(
                    f"Ignored invalid parameter update: {error}"
                ),
            )

    def copy_link(self):
        link = self.share_link_var.get()
        if link:
            self.root.clipboard_clear()
            self.root.clipboard_append(link)
            self.status_var.set("Share link copied to clipboard.")

    def open_link(self):
        link = self.share_link_var.get()
        if link:
            webbrowser.open(link)

    def close(self):
        logger.info("Shutting down desktop client")
        self.session_stop.set()
        self.discovery_stop.set()
        if self.service_browser:
            self.service_browser.cancel()
        if self.zeroconf:
            self.zeroconf.close()
        if self.osc_server:
            self.osc_server.shutdown()
            self.osc_server.server_close()
        self.root.destroy()


def main():
    configure_logging()
    logger.info("Starting VRChat OSC Control")
    root = Tk()
    set_application_icon(root)
    OscControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()