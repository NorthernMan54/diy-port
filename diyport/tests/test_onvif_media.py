# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""Unit tests for onvif_media.py: profile parsing, substream choice, and the SOAP
calls (against canned responses shaped like a Hikvision camera's)."""
import http.server
import os
import sys
import threading
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import onvif_media  # noqa: E402

_ENV = ('<?xml version="1.0" encoding="UTF-8"?><env:Envelope '
        'xmlns:env="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"><env:Body>{}</env:Body></env:Envelope>')


def _profile(token, name, enc, w, h, fps):
    return (f'<trt:Profiles token="{token}" fixed="true"><tt:Name>{name}</tt:Name>'
            '<tt:VideoSourceConfiguration token="VideoSourceToken"><tt:Name>VideoSourceConfig'
            '</tt:Name><tt:Bounds x="0" y="0" width="2560" height="1440"/>'
            '</tt:VideoSourceConfiguration>'
            f'<tt:VideoEncoderConfiguration token="V{token}"><tt:Name>V{token}</tt:Name>'
            f'<tt:Encoding>{enc}</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width>'
            f'<tt:Height>{h}</tt:Height></tt:Resolution><tt:RateControl>'
            f'<tt:FrameRateLimit>{fps}</tt:FrameRateLimit><tt:BitrateLimit>3072</tt:BitrateLimit>'
            '</tt:RateControl></tt:VideoEncoderConfiguration>'
            '<tt:AudioEncoderConfiguration token="A"><tt:Encoding>G711</tt:Encoding>'
            '</tt:AudioEncoderConfiguration></trt:Profiles>')


PROFILES = _ENV.format('<trt:GetProfilesResponse>'
                       + _profile("Profile_1", "mainStream", "H264", 2560, 1440, 20)
                       + _profile("Profile_2", "subStream", "H264", 640, 360, 20)
                       + '</trt:GetProfilesResponse>')
CAPS = _ENV.format('<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>'
                   '<tt:XAddr>http://192.0.2.10/onvif/Media</tt:XAddr></tt:Media>'
                   '</tds:Capabilities></tds:GetCapabilitiesResponse>')


def _uri(channel, token):
    return _ENV.format('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>'
                       f'rtsp://192.0.2.10:554/Streaming/Channels/{channel}?transportmode='
                       f'unicast&amp;profile={token}</tt:Uri></trt:MediaUri>'
                       '</trt:GetStreamUriResponse>')


MAIN = "rtsp://admin:p%40ss@192.0.2.10/Streaming/Channels/101?profile=Profile_1"
MAIN_PORT = "rtsp://admin:p%40ss@192.0.2.10:10554/Streaming/Channels/101"
CAPS_OFFHOST = _ENV.format(
    '<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>'
    '<tt:XAddr>http://198.51.100.9/onvif/Media</tt:XAddr></tt:Media>'
    '</tds:Capabilities></tds:GetCapabilitiesResponse>')


class TestParse(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(onvif_media.parse_profiles(PROFILES), [
            {"token": "Profile_1", "name": "mainStream", "encoding": "H264",
             "width": 2560, "height": 1440, "fps": 20},
            {"token": "Profile_2", "name": "subStream", "encoding": "H264",
             "width": 640, "height": 360, "fps": 20},
        ])

    def test_userinfo_carried_over(self):
        self.assertEqual(
            onvif_media._with_userinfo("rtsp://192.0.2.10:554/Streaming/Channels/102", MAIN),
            "rtsp://admin:p%40ss@192.0.2.10:554/Streaming/Channels/102")
        self.assertEqual(onvif_media._with_userinfo("rtsp://cam/102", "rtsp://cam/101"),
                         "rtsp://cam/102")


def _p(enc, w, h, url="rtsp://x"):
    return {"token": f"{w}", "encoding": enc, "width": w, "height": h, "fps": 20, "url": url}


class TestPick(unittest.TestCase):
    def test_smallest_h264_below_main(self):
        sub = onvif_media.pick_substream([_p("H264", 2560, 1440), _p("H264", 1280, 720),
                                          _p("H264", 640, 360)])
        self.assertEqual(sub["width"], 640)

    def test_hevc_substream_skipped(self):
        self.assertIsNone(onvif_media.pick_substream([_p("H264", 2560, 1440),
                                                      _p("H265", 640, 360)]))

    def test_single_or_same_size_profiles(self):
        self.assertIsNone(onvif_media.pick_substream([_p("H264", 2560, 1440)]))
        self.assertIsNone(onvif_media.pick_substream([_p("H264", 1920, 1080),
                                                      _p("H264", 1920, 1080)]))

    def test_profile_without_url_skipped(self):
        self.assertIsNone(onvif_media.pick_substream([_p("H264", 2560, 1440),
                                                      _p("H264", 640, 360, url=None)]))


class TestFetch(unittest.TestCase):
    def test_fetch_profiles(self):
        calls = []

        def post(url, body):
            calls.append((url, body.decode()))
            if b"GetCapabilities" in body:
                return CAPS
            if b"GetProfiles" in body:
                return PROFILES
            return _uri(101, "Profile_1") if b"Profile_1" in body else _uri(102, "Profile_2")

        profiles = onvif_media.fetch_profiles(MAIN, post=post)
        self.assertEqual([p["url"] for p in profiles], [
            "rtsp://admin:p%40ss@192.0.2.10:554/Streaming/Channels/101"
            "?transportmode=unicast&profile=Profile_1",
            "rtsp://admin:p%40ss@192.0.2.10:554/Streaming/Channels/102"
            "?transportmode=unicast&profile=Profile_2"])
        self.assertEqual([u for u, _ in calls], ["http://192.0.2.10/onvif/device_service"]
                         + ["http://192.0.2.10/onvif/Media"] * 3)
        # WS-Security digest login with the decoded user; the password never in clear.
        self.assertIn("<Username>admin</Username>", calls[0][1])
        self.assertNotIn("p@ss", calls[0][1])
        self.assertIn("#PasswordDigest", calls[0][1])

    def test_media_address_fallback(self):
        calls = []

        def post(url, body):
            calls.append(url)
            if b"GetCapabilities" in body:
                raise OSError("no device service")
            return PROFILES if b"GetProfiles" in body else _uri(102, "Profile_2")

        profiles = onvif_media.fetch_profiles("rtsp://192.0.2.10/101", post=post)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(calls[-1], "http://192.0.2.10/onvif/Media")

    def test_nonstandard_rtsp_port_is_tried_for_the_device_service(self):
        calls = []

        def post(url, body):
            calls.append(url)
            if b"GetCapabilities" in body:
                return CAPS
            return PROFILES if b"GetProfiles" in body else _uri(101, "Profile_1")

        onvif_media.fetch_profiles(MAIN_PORT, post=post)
        self.assertEqual(calls[0], "http://192.0.2.10:10554/onvif/device_service")

    def test_offhost_xaddr_is_ignored(self):
        calls = []

        def post(url, body):
            calls.append(url)
            if b"GetCapabilities" in body:
                return CAPS_OFFHOST
            return PROFILES if b"GetProfiles" in body else _uri(101, "Profile_1")

        onvif_media.fetch_profiles(MAIN, post=post)
        self.assertEqual(calls[1], "http://192.0.2.10/onvif/Media")
        self.assertNotIn("198.51.100.9", "\n".join(calls))

    def test_allowed_host_override_permits_another_camera_address(self):
        calls = []

        def post(url, body):
            calls.append(url)
            if b"GetCapabilities" in body:
                return CAPS_OFFHOST
            return PROFILES if b"GetProfiles" in body else _uri(101, "Profile_1")

        with mock.patch.dict(os.environ, {"AIPORT_ONVIF_ALLOW_HOSTS": "198.51.100.9"}):
            onvif_media.fetch_profiles(MAIN, post=post)
        self.assertIn("http://198.51.100.9/onvif/Media", calls)

    def test_offhost_stream_uri_is_ignored(self):
        def post(url, body):
            if b"GetCapabilities" in body:
                return CAPS
            if b"GetProfiles" in body:
                return PROFILES
            return _ENV.format('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>'
                               'rtsp://198.51.100.9:554/stream</tt:Uri></trt:MediaUri>'
                               '</trt:GetStreamUriResponse>')

        profiles = onvif_media.fetch_profiles(MAIN, post=post)
        self.assertIsNone(profiles[0]["url"])

    def test_camera_supplied_login_is_replaced(self):
        def post(url, body):
            if b"GetCapabilities" in body:
                return CAPS
            if b"GetProfiles" in body:
                return PROFILES
            return _ENV.format('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>'
                               'rtsp://camuser:campass@192.0.2.10/stream</tt:Uri>'
                               '</trt:MediaUri></trt:GetStreamUriResponse>')

        profile = onvif_media.fetch_profiles(MAIN, post=post)[0]
        self.assertTrue(profile["url"].startswith("rtsp://admin:p%40ss@192.0.2.10/"))
        self.assertNotIn("camuser", profile["url"])

    def test_malformed_xml_is_rejected(self):
        with self.assertRaises(ET.ParseError):
            onvif_media.fetch_profiles(MAIN, post=lambda url, body: b"not xml")

    def test_redirect_is_refused(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://198.51.100.9/steal")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(onvif_media, "ONVIF_HTTP_PORT", server.server_address[1]), \
                mock.patch.object(onvif_media, "ONVIF_SCHEME", "http"):
            with self.assertRaises(urllib.error.URLError):
                onvif_media.fetch_profiles("rtsp://127.0.0.1/101", timeout=1)

    def test_large_soap_response_is_capped(self):
        class Response:
            def __init__(self, data):
                self.data = data

            def read(self, n):
                return self.data[:n]

        self.assertEqual(onvif_media._read_capped(Response(b"x" * 10)), b"x" * 10)
        with self.assertRaises(ValueError):
            onvif_media._read_capped(Response(b"x" * (onvif_media.MAX_SOAP_BYTES + 1)))

    def test_no_login_without_userinfo(self):
        self.assertNotIn("Security", onvif_media._envelope("<x/>", "", ""))


if __name__ == "__main__":
    unittest.main()
