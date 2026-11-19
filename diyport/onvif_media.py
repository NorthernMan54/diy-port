# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""An ONVIF camera's media profiles, for pushing its substream on a low channel.

Protect's OnvifStreamControl names one RTSP URL (the camera's main profile), so
without this every channel the AI Port pushes is the full-resolution stream. The
camera's ONVIF Media service lists the rest: GetCapabilities (device service) gives
the Media address, GetProfiles the encoder settings, GetStreamUri each RTSP URL.
The ONVIF login is the RTSP URL's own user and password (WS-Security digest).
"""
import base64
import datetime
import hashlib
import logging
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import envutil

log = logging.getLogger("aiport-onvif")

MAX_SOAP_BYTES = 256 * 1024
ONVIF_SCHEME = (os.environ.get("AIPORT_ONVIF_SCHEME") or "http").strip().lower()
if ONVIF_SCHEME not in ("http", "https"):
    ONVIF_SCHEME = "http"
ONVIF_HTTP_PORT = envutil.env_int("AIPORT_ONVIF_HTTP_PORT", 0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A camera cannot bounce the WS-Security request to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlsplit(newurl).hostname or "?"
        raise urllib.error.URLError(f"ONVIF redirect refused ({host})")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _allowed_hosts(main_host):
    extra = (os.environ.get("AIPORT_ONVIF_ALLOW_HOSTS") or "").split(",")
    return {main_host.lower()} | {h.strip().lower() for h in extra if h.strip()}


def _endpoint_ok(url, allowed, schemes, allow_userinfo=False):
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme not in schemes or not p.hostname or p.hostname.lower() not in allowed:
        return False
    try:
        port = p.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    if p.fragment or (p.username and not allow_userinfo):
        return False
    return True


def _read_capped(resp):
    data = resp.read(MAX_SOAP_BYTES + 1)
    if len(data) > MAX_SOAP_BYTES:
        raise ValueError("ONVIF response too large")
    return data


def _device_bases(main_url):
    """(scheme://host[:port], ...) to try for the device service, most likely first."""
    parts = urlsplit(main_url)
    host = parts.hostname
    if not host:
        raise ValueError("ONVIF URL has no host")
    hostpart = f"[{host}]" if ":" in host else host
    try:
        rtsp_port = parts.port
    except ValueError:
        rtsp_port = None
    default = 443 if ONVIF_SCHEME == "https" else 80
    ports = []
    if ONVIF_HTTP_PORT:
        ports.append(ONVIF_HTTP_PORT)
    else:
        if rtsp_port and rtsp_port != 554:
            ports.append(rtsp_port)
        ports += [80, 8080]
    seen = set()
    for port in ports:
        if port in seen:
            continue
        seen.add(port)
        netloc = hostpart if port == default else f"{hostpart}:{port}"
        yield f"{ONVIF_SCHEME}://{netloc}"

_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_TOKEN = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
_B64 = ("http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-soap-message-security-1.0#Base64Binary")


def _envelope(body, user, password):
    header = ""
    if user:
        nonce = os.urandom(16)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        header = (
            f'<s:Header><Security xmlns="{_WSSE}"><UsernameToken>'
            f"<Username>{escape(user)}</Username>"
            f'<Password Type="{_TOKEN}#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{_WSU}">{created}</Created>'
            "</UsernameToken></Security></s:Header>")
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, *path):
    """First descendant along local tag names, ignoring namespaces."""
    for name in path:
        el = next((c for c in el.iter() if c is not el and _local(c.tag) == name), None)
        if el is None:
            return None
    return el


def _text(el, *path):
    found = _find(el, *path)
    return (found.text or "").strip() if found is not None else ""


def _int(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _with_userinfo(url, main):
    """The profile's RTSP URL carrying the main URL's login, never the camera's own."""
    m, u = urlsplit(main), urlsplit(url)
    host = u.netloc.rsplit("@", 1)[-1]
    if "@" in m.netloc:
        host = m.netloc.rsplit("@", 1)[0] + "@" + host
    return urlunsplit(u._replace(netloc=host))


def parse_profiles(xml):
    """GetProfiles response -> [{token, name, encoding, width, height, fps}]."""
    out = []
    for prof in ET.fromstring(xml).iter():
        if _local(prof.tag) != "Profiles":
            continue
        venc = _find(prof, "VideoEncoderConfiguration")
        if venc is None:
            continue
        out.append({
            "token": prof.get("token"),
            "name": _text(prof, "Name"),
            "encoding": _text(venc, "Encoding").upper(),
            "width": _int(_text(venc, "Resolution", "Width")),
            "height": _int(_text(venc, "Resolution", "Height")),
            "fps": _int(_text(venc, "RateControl", "FrameRateLimit")),
        })
    return out


def fetch_profiles(main_url, timeout=5.0, post=None):
    """The camera's video profiles, each with its RTSP `url` (login included).

    `post(url, body) -> response text` is for tests; it defaults to a SOAP POST."""
    parts = urlsplit(main_url)
    host = parts.hostname
    if not host:
        raise ValueError("ONVIF URL has no host")
    user, password = unquote(parts.username or ""), unquote(parts.password or "")
    allowed = _allowed_hosts(host)
    bases = list(_device_bases(main_url))

    def soap(url, body):
        if not _endpoint_ok(url, allowed, ("http", "https")):
            raise ValueError(f"refusing ONVIF endpoint host {urlsplit(url).hostname or '?'}")
        data = _envelope(body, user, password).encode()
        if post:
            return post(url, data)
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/soap+xml; charset=utf-8"})
        with _OPENER.open(req, timeout=timeout) as resp:
            return _read_capped(resp)

    media = bases[0] + "/onvif/Media"
    for base in bases:
        try:
            caps = ET.fromstring(soap(base + "/onvif/device_service",
                                      "<tds:GetCapabilities><tds:Category>Media</tds:Category>"
                                      "</tds:GetCapabilities>"))
            xaddr = _text(caps, "Media", "XAddr")
            if xaddr:
                if _endpoint_ok(xaddr, allowed, ("http", "https")):
                    media = xaddr
                else:
                    log.warning("ONVIF camera %s returned an off-host Media XAddr; using %s",
                                host, media)
            break
        except Exception as e:
            log.debug("ONVIF GetCapabilities on %s failed (%s); trying next", host, e)
    profiles = parse_profiles(soap(media, "<trt:GetProfiles/>"))
    for p in profiles:
        resp = soap(media, (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(p['token'] or '')}</trt:ProfileToken></trt:GetStreamUri>"))
        uri = _text(ET.fromstring(resp), "Uri")
        if uri and not _endpoint_ok(uri, allowed, ("rtsp", "rtsps"), allow_userinfo=True):
            log.warning("ONVIF camera %s returned an off-host stream URI; ignoring it", host)
            p["url"] = None
        else:
            p["url"] = _with_userinfo(uri, main_url) if uri else None
    return profiles


def pick_substream(profiles):
    """The smallest H.264 profile below the largest one's resolution, or None.

    H.264 only: the push copies video, and Protect won't ingest HEVC over FLV."""
    sized = [p for p in profiles if p.get("url") and p.get("width") and p.get("height")]
    if len(sized) < 2:
        return None
    largest = max(p["width"] * p["height"] for p in sized)
    smaller = [p for p in sized if p["encoding"] == "H264"
               and p["width"] * p["height"] < largest]
    return min(smaller, key=lambda p: p["width"] * p["height"], default=None)
