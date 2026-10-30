#!/usr/bin/env python3
"""Find ONVIF IP cameras on the local network and add them to Frigate.

Standard library only, so it runs on a stock Ubuntu server.

How it works:
  1. WS-Discovery: a multicast probe (UDP 239.255.255.250:3702) that ONVIF
     cameras on the same LAN answer with their device-service address.
     Cameras on other subnets can be given directly with --host.
  2. For each camera, the logins from the credentials file (.cam, one
     ``user:pass`` per line) are tried until one works.
  3. The camera's stream profiles and RTSP addresses are read over ONVIF.
     The highest-resolution stream is used for recording, the lowest for
     detection.
  4. With --apply, cameras that are not in the Frigate config yet (matched by
     IP address) are added under ``cameras:``; the file is backed up first.
     Existing cameras and the rest of the file are left untouched.

Cameras need ONVIF turned on (on Reolink: Settings > Network > Advanced >
Server settings > ONVIF).

Usage:
  camera_discovery.py                         # list cameras, change nothing
  camera_discovery.py --apply                 # add new cameras to Frigate
  camera_discovery.py --host 192.168.1.50     # skip discovery, ask this camera
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
ONVIF_PORTS = (80, 8000, 8080, 2020, 8899)  # common device-service ports
DEFAULT_CONFIG = "/opt/edge-server/frigate/config.yml"
CREDENTIAL_LOCATIONS = (".cam", "/opt/edge-server/.cam")

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)

# Probe for both device types: some cameras only answer one of them.
PROBE_TYPES = (
    ('xmlns:dn="http://www.onvif.org/ver10/network/wsdl"', "dn:NetworkVideoTransmitter"),
    (f'xmlns:dn="{NS_TDS}"', "dn:Device"),
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Credential:
    user: str
    password: str


@dataclass
class Stream:
    token: str
    name: str = ""
    uri: str = ""
    width: int = 0
    height: int = 0
    encoding: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class Camera:
    host: str
    xaddr: str
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    streams: list[Stream] = field(default_factory=list)
    credential: Credential | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.credential is not None and any(s.uri for s in self.streams)


class OnvifError(Exception):
    """The camera answered with an error or not at all."""


class AuthError(OnvifError):
    """The camera rejected the login."""


# ---------------------------------------------------------------------------
# Credentials file
# ---------------------------------------------------------------------------


def load_credentials(path: Path) -> list[Credential]:
    """Read ``user:pass`` lines (``#`` comments and blank lines ignored)."""
    creds: list[Credential] = []
    for n, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        user, sep, password = line.partition(":")
        if not sep or not user:
            raise ValueError(f"{path}:{n}: expected user:password")
        creds.append(Credential(user, password))
    if not creds:
        raise ValueError(f"{path}: no user:password lines")
    return creds


def find_credentials_file(explicit: str | None) -> Path | None:
    candidates = [explicit] if explicit else []
    if os.environ.get("CAM_CREDENTIALS_FILE"):
        candidates.append(os.environ["CAM_CREDENTIALS_FILE"])
    candidates.append(str(Path(__file__).resolve().parent.parent / ".cam"))
    candidates.extend(CREDENTIAL_LOCATIONS)
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def credentials_file_warning(path: Path) -> str | None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"{path} is readable by other users (mode {mode:o}); run: chmod 600 {path}"
    return None


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, *path: str) -> ET.Element | None:
    """Find a descendant by local tag names, ignoring namespaces."""
    current: ET.Element | None = el
    for name in path:
        if current is None:
            return None
        current = next((c for c in current.iter() if _local(c.tag) == name), None)
    return current


def _text(el: ET.Element | None, *path: str) -> str:
    found = _find(el, *path) if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# WS-Discovery
# ---------------------------------------------------------------------------


def probe_message(types_ns: str, types: str, message_id: str | None = None) -> bytes:
    message_id = message_id or f"uuid:{uuid.uuid4()}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<e:Envelope xmlns:e="{NS_SOAP}"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        f" {types_ns}>"
        "<e:Header>"
        f"<w:MessageID>{message_id}</w:MessageID>"
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">'
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header>"
        f"<e:Body><d:Probe><d:Types>{types}</d:Types></d:Probe></e:Body>"
        "</e:Envelope>"
    ).encode()


def parse_probe_matches(data: bytes) -> list[str]:
    """Return the device-service URLs (XAddrs) in a ProbeMatches reply."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    urls: list[str] = []
    for match in (e for e in root.iter() if _local(e.tag) == "ProbeMatch"):
        xaddrs = _text(match, "XAddrs").split()
        # Prefer an IPv4 http address; cameras often also list IPv6 ones
        ipv4 = [u for u in xaddrs if re.match(r"https?://\d+\.\d+\.\d+\.\d+", u)]
        chosen = (ipv4 or xaddrs)[:1]
        urls.extend(chosen)
    return urls


def ws_discover(
    timeout: float = 3.0,
    target: tuple[str, int] = WS_DISCOVERY_ADDR,
    interface_ip: str | None = None,
) -> list[str]:
    """Multicast a WS-Discovery probe and collect device-service URLs."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if interface_ip:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip)
            )
        sock.bind(("", 0))
        for _ in range(2):  # UDP is lossy: send each probe twice
            for types_ns, types in PROBE_TYPES:
                sock.sendto(probe_message(types_ns, types), target)

        found: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(65535)
            except TimeoutError:
                break
            for url in parse_probe_matches(data):
                host = urllib.parse.urlparse(url).hostname or url
                found.setdefault(host, url)
        return list(found.values())
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# ONVIF SOAP client
# ---------------------------------------------------------------------------


def _rewrite_host(url: str, host: str) -> str:
    """Point a URL returned by the camera at the address we reached it on.

    Cameras behind NAT or with stale settings often report an internal or
    wrong IP in the addresses they hand out; the port and path are kept.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return url
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


class OnvifClient:
    def __init__(self, xaddr: str, credential: Credential | None = None, timeout: float = 5.0):
        self.xaddr = xaddr
        self.host = urllib.parse.urlparse(xaddr).hostname or ""
        self.credential = credential
        self.timeout = timeout
        self.time_offset = 0.0  # camera clock minus our clock, in seconds

        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
        if credential:
            # Some cameras want HTTP auth in addition to (or instead of) the
            # WS-Security header; urllib only uses these after a 401.
            # Registered for the whole camera, not just the device-service
            # path: the media service usually lives at a different path.
            parts = urllib.parse.urlsplit(xaddr)
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(
                None, f"{parts.scheme}://{parts.netloc}/", credential.user, credential.password
            )
            handlers += [
                urllib.request.HTTPDigestAuthHandler(mgr),
                urllib.request.HTTPBasicAuthHandler(mgr),
            ]
        self._opener = urllib.request.build_opener(*handlers)

    def _security_header(self) -> str:
        if not self.credential:
            return ""
        nonce = os.urandom(16)
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.time_offset)
        created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.credential.password.encode()).digest()
        ).decode()
        return (
            f'<Security s:mustUnderstand="1" xmlns="{NS_WSSE}"><UsernameToken>'
            f"<Username>{escape(self.credential.user)}</Username>"
            f'<Password Type="{PASSWORD_DIGEST}">{digest}</Password>'
            f'<Nonce EncodingType="{BASE64_BINARY}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{NS_WSU}">{created}</Created>'
            "</UsernameToken></Security>"
        )

    def call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        header = self._security_header() if auth else ""
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS_SOAP}" xmlns:tds="{NS_TDS}"'
            f' xmlns:trt="{NS_TRT}" xmlns:tt="{NS_TT}">'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        ).encode()
        request = urllib.request.Request(
            url,
            data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            payload = e.read() or b""
            fault = self._fault(payload)
            if e.code == 401 or _is_auth_fault(fault):
                raise AuthError(fault or f"HTTP {e.code}") from None
            raise OnvifError(fault or f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise OnvifError(f"no response from {url}: {reason}") from None

        fault = self._fault(payload)
        if fault:
            raise AuthError(fault) if _is_auth_fault(fault) else OnvifError(fault)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as e:
            raise OnvifError(f"invalid response from {url}: {e}") from None
        body_el = _find(root, "Body")
        if body_el is None:
            raise OnvifError(f"invalid response from {url}: no SOAP body")
        return body_el

    @staticmethod
    def _fault(payload: bytes) -> str:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return ""
        fault = _find(root, "Fault")
        if fault is None:
            return ""
        parts = [(e.text or "").strip() for e in fault.iter() if _local(e.tag) in ("Value", "Text")]
        return " / ".join(p for p in parts if p) or "SOAP fault"

    def sync_time(self) -> None:
        """Match the camera's clock: digest logins are rejected on clock skew."""
        try:
            body = self.call(self.xaddr, "<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError:
            return
        utc = _find(body, "UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                _int(_text(utc, "Date", "Year")),
                _int(_text(utc, "Date", "Month")),
                _int(_text(utc, "Date", "Day")),
                _int(_text(utc, "Time", "Hour")),
                _int(_text(utc, "Time", "Minute")),
                _int(_text(utc, "Time", "Second")),
                tzinfo=dt.timezone.utc,
            )
        except ValueError:
            return
        self.time_offset = (cam - dt.datetime.now(dt.timezone.utc)).total_seconds()

    def device_info(self) -> dict[str, str]:
        body = self.call(self.xaddr, "<tds:GetDeviceInformation/>")
        return {
            "manufacturer": _text(body, "Manufacturer"),
            "model": _text(body, "Model"),
            "serial": _text(body, "SerialNumber"),
        }

    def media_url(self) -> str:
        try:
            body = self.call(
                self.xaddr,
                "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
            )
            url = _text(body, "Media", "XAddr")
        except AuthError:
            raise
        except OnvifError:
            url = ""
        return _rewrite_host(url, self.host) if url else self.xaddr

    def profiles(self, media_url: str) -> list[Stream]:
        body = self.call(media_url, "<trt:GetProfiles/>")
        streams = []
        for p in (e for e in body.iter() if _local(e.tag) == "Profiles"):
            enc = _find(p, "VideoEncoderConfiguration")
            streams.append(
                Stream(
                    token=p.get("token", ""),
                    name=_text(p, "Name"),
                    width=_int(_text(enc, "Resolution", "Width")),
                    height=_int(_text(enc, "Resolution", "Height")),
                    encoding=_text(enc, "Encoding"),
                )
            )
        return [s for s in streams if s.token]

    def stream_uri(self, media_url: str, token: str) -> str:
        body = self.call(
            media_url,
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>",
        )
        uri = _text(body, "MediaUri", "Uri")
        return _rewrite_host(uri, self.host) if uri else ""


def _is_auth_fault(fault: str) -> bool:
    lowered = fault.lower()
    return any(k in lowered for k in ("notauthorized", "not authorized", "unauthorized"))


# ---------------------------------------------------------------------------
# Probing cameras
# ---------------------------------------------------------------------------


def probe_camera(xaddr: str, credentials: list[Credential], timeout: float = 5.0) -> Camera:
    """Log in with the first working credential and read the camera's streams."""
    cam = Camera(host=urllib.parse.urlparse(xaddr).hostname or xaddr, xaddr=xaddr)
    last_error = "no credentials to try"
    for cred in credentials:
        client = OnvifClient(xaddr, cred, timeout)
        client.sync_time()
        try:
            info = client.device_info()
            media = client.media_url()
            streams = client.profiles(media)
            for s in streams:
                try:
                    s.uri = client.stream_uri(media, s.token)
                except AuthError:
                    raise
                except OnvifError:
                    s.uri = ""
        except AuthError:
            last_error = "login rejected for every user in the credentials file"
            continue
        except OnvifError as e:
            cam.error = str(e)
            return cam
        cam.credential = cred
        cam.manufacturer = info["manufacturer"]
        cam.model = info["model"]
        cam.serial = info["serial"]
        cam.streams = [s for s in streams if s.uri]
        if not cam.streams:
            cam.error = "camera reported no RTSP streams"
        return cam
    cam.error = last_error
    return cam


def resolve_host(host: str, timeout: float = 3.0) -> str | None:
    """Turn ``IP``, ``IP:PORT`` or a URL into a responding device-service URL."""
    if "://" in host:
        return host
    if ":" in host:
        candidates = [f"http://{host}/onvif/device_service"]
    else:
        candidates = [
            f"http://{host}{'' if p == 80 else f':{p}'}/onvif/device_service" for p in ONVIF_PORTS
        ]
    for url in candidates:
        try:
            OnvifClient(url, timeout=timeout).call(url, "<tds:GetSystemDateAndTime/>", auth=False)
            return url
        except AuthError:
            return url  # answered, just wants a login
        except OnvifError:
            continue
    return None


# ---------------------------------------------------------------------------
# Frigate config
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def camera_name(cam: Camera, taken: set[str]) -> str:
    base = _slug(cam.manufacturer) or "camera"
    suffix = cam.host.rsplit(".", 1)[-1] if re.fullmatch(r"[\d.]+", cam.host) else _slug(cam.host)
    name = f"{base}_{suffix}"
    n = 2
    while name in taken:
        name = f"{base}_{suffix}_{n}"
        n += 1
    taken.add(name)
    return name


def choose_streams(streams: list[Stream]) -> tuple[Stream, Stream]:
    """Highest resolution for recording, lowest for detection."""
    ordered = sorted(streams, key=lambda s: s.pixels)
    return ordered[-1], ordered[0]


def with_credentials(uri: str, cred: Credential | None, mask: bool = False) -> str:
    """Put the login into an RTSP URL the way Frigate expects it.

    The password is written as-is: Frigate URL-encodes it itself before
    handing the URL to ffmpeg, so encoding it here as well would turn
    ``#`` into ``%2523`` and the camera would reject the login.
    Braces are doubled because Frigate runs input paths through
    ``str.format`` to substitute ``{FRIGATE_*}`` variables.
    """
    if cred is None:
        return uri
    parts = urllib.parse.urlsplit(uri)
    host = parts.hostname or ""
    hostport = host if parts.port is None else f"{host}:{parts.port}"
    password = "***" if mask else cred.password
    userinfo = f"{cred.user}:{password}".replace("{", "{{").replace("}", "}}")
    # Build the string directly: urlunsplit would mangle '#', '?' or '/' in
    # the password by treating them as URL delimiters.
    rest = urllib.parse.urlunsplit(parts._replace(netloc=hostport))
    scheme_prefix = f"{parts.scheme}://"
    return f"{scheme_prefix}{userinfo}@{rest[len(scheme_prefix):]}"


def camera_yaml(name: str, cam: Camera, indent: int = 2, mask: bool = False) -> str:
    record, detect = choose_streams(cam.streams)
    pad = " " * indent

    def q(value: str) -> str:
        return json.dumps(value)  # a JSON string is a valid YAML double-quoted scalar

    desc = " ".join(x for x in (cam.manufacturer, cam.model, cam.host) if x)
    lines = [f"{pad}{name}:  # {desc} (added by camera discovery)", f"{pad}  enabled: true"]
    lines += [f"{pad}  ffmpeg:", f"{pad}    inputs:"]
    if record.token == detect.token:
        lines += [
            f"{pad}      - path: {q(with_credentials(record.uri, cam.credential, mask))}",
            f"{pad}        roles:",
            f"{pad}          - detect",
            f"{pad}          - record",
        ]
    else:
        lines += [
            f"{pad}      - path: {q(with_credentials(record.uri, cam.credential, mask))}",
            f"{pad}        roles:",
            f"{pad}          - record",
            f"{pad}      - path: {q(with_credentials(detect.uri, cam.credential, mask))}",
            f"{pad}        roles:",
            f"{pad}          - detect",
        ]
    lines.append(f"{pad}  detect:")
    if detect.width and detect.height:
        lines += [f"{pad}    width: {detect.width}", f"{pad}    height: {detect.height}"]
    lines.append(f"{pad}    fps: 5")
    return "\n".join(lines) + "\n"


_HOST_AFTER_LOGIN = re.compile(r".*@([A-Za-z0-9.\-]+)(?::\d+)?(?:[/\"'\s]|$)")
_HOST_NO_LOGIN = re.compile(r"([A-Za-z0-9.\-]+)(?::\d+)?(?:[/\"'\s]|$)")


def configured_hosts(config_text: str) -> set[str]:
    """Hosts already used in any rtsp:// address in the config.

    Passwords are stored unencoded and may contain '@', '/', '#' or spaces,
    so the host is taken after the *last* '@' on the line.
    """
    hosts: set[str] = set()
    for line in config_text.splitlines():
        for start in (m.end() for m in re.finditer("rtsp://", line)):
            rest = line[start:]
            match = _HOST_AFTER_LOGIN.match(rest) if "@" in rest else _HOST_NO_LOGIN.match(rest)
            if match:
                hosts.add(match.group(1))
    return hosts


def _cameras_indent(lines: list[str], start: int) -> int:
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        return indent if indent > 0 else 2
    return 2


def insert_cameras(config_text: str, cams: list[tuple[str, Camera]]) -> str:
    """Add camera blocks under the top-level ``cameras:`` key (created if absent)."""
    lines = config_text.splitlines(keepends=True)
    idx = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"^cameras:\s*(\{\s*\})?\s*(#.*)?$", line.rstrip("\n"))
        ),
        None,
    )
    if idx is None:
        if re.search(r"^cameras:", config_text, re.M):
            raise ValueError("the 'cameras:' section has an unexpected layout; add cameras by hand")
        if config_text and not config_text.endswith("\n"):
            config_text += "\n"
        blocks = "".join(camera_yaml(n, c, indent=2) for n, c in cams)
        return f"{config_text}\ncameras:\n{blocks}"
    if "{" in lines[idx]:
        lines[idx] = "cameras:\n"
    indent = _cameras_indent(lines, idx)
    blocks = "".join(camera_yaml(n, c, indent=indent) for n, c in cams)
    return "".join(lines[: idx + 1]) + blocks + "".join(lines[idx + 1 :])


def existing_camera_names(config_text: str) -> set[str]:
    names: set[str] = set()
    lines = config_text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^cameras:", line):
            indent = _cameras_indent(lines, i)
            for child in lines[i + 1 :]:
                if child.strip() and not child.startswith(" ") and not child.startswith("#"):
                    break
                m = re.match(rf"^ {{{indent}}}([A-Za-z0-9_-]+):", child)
                if m:
                    names.add(m.group(1))
    return names


def write_config(path: Path, text: str) -> Path:
    """Back up ``path``, then replace it atomically keeping owner and mode 0600."""
    try:
        import yaml  # optional: validate when PyYAML is available
    except ImportError:  # pragma: no cover - depends on the host
        yaml = None
    if yaml is not None:
        yaml.safe_load(text)

    backup = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    st = path.stat()
    os.chmod(tmp, 0o600)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    return backup


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def plan_additions(cams: list[Camera], config_text: str) -> list[tuple[str, Camera]]:
    known = configured_hosts(config_text)
    taken = existing_camera_names(config_text)
    return [(camera_name(c, taken), c) for c in cams if c.ok and c.host not in known]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find ONVIF cameras and add them to the Frigate config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[0] if __doc__ else None,
    )
    parser.add_argument("--credentials", help="file with user:pass lines (default: .cam)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Frigate config file")
    parser.add_argument("--apply", action="store_true", help="write new cameras to the config")
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="camera IP, IP:PORT or ONVIF URL to use instead of discovery (repeatable)",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="discovery wait (seconds)")
    parser.add_argument("--interface-ip", help="local IP of the interface to discover on")
    args = parser.parse_args(argv)

    cred_path = find_credentials_file(args.credentials)
    if cred_path is None:
        print(
            "No credentials file found. Create .cam with one user:pass per line.", file=sys.stderr
        )
        return 2
    try:
        credentials = load_credentials(cred_path)
    except (OSError, ValueError) as e:
        print(f"Credentials: {e}", file=sys.stderr)
        return 2
    warning = credentials_file_warning(cred_path)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)

    if args.host:
        xaddrs = []
        for h in args.host:
            url = resolve_host(h)
            if url:
                xaddrs.append(url)
            else:
                print(f"  {h}: no ONVIF service found (is ONVIF enabled?)")
    else:
        print(f"Searching for ONVIF cameras ({args.timeout:.0f}s)...")
        xaddrs = ws_discover(args.timeout, interface_ip=args.interface_ip)

    if not xaddrs:
        print("No cameras found.")
        print("  - Turn on ONVIF in the camera settings")
        print("  - The server must be on the same network as the cameras,")
        print("    or pass their addresses with --host")
        return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        cams = list(pool.map(lambda x: probe_camera(x, credentials), xaddrs))
    cams.sort(key=lambda c: tuple(int(p) if p.isdigit() else 0 for p in c.host.split(".")))

    print(f"\nFound {len(cams)} camera(s):")
    for c in cams:
        label = " ".join(x for x in (c.manufacturer, c.model) if x) or "unknown model"
        if c.ok:
            streams = ", ".join(
                f"{s.width}x{s.height}" if s.width else (s.name or s.token) for s in c.streams
            )
            print(
                f"  {c.host:<16} {label:<32} login: {c.credential.user if c.credential else ''}  streams: {streams}"
            )
        else:
            print(f"  {c.host:<16} {label:<32} {c.error}")

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"\nFrigate config not found: {config_path}", file=sys.stderr)
        return 2 if args.apply else 0
    config_text = config_path.read_text()
    additions = plan_additions(cams, config_text)
    already = [c for c in cams if c.ok and c.host in configured_hosts(config_text)]
    for c in already:
        print(f"  {c.host}: already in {config_path}")

    if not additions:
        print("\nNo new cameras to add.")
        return 0

    print(f"\n{'Adding' if args.apply else 'Would add'} {len(additions)} camera(s):\n")
    for name, cam in additions:
        print(camera_yaml(name, cam, mask=True))

    if not args.apply:
        print("Run again with --apply to write them to the Frigate config.")
        return 0
    try:
        new_text = insert_cameras(config_text, additions)
        backup = write_config(config_path, new_text)
    except Exception as e:  # noqa: BLE001 - report any failure, config untouched
        print(f"Could not update {config_path}: {e}", file=sys.stderr)
        return 1
    print(f"Updated {config_path} (backup: {backup.name}). Restart Frigate to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
