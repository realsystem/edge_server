"""Tests for tools/camera_discovery.py against a fake ONVIF camera."""

import base64
import datetime as dt
import hashlib
import socket
import stat
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

import camera_discovery as cd

USER, PASSWORD = "admin", "p@ss:w/rd {x}"
CLOCK_OFFSET = 3600  # camera clock runs an hour ahead (common in the field)
WRONG_IP = "10.99.99.99"  # cameras often report an internal address


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    return next((e for e in el.iter() if _local(e.tag) == name), None)


class FakeCamera:
    """Minimal ONVIF device + media service with WS-Security digest auth."""

    def __init__(self, http_basic=False, fail_status=400, profiles=None, manufacturer="Reolink"):
        self.http_basic = http_basic
        self.fail_status = fail_status
        self.manufacturer = manufacturer
        self.profiles = profiles or [
            ("000", "mainStream", 3840, 2160),
            ("001", "subStream", 640, 360),
        ]
        self.requests = []
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                camera.requests.append(body)
                status, payload, headers = camera.handle(body, self.headers)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def xaddr(self):
        return f"http://127.0.0.1:{self.port}/onvif/device_service"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def now(self):
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=CLOCK_OFFSET)

    @staticmethod
    def envelope(inner):
        return (
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
            ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema">'
            f"<s:Body>{inner}</s:Body></s:Envelope>"
        ).encode()

    def fault(self):
        return (
            self.fail_status,
            self.envelope(
                "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                "<s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>"
                "<s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>"
            ),
            {},
        )

    def authorized(self, root, headers):
        if self.http_basic:
            expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            return headers.get("Authorization") == expected
        token = _find(root, "UsernameToken")
        if token is None:
            return False
        nonce = base64.b64decode(_find(token, "Nonce").text)
        created = _find(token, "Created").text
        expected = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + PASSWORD.encode()).digest()
        ).decode()
        when = dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.000Z").replace(
            tzinfo=dt.timezone.utc
        )
        fresh = abs((when - self.now()).total_seconds()) < 300
        return (
            _find(token, "Username").text == USER
            and _find(token, "Password").text == expected
            and fresh
        )

    def handle(self, body, headers):
        root = ET.fromstring(body)
        action = _local(next(iter(_find(root, "Body"))).tag)
        if action == "GetSystemDateAndTime":
            n = self.now()
            return (
                200,
                self.envelope(
                    "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                    f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month>"
                    f"<tt:Day>{n.day}</tt:Day></tt:Date><tt:Time><tt:Hour>{n.hour}</tt:Hour>"
                    f"<tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                    "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
                ),
                {},
            )
        if not self.authorized(root, headers):
            if self.http_basic:
                return 401, b"", {"WWW-Authenticate": 'Basic realm="cam"'}
            return self.fault()
        if action == "GetDeviceInformation":
            return (
                200,
                self.envelope(
                    "<tds:GetDeviceInformationResponse>"
                    f"<tds:Manufacturer>{self.manufacturer}</tds:Manufacturer>"
                    "<tds:Model>RLC-810A</tds:Model><tds:SerialNumber>SN123</tds:SerialNumber>"
                    "</tds:GetDeviceInformationResponse>"
                ),
                {},
            )
        if action == "GetCapabilities":
            return (
                200,
                self.envelope(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                    f"<tt:XAddr>http://{WRONG_IP}:{self.port}/onvif/media</tt:XAddr>"
                    "</tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
                ),
                {},
            )
        if action == "GetProfiles":
            profiles = "".join(
                f'<trt:Profiles token="{t}"><tt:Name>{n}</tt:Name><tt:VideoEncoderConfiguration>'
                f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width>"
                f"<tt:Height>{h}</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration>"
                "</trt:Profiles>"
                for t, n, w, h in self.profiles
            )
            return (
                200,
                self.envelope(f"<trt:GetProfilesResponse>{profiles}</trt:GetProfilesResponse>"),
                {},
            )
        if action == "GetStreamUri":
            token = _find(root, "ProfileToken").text
            path = "h264Preview_01_main" if token == "000" else "h264Preview_01_sub"
            return (
                200,
                self.envelope(
                    "<trt:GetStreamUriResponse><trt:MediaUri>"
                    f"<tt:Uri>rtsp://{WRONG_IP}:554/{path}</tt:Uri>"
                    "</trt:MediaUri></trt:GetStreamUriResponse>"
                ),
                {},
            )
        return (
            500,
            self.envelope("<s:Fault><s:Reason><s:Text>unknown</s:Text></s:Reason></s:Fault>"),
            {},
        )


@pytest.fixture
def camera():
    cam = FakeCamera()
    yield cam
    cam.close()


CREDS = [cd.Credential("admin", "wrong"), cd.Credential(USER, PASSWORD)]


# --------------------------------------------------------------------- ONVIF


class TestProbeCamera:
    def test_success_tries_credentials_in_order(self, camera):
        cam = cd.probe_camera(camera.xaddr, CREDS)
        assert cam.ok, cam.error
        assert cam.credential.password == PASSWORD
        assert (cam.manufacturer, cam.model, cam.serial) == ("Reolink", "RLC-810A", "SN123")
        assert [(s.width, s.height) for s in cam.streams] == [(3840, 2160), (640, 360)]
        # wrong IP from the camera replaced with the address we reached it on
        assert cam.streams[0].uri == "rtsp://127.0.0.1:554/h264Preview_01_main"

    def test_clock_offset_is_used(self, camera):
        client = cd.OnvifClient(camera.xaddr, CREDS[1])
        client.sync_time()
        assert abs(client.time_offset - CLOCK_OFFSET) < 5

    def test_without_clock_sync_login_fails(self, camera, monkeypatch):
        monkeypatch.setattr(cd.OnvifClient, "sync_time", lambda self: None)
        cam = cd.probe_camera(camera.xaddr, CREDS)
        assert not cam.ok and "login rejected" in cam.error

    def test_all_credentials_rejected(self, camera):
        cam = cd.probe_camera(camera.xaddr, [cd.Credential("admin", "nope")])
        assert not cam.ok
        assert "login rejected" in cam.error

    @pytest.mark.parametrize("status", [400, 500])
    def test_auth_fault_any_status(self, status):
        cam_server = FakeCamera(fail_status=status)
        try:
            cam = cd.probe_camera(cam_server.xaddr, CREDS)
            assert cam.ok, cam.error
        finally:
            cam_server.close()

    def test_http_basic_auth_camera(self):
        cam_server = FakeCamera(http_basic=True)
        try:
            cam = cd.probe_camera(cam_server.xaddr, CREDS)
            assert cam.ok, cam.error
        finally:
            cam_server.close()

    def test_unreachable_camera(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cam = cd.probe_camera(f"http://127.0.0.1:{port}/onvif/device_service", CREDS, timeout=1)
        assert not cam.ok and "no response" in cam.error

    def test_single_profile_camera(self):
        cam_server = FakeCamera(profiles=[("000", "main", 1920, 1080)])
        try:
            cam = cd.probe_camera(cam_server.xaddr, CREDS)
            record, detect = cd.choose_streams(cam.streams)
            assert record is detect
            block = yaml.safe_load(cd.camera_yaml("c", cam, indent=0))
            inputs = block["c"]["ffmpeg"]["inputs"]
            assert len(inputs) == 1 and inputs[0]["roles"] == ["detect", "record"]
        finally:
            cam_server.close()


class TestResolveHost:
    def test_ip_port(self, camera):
        assert cd.resolve_host(f"127.0.0.1:{camera.port}") == camera.xaddr

    def test_url_passthrough(self):
        assert cd.resolve_host("http://x/onvif/device_service") == "http://x/onvif/device_service"

    def test_nothing_listening(self, monkeypatch):
        monkeypatch.setattr(cd, "ONVIF_PORTS", (1,))
        assert cd.resolve_host("127.0.0.1", timeout=1) is None


# ------------------------------------------------------------- WS-Discovery


def probe_match(*xaddrs):
    return (
        '<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches>'
        "<d:ProbeMatch>"
        f"<d:XAddrs>{' '.join(xaddrs)}</d:XAddrs>"
        "</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"
    ).encode()


class TestWSDiscovery:
    def test_parse_prefers_ipv4(self):
        data = probe_match(
            "http://[fe80::1]/onvif/device_service", "http://192.168.1.20:8000/onvif/device_service"
        )
        assert cd.parse_probe_matches(data) == ["http://192.168.1.20:8000/onvif/device_service"]

    def test_parse_garbage(self):
        assert cd.parse_probe_matches(b"not xml") == []

    def test_probe_message_is_valid_xml(self):
        for ns, types in cd.PROBE_TYPES:
            root = ET.fromstring(cd.probe_message(ns, types, "uuid:1"))
            assert _find(root, "Types").text == types

    def test_discover_collects_and_dedupes(self):
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.bind(("127.0.0.1", 0))
        responder.settimeout(5)
        port = responder.getsockname()[1]

        def reply():
            # answer every probe (4 sent: 2 types x 2 repeats) for two cameras
            for _ in range(4):
                try:
                    _, addr = responder.recvfrom(65535)
                except OSError:
                    return
                responder.sendto(probe_match("http://192.168.1.20/onvif/device_service"), addr)
                responder.sendto(probe_match("http://192.168.1.21:8000/onvif/device_service"), addr)

        t = threading.Thread(target=reply)
        t.start()
        try:
            found = cd.ws_discover(timeout=1.0, target=("127.0.0.1", port))
        finally:
            t.join()
            responder.close()
        assert sorted(found) == [
            "http://192.168.1.20/onvif/device_service",
            "http://192.168.1.21:8000/onvif/device_service",
        ]


# -------------------------------------------------------------- credentials


class TestCredentials:
    def test_load(self, tmp_path):
        f = tmp_path / ".cam"
        f.write_text("# comment\n\nadmin:pass:with:colons\nviewer:\n")
        assert cd.load_credentials(f) == [
            cd.Credential("admin", "pass:with:colons"),
            cd.Credential("viewer", ""),
        ]

    @pytest.mark.parametrize("content", ["no-colon\n", ":pass\n", "# only comments\n"])
    def test_invalid(self, tmp_path, content):
        f = tmp_path / ".cam"
        f.write_text(content)
        with pytest.raises(ValueError):
            cd.load_credentials(f)

    def test_permission_warning(self, tmp_path):
        f = tmp_path / ".cam"
        f.write_text("a:b\n")
        f.chmod(0o644)
        assert "chmod 600" in cd.credentials_file_warning(f)
        f.chmod(0o600)
        assert cd.credentials_file_warning(f) is None

    def test_find_order(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env.cam"
        env_file.write_text("a:b\n")
        monkeypatch.setenv("CAM_CREDENTIALS_FILE", str(env_file))
        assert cd.find_credentials_file(None) == env_file
        explicit = tmp_path / "x.cam"
        explicit.write_text("a:b\n")
        assert cd.find_credentials_file(str(explicit)) == explicit


# ------------------------------------------------------------ Frigate config


def make_cam(host="192.168.1.20", manufacturer="Reolink", password="p@ss/{x}"):
    return cd.Camera(
        host=host,
        xaddr=f"http://{host}/onvif/device_service",
        manufacturer=manufacturer,
        model="RLC-810A",
        credential=cd.Credential("admin", password),
        streams=[
            cd.Stream("000", uri=f"rtsp://{host}:554/main", width=3840, height=2160),
            cd.Stream("001", uri=f"rtsp://{host}:554/sub", width=640, height=360),
        ],
    )


TEMPLATE = """# Frigate NVR Configuration
mqtt:
  enabled: false

cameras:
  # Placeholder camera - replace with your actual camera
  placeholder_camera:
    enabled: false
    ffmpeg:
      inputs:
        - path: rtsp://user:password@camera-ip:554/stream
          roles:
            - detect

  # Example: Add your cameras here
  # barn_cam:
  #   enabled: true
"""


class TestFrigateConfig:
    def test_camera_yaml_is_valid_and_password_not_encoded(self):
        block = yaml.safe_load(cd.camera_yaml("reolink_20", make_cam(), indent=0))
        cam = block["reolink_20"]
        record, detect = cam["ffmpeg"]["inputs"]
        assert record == {
            # raw password (Frigate encodes it itself); braces doubled for
            # Frigate's {FRIGATE_*} substitution
            "path": "rtsp://admin:p@ss/{{x}}@192.168.1.20:554/main",
            "roles": ["record"],
        }
        assert detect["roles"] == ["detect"]
        assert cam["detect"] == {"width": 640, "height": 360, "fps": 5}

    def test_masked_output_hides_password(self):
        text = cd.camera_yaml("c", make_cam(), mask=True)
        assert "p@ss" not in text and "admin:***@" in text

    @pytest.mark.parametrize("password", ["#", "p#ss", "a%23b", "x?y/z", "p@ss:w", "with space"])
    def test_password_round_trips_through_frigate_encoding(self, password):
        # What Frigate does with the path: str.format for {FRIGATE_*} vars,
        # then URL-encode the password (quote_plus). Decoding that must give
        # the original password back, i.e. no double encoding.
        path = cd.with_credentials("rtsp://10.0.0.1:554/s", cd.Credential("admin", password))
        formatted = path.format()
        userinfo = formatted[len("rtsp://") : formatted.rindex("@")]
        assert userinfo.split(":", 1)[1] == password
        assert formatted.endswith("@10.0.0.1:554/s")

    def test_existing_userinfo_replaced(self):
        uri = cd.with_credentials("rtsp://old:old@10.0.0.1/x", cd.Credential("u", "p"))
        assert uri == "rtsp://u:p@10.0.0.1/x"

    def test_insert_into_template(self):
        new = cd.insert_cameras(TEMPLATE, [("reolink_20", make_cam())])
        data = yaml.safe_load(new)
        assert set(data["cameras"]) == {"reolink_20", "placeholder_camera"}
        assert new.startswith(TEMPLATE.split("cameras:")[0])  # rest of file untouched
        assert "# Example: Add your cameras here" in new

    def test_insert_matches_four_space_indent(self):
        text = "cameras:\n    existing:\n        enabled: true\n"
        data = yaml.safe_load(cd.insert_cameras(text, [("new", make_cam())]))
        assert set(data["cameras"]) == {"existing", "new"}

    @pytest.mark.parametrize("text", ["mqtt:\n  enabled: false\n", "mqtt: {}", "cameras: {}\n"])
    def test_insert_creates_or_expands_section(self, text):
        data = yaml.safe_load(cd.insert_cameras(text, [("new", make_cam())]))
        assert list(data["cameras"]) == ["new"]

    def test_unexpected_layout_refused(self):
        with pytest.raises(ValueError):
            cd.insert_cameras("cameras: !include cams.yml\n", [("new", make_cam())])

    @pytest.mark.parametrize(
        "line,host",
        [
            (
                '        - path: "rtsp://admin:p@ss:w/rd {{x}}@192.168.1.20:554/main"',
                "192.168.1.20",
            ),
            ('        - path: "rtsp://admin:#@192.168.1.21:554/x"', "192.168.1.21"),
            ("        - path: rtsp://192.168.1.22/stream", "192.168.1.22"),
            ("        - path: rtsp://user:password@camera-ip:554/stream", "camera-ip"),
            ('        - path: "rtsp://u:a b@cam.local"', "cam.local"),
        ],
    )
    def test_configured_hosts_with_raw_passwords(self, line, host):
        assert cd.configured_hosts(line) == {host}

    def test_plan_skips_known_hosts_and_avoids_name_clash(self):
        text = TEMPLATE.replace("placeholder_camera", "reolink_20") + (
            "  front:\n    ffmpeg:\n      inputs:\n"
            '        - path: "rtsp://u:p@192.168.1.30:554/x"\n'
        )
        cams = [
            make_cam("192.168.1.20"),  # name clash only
            make_cam("192.168.1.30"),  # already configured
            make_cam("192.168.1.31", manufacturer=""),
        ]
        broken = make_cam("192.168.1.40")
        broken.credential = None
        plan = cd.plan_additions([*cams, broken], text)
        assert [(n, c.host) for n, c in plan] == [
            ("reolink_20_2", "192.168.1.20"),
            ("camera_31", "192.168.1.31"),
        ]

    def test_write_config_backup_and_mode(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("cameras:\n")
        cfg.chmod(0o644)
        backup = cd.write_config(cfg, "cameras:\n  a: {}\n")
        assert backup.read_text() == "cameras:\n"
        assert cfg.read_text() == "cameras:\n  a: {}\n"
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_write_config_rejects_invalid_yaml(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("cameras:\n")
        with pytest.raises(yaml.YAMLError):
            cd.write_config(cfg, "cameras: [unclosed\n")
        assert cfg.read_text() == "cameras:\n"
        assert not list(tmp_path.glob("config.yml.bak.*"))


# ----------------------------------------------------------------------- CLI


@pytest.fixture
def setup(tmp_path, camera):
    creds = tmp_path / ".cam"
    creds.write_text(f"admin:wrong\n{USER}:{PASSWORD}\n")
    creds.chmod(0o600)
    cfg = tmp_path / "config.yml"
    cfg.write_text(TEMPLATE)
    base = ["--credentials", str(creds), "--config", str(cfg), "--host", f"127.0.0.1:{camera.port}"]
    return cfg, base


class TestMain:
    def test_dry_run_changes_nothing(self, setup, capsys):
        cfg, base = setup
        assert cd.main(base) == 0
        out = capsys.readouterr().out
        assert "Reolink RLC-810A" in out and "3840x2160, 640x360" in out
        assert "Would add 1 camera(s)" in out
        assert "admin:***@127.0.0.1" in out and "p@ss" not in out
        assert cfg.read_text() == TEMPLATE

    def test_apply_then_idempotent(self, setup, capsys):
        cfg, base = setup
        assert cd.main([*base, "--apply"]) == 0
        data = yaml.safe_load(cfg.read_text())
        assert "reolink_1" in data["cameras"]
        assert list(cfg.parent.glob("config.yml.bak.*"))
        capsys.readouterr()
        assert cd.main([*base, "--apply"]) == 0
        out = capsys.readouterr().out
        assert "already in" in out and "No new cameras" in out

    def test_missing_credentials(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CAM_CREDENTIALS_FILE", raising=False)
        monkeypatch.setattr(cd, "CREDENTIAL_LOCATIONS", ())
        monkeypatch.setattr(cd, "find_credentials_file", lambda explicit: None)
        assert cd.main(["--config", str(tmp_path / "c.yml")]) == 2

    def test_bad_credentials_file(self, tmp_path):
        f = tmp_path / ".cam"
        f.write_text("nocolon\n")
        assert cd.main(["--credentials", str(f)]) == 2

    def test_no_cameras_found(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / ".cam"
        f.write_text("a:b\n")
        f.chmod(0o600)
        monkeypatch.setattr(cd, "ws_discover", lambda *a, **k: [])
        assert cd.main(["--credentials", str(f)]) == 0
        assert "Turn on ONVIF" in capsys.readouterr().out

    def test_host_without_onvif(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / ".cam"
        f.write_text("a:b\n")
        monkeypatch.setattr(cd, "resolve_host", lambda h: None)
        assert cd.main(["--credentials", str(f), "--host", "10.0.0.9"]) == 0
        out = capsys.readouterr().out
        assert "no ONVIF service found" in out
        assert "WARNING" not in out  # warning goes to stderr

    def test_missing_config_with_apply(self, setup, tmp_path):
        _, base = setup
        args = [
            a if a != str(tmp_path / "config.yml") else str(tmp_path / "nope.yml") for a in base
        ]
        assert cd.main([*args, "--apply"]) == 2

    def test_apply_failure_reports(self, setup, monkeypatch):
        cfg, base = setup
        cfg.write_text("cameras: !weird\n")
        assert cd.main([*base, "--apply"]) == 1
        assert cfg.read_text() == "cameras: !weird\n"
