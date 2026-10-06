# Copyright (c) 2026 Ximmer's Creations <ximmer@ximmer.dev>.
# Licensed under the source-available proprietary terms in LICENSE.
# Personal noncommercial use and private modifications only; see LICENSE.

import asyncio
import ipaddress
import json
import logging
import queue
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
import os
import threading
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, X, Y, StringVar, Tk, messagebox
from tkinter import ttk
from urllib.parse import unquote, urlsplit

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient
from osc_core import (
    DEFAULT_OSC_SEND_HOST,
    DEFAULT_OSC_SEND_PORT,
    OSC_TYPE_NAMES,
    SUPPORTED_TYPES,
    OscQueryConnection,
    Parameter,
    UnsupportedProtocolVersion,
    build_add_parameters,
    build_clear_parameters,
    build_oscquery_host_info,
    build_oscquery_connection,
    build_oscquery_tree,
    build_parameter_update,
    build_registration_payload,
    build_remove_parameters,
    build_set_avatar_name,
    coerce_control_value,
    extract_oscquery_values,
    filter_oscquery_parameters,
    flatten_oscquery_parameters,
    load_avatar_name,
    load_shared_parameters,
    oscquery_value,
    parse_parameter_changed,
    query_oscquery_value,
    registration_token_from_acknowledgement,
    registration_url_from_acknowledgement,
    save_shared_parameters,
    serialize_parameters,
)


APP_DATA_DIRECTORY = Path(os.getenv("LOCALAPPDATA", Path.home())) / "VRChatOSCControl"
SHARED_PARAMETERS_FILE = APP_DATA_DIRECTORY / "shared_parameters.json"
SETTINGS_FILE = APP_DATA_DIRECTORY / "settings.json"
DEFAULT_OSCQUERY_URL = "http://127.0.0.1:9001/"
OSCQUERY_SERVICE_TYPE = "_oscjson._tcp.local."
OSC_SERVICE_TYPE = "_osc._udp.local."
OSC_SERVICE_NAME = "VRChat OSC Control"
AVATAR_CHANGE_REFRESH_DELAY_MS = 500
APP_VERSION = "0.1"
DEFAULT_API_WS_URL = "wss://osccontrol.app/ws"
API_WS_503_RETRIES = 3
API_WS_RETRY_DELAY_SECONDS = 1

logger = logging.getLogger(__name__)


async def _connect_control_websocket(url):
    import websockets

    retries_remaining = API_WS_503_RETRIES
    while True:
        try:
            return await websockets.connect(url, open_timeout=10)
        except (
            websockets.exceptions.InvalidStatus,
            websockets.exceptions.ConnectionClosedError,
        ) as error:
            if isinstance(error, websockets.exceptions.InvalidStatus):
                retry_reason = error.response.status_code == 503
                retry_cause = "HTTP 503"
            else:
                close = error.rcvd
                retry_reason = (
                    close is not None
                    and close.code == 1013
                    and close.reason == "instance draining"
                )
                retry_cause = "1013 instance draining"
            if not retry_reason or retries_remaining == 0:
                raise
            retries_remaining -= 1
            retry_number = API_WS_503_RETRIES - retries_remaining
            logger.warning(
                "Control websocket connection received %s; retrying (%s/%s)",
                retry_cause,
                retry_number,
                API_WS_503_RETRIES,
            )
            await asyncio.sleep(API_WS_RETRY_DELAY_SECONDS)


def normalize_system_theme(theme):
    return "dark" if isinstance(theme, str) and theme.casefold() == "dark" else "light"


def apply_system_theme(root, theme):
    import sv_ttk

    mode = normalize_system_theme(theme)
    sv_ttk.set_theme(mode)
    if sys.platform == "win32":
        try:
            import pywinstyles

            pywinstyles.apply_style(root, mode)
        except Exception:
            logger.debug("Could not update Windows title bar theme", exc_info=True)
    logger.info("Applied %s system theme", mode)


def follow_system_theme(root):
    try:
        import darkdetect
    except ImportError:
        logger.exception("darkdetect is unavailable; using the default Tk theme")
        return

    apply_system_theme(root, darkdetect.theme())

    def on_theme_change(theme):
        try:
            root.after(0, lambda: apply_system_theme(root, theme))
        except Exception:
            logger.debug("Could not schedule system theme update", exc_info=True)

    threading.Thread(
        target=darkdetect.listener,
        args=(on_theme_change,),
        name="SystemThemeListener",
        daemon=True,
    ).start()


def default_settings(app_data_directory):
    app_data_directory = Path(app_data_directory)
    return {
        "oscquery_url": None,
        "osc_send_host": None,
        "osc_send_port": None,
        "osc_receive_host": "127.0.0.1",
        "osc_receive_port": 0,
        "api_websocket_url": DEFAULT_API_WS_URL,
        "log_level": "INFO",
        "log_file": str(app_data_directory / "app.log"),
    }


def load_settings(settings_file):
    settings_file = Path(settings_file)
    defaults = default_settings(settings_file.parent)
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(defaults, indent=4) + "\n", encoding="utf-8")
        return defaults
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read settings from {settings_file}; using defaults: {error}")
        return defaults

    if not isinstance(settings, dict):
        print(f"Settings in {settings_file} must be a JSON object; using defaults")
        return defaults

    merged = {**defaults, **settings}
    for key in ("oscquery_url", "osc_send_host"):
        if merged[key] is not None and not isinstance(merged[key], str):
            merged[key] = defaults[key]
    for key in ("osc_send_port", "osc_receive_port"):
        value = merged[key]
        if value is not None or key == "osc_receive_port":
            if isinstance(value, bool):
                value = defaults[key]
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = defaults[key]
            minimum_port = 0 if key == "osc_receive_port" else 1
            if isinstance(value, bool) or not minimum_port <= value <= 65535:
                value = defaults[key]
        merged[key] = value
    if merged["osc_receive_port"] == 9001:
        merged["osc_receive_port"] = defaults["osc_receive_port"]
    for key in ("osc_receive_host", "api_websocket_url", "log_level", "log_file"):
        if not isinstance(merged[key], str) or not merged[key].strip():
            merged[key] = defaults[key]
    merged["log_level"] = merged["log_level"].upper()
    if not isinstance(getattr(logging, merged["log_level"], None), int):
        merged["log_level"] = defaults["log_level"]
    return merged


def configure_logging(settings):
    configured_level = settings["log_level"]
    level = getattr(logging, configured_level, logging.INFO)
    log_path = Path(settings["log_file"]).expanduser()
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


def set_windows_app_user_model_id():
    if sys.platform != "win32":
        return

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Ximmer.VRChatOSCControl"
        )
    except Exception:
        logger.exception("Could not set Windows application identity")


class OscControlApp:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.root.title(f"VRChat OSC Control v{APP_VERSION}")
        self.root.geometry("780x650")
        self.root.minsize(660, 900)

        self.parameters = {}
        self.parameter_values = {}
        self.parameter_update_queue = queue.Queue()
        self.websocket_ready = threading.Event()
        self.available_parameters = []
        self.active_avatar = "Waiting for VRChat"
        self.oscquery_url = settings["oscquery_url"] or DEFAULT_OSCQUERY_URL
        self.osc_send_host = settings["osc_send_host"] or DEFAULT_OSC_SEND_HOST
        self.osc_send_port = settings["osc_send_port"] or DEFAULT_OSC_SEND_PORT
        self.osc_receive_host = settings["osc_receive_host"]
        self.osc_receive_port = settings["osc_receive_port"]
        self.session_stop = threading.Event()
        self.session_token = None
        self.discovery_stop = threading.Event()
        self.session_thread = None
        self.osc_server = None
        self.oscquery_http_server = None
        self.oscquery_http_thread = None
        self.osc_service_infos = []
        self.osc_advertised_host = None
        self.oscquery_endpoints_lock = threading.Lock()
        self.oscquery_endpoints = (("/avatar/change", "string"),)
        self.zeroconf = None
        self.service_browser = None

        self.avatar_var = StringVar(value=self.active_avatar)
        self.avatar_name_var = StringVar()
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

        avatar_name_row = ttk.Frame(outer)
        avatar_name_row.pack(fill=X, pady=(0, 12))
        ttk.Label(avatar_name_row, text="Avatar name", width=15).pack(side=LEFT)
        ttk.Entry(avatar_name_row, textvariable=self.avatar_name_var).pack(
            side=LEFT, fill=X, expand=True
        )
        ttk.Button(avatar_name_row, text="Save name", command=self.save_avatar_name).pack(
            side=RIGHT, padx=(8, 0)
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
        logger.info("Starting VRChat OSC listener on %s:%s", self.osc_receive_host, self.osc_receive_port)
        dispatcher = Dispatcher()
        dispatcher.map("/avatar/change", self._on_avatar_change)
        dispatcher.set_default_handler(self._on_osc_parameter)
        try:
            self.osc_server = ThreadingOSCUDPServer((self.osc_receive_host, self.osc_receive_port), dispatcher)
            logger.debug("OSC server created on port %s", self.osc_server.server_address[1])

        except OSError as error:
            logger.exception("Could not start VRChat OSC listener")
            self.status_var.set(f"Could not listen for VRChat avatar changes: {error}")
            return
        self.osc_receive_port = self.osc_server.server_address[1]
        self.osc_advertised_host = self._get_osc_advertised_host()
        threading.Thread(target=self.osc_server.serve_forever, daemon=True).start()
        logger.info("VRChat OSC listener started on %s:%s", self.osc_advertised_host, self.osc_receive_port)

    def _get_osc_advertised_host(self):
        bound_host = self.osc_server.server_address[0]
        try:
            address = ipaddress.ip_address(bound_host)
        except ValueError:
            address = ipaddress.ip_address(socket.gethostbyname(bound_host))
        if not address.is_unspecified:
            return str(address)
        try:
            results = socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
            )
        except OSError:
            logger.warning("Could not resolve local network addresses for OSC advertisement")
            results = ()
        for result in results:
            candidate = ipaddress.ip_address(result[4][0])
            if not candidate.is_loopback and not candidate.is_unspecified:
                return str(candidate)
        return "127.0.0.1"

    def _start_oscquery_http_server(self):
        app = self

        class OSCQueryRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                request = urlsplit(self.path)
                if "HOST_INFO" in request.query.split("&"):
                    response = build_oscquery_host_info(
                        OSC_SERVICE_NAME,
                        app.osc_advertised_host,
                        app.osc_receive_port,
                    )
                else:
                    with app.oscquery_endpoints_lock:
                        endpoints = app.oscquery_endpoints
                    response = build_oscquery_tree(endpoints)
                    node = response
                    request_path = unquote(request.path)
                    if request_path != "/":
                        for part in request_path.strip("/").split("/"):
                            node = node.get("CONTENTS", {}).get(part)
                            if node is None:
                                self.send_error(404, "OSC path not found")
                                return
                        response = node
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format_string, *args):
                logger.debug("OSCQuery HTTP request: " + format_string, *args)

        self.oscquery_http_server = ThreadingHTTPServer(
            (self.osc_receive_host, 0), OSCQueryRequestHandler
        )
        self.oscquery_http_server.daemon_threads = True
        self.oscquery_http_thread = threading.Thread(
            target=self.oscquery_http_server.serve_forever, daemon=True
        )
        self.oscquery_http_thread.start()

    def _register_osc_services(self):
        if self.osc_server is None:
            return

        from zeroconf import ServiceInfo

        ip_address = ipaddress.ip_address(self.osc_advertised_host).packed
        server_name = f"osc-control-{os.getpid()}.local."
        service_name = f"{OSC_SERVICE_NAME}.{OSC_SERVICE_TYPE}"
        osc_service = ServiceInfo(
            OSC_SERVICE_TYPE,
            service_name,
            addresses=[ip_address],
            port=self.osc_receive_port,
            server=server_name,
        )
        try:
            self.zeroconf.register_service(osc_service, allow_name_change=True)
            self.osc_service_infos.append(osc_service)
            logger.info(
                "Advertised OSC receiver on %s:%s",
                self.osc_advertised_host,
                self.osc_receive_port,
            )
        except Exception as error:
            logger.exception("Could not advertise the OSC UDP receiver")
            self.root.after(
                0,
                lambda error=error: self.status_var.set(
                    f"OSC listener is active, but receiver advertising failed: {error}"
                ),
            )

        try:
            self._start_oscquery_http_server()
            oscquery_service = ServiceInfo(
                OSCQUERY_SERVICE_TYPE,
                f"{OSC_SERVICE_NAME}.{OSCQUERY_SERVICE_TYPE}",
                addresses=[ip_address],
                port=self.oscquery_http_server.server_port,
                server=server_name,
            )
            self.zeroconf.register_service(oscquery_service, allow_name_change=True)
            self.osc_service_infos.append(oscquery_service)
            logger.info(
                "Advertised OSCQuery service on %s:%s",
                self.osc_advertised_host,
                self.oscquery_http_server.server_port,
            )
        except Exception as error:
            logger.exception("Could not advertise the OSCQuery HTTP service")
            self.root.after(
                0,
                lambda error=error: self.status_var.set(
                    f"OSC listener is active, but OSCQuery advertising failed: {error}"
                ),
            )

    def _update_oscquery_endpoints(self):
        endpoints = [("/avatar/change", "string")]
        endpoints.extend((parameter.path, parameter.type) for parameter in self.parameters.values())
        with self.oscquery_endpoints_lock:
            self.oscquery_endpoints = tuple(endpoints)

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
                    if name.startswith(f"{OSC_SERVICE_NAME}."):
                        return
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
            self._register_osc_services()
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
        if not self.settings["oscquery_url"]:
            self.oscquery_url = connection.url
        if not self.settings["osc_send_host"]:
            self.osc_send_host = connection.osc_host
        if not self.settings["osc_send_port"]:
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
            avatar_name = load_avatar_name(SHARED_PARAMETERS_FILE, avatar_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not load shared parameters for avatar %s", avatar_id)
            self.status_var.set(f"Could not load saved parameters: {error}")
            parameters = []
            avatar_name = ""
        self.avatar_name_var.set(avatar_name)
        self._replace_shared_parameters(parameters)
        if self.session_thread and self.session_thread.is_alive():
            if self.websocket_ready.is_set():
                logger.info("Replacing active API parameters for avatar %s", avatar_id)
                self.parameter_update_queue.put(
                    ("replace_parameters", avatar_id, list(parameters), avatar_name or avatar_id)
                )
                self.status_var.set("Synchronizing saved parameters with the active session...")
            else:
                logger.info("Stopping session setup because the avatar changed")
                self.session_stop.set()
        if parameters:
            self.status_var.set(f"Loaded {len(parameters)} saved parameters for this avatar.")
        else:
            self.status_var.set("No saved shared parameters for this avatar.")
        self.root.after(AVATAR_CHANGE_REFRESH_DELAY_MS, self.refresh_parameters)

    def _replace_shared_parameters(self, parameters):
        self.parameters = {parameter.path: parameter for parameter in parameters}
        self._update_oscquery_endpoints()
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
        self._update_oscquery_endpoints()
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
                SHARED_PARAMETERS_FILE,
                self.active_avatar,
                list(self.parameters.values()),
                self.avatar_name_var.get(),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not save shared parameters for avatar %s", self.active_avatar)
            self.status_var.set(f"Could not save shared parameters: {error}")

    def save_avatar_name(self):
        if not self.active_avatar or self.active_avatar == "Waiting for VRChat":
            self.status_var.set("Wait for VRChat to detect an avatar before saving its name.")
            return
        avatar_name = self.avatar_name_var.get().strip()
        self.avatar_name_var.set(avatar_name)
        try:
            save_shared_parameters(
                SHARED_PARAMETERS_FILE,
                self.active_avatar,
                list(self.parameters.values()),
                avatar_name,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.exception("Could not save avatar name for %s", self.active_avatar)
            self.status_var.set(f"Could not save avatar name: {error}")
            return
        logger.info("Saved avatar name %r for %s", avatar_name, self.active_avatar)
        if self.websocket_ready.is_set():
            self.parameter_update_queue.put(
                ("set_avatar_name", avatar_name or self.active_avatar)
            )
        self.status_var.set("Avatar name saved.")

    def remove_selected(self):
        paths = list(self.registered_list.selection())
        for path in paths:
            self.parameters.pop(path, None)
            self.registered_list.delete(path)
            logger.info("Removed shared parameter path=%r", path)
        if paths:
            self._update_oscquery_endpoints()
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
                SHARED_PARAMETERS_FILE,
                self.active_avatar,
                list(self.parameters.values()),
                self.avatar_name_var.get(),
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
            if values.get(parameter.path) is None:
                values.pop(parameter.path, None)
                continue
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
                continue
            values[parameter.path] = coerce_control_value(value, parameter.type)
        return values

    def _report_ignored_parameters(self, parameters, avatar_id=None):
        if not parameters:
            return
        parameter_details = ", ".join(
            f"{parameter.name} ({parameter.path})" for parameter in parameters
        )
        logger.warning(
            "Ignoring %s unavailable avatar parameter(s)%s: %s",
            len(parameters),
            f" for avatar {avatar_id}" if avatar_id else "",
            parameter_details,
        )

        def update_ui():
            if avatar_id and self.active_avatar != avatar_id:
                return
            removed = False
            for parameter in parameters:
                if self.parameters.get(parameter.path) == parameter:
                    self.parameters.pop(parameter.path, None)
                    self.registered_list.delete(parameter.path)
                    removed = True
            if removed:
                self._update_oscquery_endpoints()
                self._persist_current_shared_parameters()
            self.status_var.set(f"Ignored unavailable avatar parameters: {parameter_details}")

        self.root.after(0, update_ui)

    def _queue_current_parameter_updates(self, parameters, initial_values):
        for parameter in parameters:
            current_value = self.parameter_values.get(parameter.path)
            if current_value is not None and current_value != initial_values[parameter.path]:
                self.parameter_update_queue.put(
                    ("update_parameter", parameter.path, current_value)
                )

    async def _websocket_session(self, parameters, fallback_avatar_id):
        logger.debug("Creating osc client on %s:%s", self.osc_send_host, self.osc_send_port)
        osc_client = SimpleUDPClient(self.osc_send_host, self.osc_send_port)
        try:
            avatar_id, values = await asyncio.to_thread(self._read_registration_state, parameters, fallback_avatar_id)
            ignored_parameters = [parameter for parameter in parameters if parameter.path not in values]
            self._report_ignored_parameters(ignored_parameters, avatar_id)
            parameters = [parameter for parameter in parameters if parameter.path in values]
            avatar_name = load_avatar_name(SHARED_PARAMETERS_FILE, avatar_id)
            logger.info("Registering %s parameters for avatar %s", len(parameters), avatar_id)
            logger.info("Connecting to control websocket")
            websocket_connection = await _connect_control_websocket(
                self.settings["api_websocket_url"]
            )
            async with websocket_connection as websocket:
                payload = build_registration_payload(avatar_name, parameters, values)
                logger.debug("Sending registration message for %s parameters", len(payload["parameters"]))
                await websocket.send(json.dumps(payload))
                raw_ack = await asyncio.wait_for(websocket.recv(), timeout=10)
                acknowledgement = json.loads(raw_ack)
                logger.debug("Received acknowledgement: %s", acknowledgement)
                token = registration_token_from_acknowledgement(acknowledgement)
                link = registration_url_from_acknowledgement(acknowledgement)
                self.session_token = token
                logger.info("Control session registered successfully")
                active_parameters = {parameter.path: parameter for parameter in parameters}
                self.websocket_ready.set()
                self._queue_current_parameter_updates(parameters, values)
                self.root.after(
                    0, lambda: self._session_ready(link, ignored_parameters)
                )

                while not self.session_stop.is_set():
                    try:
                        event = self.parameter_update_queue.get_nowait()
                    except queue.Empty:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_websocket_command(message, active_parameters, osc_client)
                    else:
                        event_type = event[0]

                        match event_type:

                            case "update_parameter":
                                _, path, value = event
                                if path in active_parameters:
                                    logger.debug("Sending update_parameter for path=%r", path)
                                    await websocket.send(json.dumps(build_parameter_update(token, path, value)))

                            case "add_parameters":
                                add_parameters = event[1]
                                try:
                                    add_values = await asyncio.to_thread(self._read_parameter_values, add_parameters)
                                    ignored_parameters = [parameter for parameter in add_parameters if parameter.path not in add_values]
                                    self._report_ignored_parameters(ignored_parameters)
                                    add_parameters = [parameter for parameter in add_parameters if parameter.path in add_values]
                                    if not add_parameters:
                                        continue
                                    await websocket.send(json.dumps(build_add_parameters(token, add_parameters, add_values)))
                                except (OSError, RuntimeError, ValueError, TypeError) as error:
                                    logger.exception("Could not add parameters to active API session")
                                    self.root.after(0, lambda error=error: self.status_var.set(f"Could not add parameter to active session: {error}"))
                                    continue
                                active_parameters.update({parameter.path: parameter for parameter in add_parameters})
                                self._queue_current_parameter_updates(add_parameters, add_values)
                                logger.info("Added %s parameters to active API session", len(add_parameters))

                            case "remove_parameters":
                                paths = event[1]
                                await websocket.send(json.dumps(build_remove_parameters(token, paths)))
                                for path in paths:
                                    active_parameters.pop(path, None)
                                logger.info("Removed %s parameters from active API session", len(paths))

                            case "set_avatar_name":
                                await websocket.send(json.dumps(build_set_avatar_name(event[1])))
                                logger.info("Updated active avatar name")

                            case "replace_parameters":
                                _, avatar_id, replacement_parameters, avatar_name = event
                                await websocket.send(json.dumps(build_clear_parameters(token)))
                                await websocket.send(json.dumps(build_set_avatar_name(avatar_name)))
                                active_parameters.clear()
                                if replacement_parameters:
                                    try:
                                        replacement_values = await asyncio.to_thread(self._read_parameter_values, replacement_parameters)
                                        ignored_parameters = [parameter for parameter in replacement_parameters if parameter.path not in replacement_values]
                                        self._report_ignored_parameters(ignored_parameters, avatar_id)
                                        replacement_parameters = [parameter for parameter in replacement_parameters if parameter.path in replacement_values]
                                        if replacement_parameters:
                                            await websocket.send(json.dumps(build_add_parameters(token, replacement_parameters, replacement_values)))
                                    except (OSError, RuntimeError, ValueError, TypeError) as error:
                                        logger.exception("Could not load saved parameters into the active API session")
                                        self.root.after(0, lambda error=error: self.status_var.set(f"Could not sync avatar parameters: {error}"))
                                        continue
                                    active_parameters.update({parameter.path: parameter for parameter in replacement_parameters})
                                    self._queue_current_parameter_updates(replacement_parameters, replacement_values)
                                logger.info("Replaced API parameter set for avatar %s with %s parameters", avatar_id, len(replacement_parameters))
        except Exception as error:
            logger.exception("Websocket session failed")
            if not self.session_stop.is_set():
                self.root.after(0, lambda error=error: self._session_failed(error))
        finally:
            self.websocket_ready.clear()
            self.session_token = None
            logger.info("Websocket session closed")
            self.root.after(0, self._session_closed)

    def _session_ready(self, link, ignored_parameters=()):
        self.share_link_var.set(link)
        self.copy_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.register_button.configure(
            state="normal",
            text="End Session",
            command=self.end_session,
        )
        if ignored_parameters:
            paths = ", ".join(parameter.path for parameter in ignored_parameters)
            self.status_var.set(
                "Session registered; ignored unavailable avatar parameter paths: "
                f"{paths}"
            )
        else:
            self.status_var.set(
                "Session registered. Incoming websocket controls will be sent to VRChat."
            )

    def _session_failed(self, error):
        if isinstance(error, UnsupportedProtocolVersion):
            message = str(error)
            self.status_var.set(message)
            messagebox.showerror("Client update required", message)
            return
        self.status_var.set(f"Websocket session ended: {error}")

    def end_session(self):
        if self.session_stop.is_set():
            return
        logger.info("Ending active websocket session")
        self.session_stop.set()
        self.websocket_ready.clear()
        self.register_button.configure(state="disabled", text="Ending session...")
        self.copy_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.status_var.set("Ending control session...")

    def _session_closed(self):
        ended_by_user = self.session_stop.is_set()
        self.session_stop.clear()
        self.register_button.configure(
            state="normal",
            text="Connect and create share link",
            command=self.register_session,
        )
        if ended_by_user:
            self.share_link_var.set("")
            self.copy_button.configure(state="disabled")
            self.open_button.configure(state="disabled")
            self.status_var.set("Control session ended.")

    def _handle_websocket_command(self, raw_message, registered_parameters, osc_client):
        try:
            message = json.loads(raw_message)

            if not isinstance(message, dict):
                logger.debug("Received non-dict message from API")
                return

            command_type = message.get("type")

            match command_type:
                case "avatar_name_updated":
                    logger.debug("Received avatar name update from API. name=%r" % message.get("name"))
                    return

                case "parameters_cleared":
                    logger.debug("Received parameters cleared update from API. count:%d" % message.get("count"))
                    return

                case "parameters_added":
                    logger.debug("Received parameters added update from API. count:%d" % message.get("count"))
                    return

                case "parameters_removed":
                    logger.debug("Received parameters removed update from API. count:%d" % message.get("count"))
                    return

                case "parameter_changed":
                    parameter, value = parse_parameter_changed(message, registered_parameters)
                    try:
                        osc_client.send_message(parameter.path, value)
                    except Exception:
                        logger.exception("Failed to send OSC control for parameter %r", parameter.name)
                        self.root.after(0, lambda name=parameter.name: self.status_var.set(f"Failed to send OSC update for {name}. See the log for details."))
                        return
                    self.parameter_values[parameter.path] = value
                    logger.debug("Applied remote parameter change name=%r path=%r type=%s", parameter.name, parameter.path, parameter.type)

                case _:
                    logger.debug("Received unknown command type from API: %r message: %r", command_type, raw_message)
                    return

        except (json.JSONDecodeError, ValueError, TypeError) as error:
            logger.warning("Ignored invalid parameter_changed message: %s [raw_message=%s]", error, raw_message)
            self.root.after(0, lambda error=error: self.status_var.set(f"Ignored invalid parameter update: {error}"))

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
            for service_info in self.osc_service_infos:
                try:
                    self.zeroconf.unregister_service(service_info)
                except Exception:
                    logger.exception(
                        "Could not withdraw OSC service advertisement %s",
                        service_info.name,
                    )
            self.zeroconf.close()
        if self.oscquery_http_server:
            self.oscquery_http_server.shutdown()
            self.oscquery_http_server.server_close()
        if self.osc_server:
            self.osc_server.shutdown()
            self.osc_server.server_close()
        self.root.destroy()


def main():
    settings = load_settings(SETTINGS_FILE)
    configure_logging(settings)
    logger.info("Starting VRChat OSC Control")
    set_windows_app_user_model_id()
    root = Tk()
    set_application_icon(root)
    follow_system_theme(root)
    OscControlApp(root, settings)
    root.mainloop()


if __name__ == "__main__":
    main()