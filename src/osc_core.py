# Copyright (c) 2026 Ximmer's Creations <ximmer@ximmer.dev>.
# Licensed under the source-available proprietary terms in LICENSE.
# Personal noncommercial use and private modifications only; see LICENSE.

import ipaddress
import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


DEFAULT_OSC_SEND_HOST = "127.0.0.1"
DEFAULT_OSC_SEND_PORT = 9000
OSC_TYPE_NAMES = {"i": "int", "f": "float", "s": "string", "T": "bool", "F": "bool"}
SUPPORTED_TYPES = ("bool", "int", "float", "string")
OSC_TYPE_TAGS = {"bool": "T", "int": "i", "float": "f", "string": "s"}
logger = logging.getLogger(__name__)


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


def build_oscquery_host_info(name, osc_ip, osc_port):
    return {
        "NAME": name,
        "EXTENSIONS": {
            "ACCESS": True,
            "CLIPMODE": False,
            "RANGE": True,
            "TYPE": True,
            "VALUE": True,
        },
        "OSC_IP": osc_ip,
        "OSC_PORT": osc_port,
        "OSC_TRANSPORT": "UDP",
    }


def build_oscquery_tree(endpoints):
    root = {"FULL_PATH": "/", "CONTENTS": {}}
    for path, parameter_type in endpoints:
        type_tag = OSC_TYPE_TAGS.get(parameter_type)
        if (
            not type_tag
            or not path.startswith("/")
            or path == "/"
            or any(not part for part in path.strip("/").split("/"))
        ):
            continue
        node = root
        parts = path.strip("/").split("/")
        for index, part in enumerate(parts):
            contents = node.setdefault("CONTENTS", {})
            full_path = "/" + "/".join(parts[: index + 1])
            child = contents.setdefault(part, {"FULL_PATH": full_path})
            node = child
        node["TYPE"] = type_tag
        node["ACCESS"] = 2
    return root


def _load_shared_parameter_data(file_path):
    try:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Shared parameter file must contain an object keyed by avatar ID")
    return data


def load_shared_parameters(file_path, avatar_id):
    entry = _load_shared_parameter_data(file_path).get(avatar_id, [])
    if isinstance(entry, dict):
        entries = entry.get("parameters", [])
    else:
        entries = entry
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


def load_avatar_name(file_path, avatar_id):
    entry = _load_shared_parameter_data(file_path).get(avatar_id, [])
    if isinstance(entry, list):
        return ""
    if not isinstance(entry, dict):
        raise ValueError(f"Saved data for avatar {avatar_id!r} must be an object")
    name = entry.get("name", "")
    return name.strip() if isinstance(name, str) else ""


def save_shared_parameters(file_path, avatar_id, parameters, avatar_name=None):
    if not isinstance(avatar_id, str) or not avatar_id or avatar_id == "Waiting for VRChat":
        raise ValueError("Cannot save shared parameters without an active avatar ID")
    file_path = Path(file_path)
    data = _load_shared_parameter_data(file_path)
    previous_entry = data.get(avatar_id, {})
    previous_name = previous_entry.get("name", "") if isinstance(previous_entry, dict) else ""
    if avatar_name is not None and not isinstance(avatar_name, str):
        raise ValueError("Avatar name must be a string")
    name = previous_name if avatar_name is None else avatar_name.strip()
    data[avatar_id] = {
        "name": name if isinstance(name, str) else "",
        "parameters": [
            {"path": item.path, "name": item.name, "type": item.type}
            for item in parameters
        ],
    }
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = file_path.with_name(f"{file_path.name}.tmp")
    temporary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary_path.replace(file_path)


def build_set_avatar_name(name):
    if not isinstance(name, str):
        raise ValueError("Avatar name must be a string")
    return {"type": "set_avatar_name", "name": name}


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
    osc_host = decoded_properties.get("OSC_IP") or DEFAULT_OSC_SEND_HOST
    if osc_host in ("0.0.0.0", "::"):
        osc_host = ipv4_host or DEFAULT_OSC_SEND_HOST
    try:
        osc_port = int(decoded_properties.get("OSC_PORT", DEFAULT_OSC_SEND_PORT))
        if not 1 <= osc_port <= 65535:
            raise ValueError
    except ValueError:
        logger.warning(
            "Invalid OSC_PORT in OSCQuery service advertisement; using %s",
            DEFAULT_OSC_SEND_PORT,
        )
        osc_port = DEFAULT_OSC_SEND_PORT
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


def build_registration_payload(avatar_name, parameters, values, supporter_key=None):
    payload = {
        "type": "register",
        "version": 1,
        "avatar_name": avatar_name,
        "parameters": serialize_parameters(parameters, values),
    }
    if supporter_key:
        payload["supporter_key"] = supporter_key
    return payload


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


def registration_url_from_acknowledgement(acknowledgement):
    if not isinstance(acknowledgement, dict) or acknowledgement.get("type") != "registered":
        raise RuntimeError("Service did not acknowledge registration")
    url = acknowledgement.get("url")
    if url is None:
        return None
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("Service registration response included an invalid URL")
    return url
