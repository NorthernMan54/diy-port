# SPDX-License-Identifier: AGPL-3.0-or-later
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
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

log = logging.getLogger("aiport-onvif")

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
    """The profile's RTSP URL carrying the main URL's login (GetStreamUri omits it)."""
    m, u = urlsplit(main), urlsplit(url)
    if "@" not in m.netloc or "@" in u.netloc:
        return url
    return urlunsplit(u._replace(netloc=m.netloc.rsplit("@", 1)[0] + "@" + u.netloc))


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
    user, password = unquote(parts.username or ""), unquote(parts.password or "")

    def soap(url, body):
        data = _envelope(body, user, password).encode()
        if post:
            return post(url, data)
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/soap+xml; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    media = f"http://{parts.hostname}/onvif/Media"
    try:
        caps = ET.fromstring(soap(f"http://{parts.hostname}/onvif/device_service",
                                  "<tds:GetCapabilities><tds:Category>Media</tds:Category>"
                                  "</tds:GetCapabilities>"))
        media = _text(caps, "Media", "XAddr") or media
    except Exception as e:
        log.debug("ONVIF GetCapabilities on %s failed (%s); trying %s", parts.hostname, e, media)
    profiles = parse_profiles(soap(media, "<trt:GetProfiles/>"))
    for p in profiles:
        resp = soap(media, (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(p['token'] or '')}</trt:ProfileToken></trt:GetStreamUri>"))
        uri = _text(ET.fromstring(resp), "Uri")
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
