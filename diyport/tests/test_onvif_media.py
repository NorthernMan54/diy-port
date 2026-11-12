# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for onvif_media.py: profile parsing, substream choice, and the SOAP
calls (against canned responses shaped like a Hikvision camera's)."""
import os
import sys
import unittest

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
                   '<tt:XAddr>http://192.168.1.42/onvif/Media</tt:XAddr></tt:Media>'
                   '</tds:Capabilities></tds:GetCapabilitiesResponse>')


def _uri(channel, token):
    return _ENV.format('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>'
                       f'rtsp://192.168.1.42:554/Streaming/Channels/{channel}?transportmode='
                       f'unicast&amp;profile={token}</tt:Uri></trt:MediaUri>'
                       '</trt:GetStreamUriResponse>')


MAIN = "rtsp://admin:p%40ss@192.168.1.42/Streaming/Channels/101?profile=Profile_1"


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
            onvif_media._with_userinfo("rtsp://192.168.1.42:554/Streaming/Channels/102", MAIN),
            "rtsp://admin:p%40ss@192.168.1.42:554/Streaming/Channels/102")
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
            "rtsp://admin:p%40ss@192.168.1.42:554/Streaming/Channels/101"
            "?transportmode=unicast&profile=Profile_1",
            "rtsp://admin:p%40ss@192.168.1.42:554/Streaming/Channels/102"
            "?transportmode=unicast&profile=Profile_2"])
        self.assertEqual([u for u, _ in calls], ["http://192.168.1.42/onvif/device_service"]
                         + ["http://192.168.1.42/onvif/Media"] * 3)
        # WS-Security digest login with the decoded user; the password never in clear.
        self.assertIn("<Username>admin</Username>", calls[0][1])
        self.assertNotIn("p@ss", calls[0][1])
        self.assertIn("#PasswordDigest", calls[0][1])

    def test_media_address_fallback(self):
        def post(url, body):
            if b"GetCapabilities" in body:
                raise OSError("no device service")
            return PROFILES if b"GetProfiles" in body else _uri(102, "Profile_2")

        profiles = onvif_media.fetch_profiles("rtsp://cam.local/101", post=post)
        self.assertEqual(len(profiles), 2)

    def test_no_login_without_userinfo(self):
        self.assertNotIn("Security", onvif_media._envelope("<x/>", "", ""))


if __name__ == "__main__":
    unittest.main()
