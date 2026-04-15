"""Unit tests for the URL allowlist used by /api/transcribe."""

import unittest
from api._shared import is_allowed_url


class URLAllowlistTests(unittest.TestCase):
    def test_accepts_tiktok_video(self):
        self.assertTrue(is_allowed_url("https://www.tiktok.com/@user/video/123456"))

    def test_accepts_tiktok_short_link(self):
        self.assertTrue(is_allowed_url("https://www.tiktok.com/t/ZTkkabcd/"))

    def test_accepts_tiktok_mobile(self):
        self.assertTrue(is_allowed_url("https://m.tiktok.com/@user/video/123"))

    def test_accepts_vm_tiktok(self):
        self.assertTrue(is_allowed_url("https://vm.tiktok.com/abc/"))

    def test_accepts_youtube_watch(self):
        self.assertTrue(is_allowed_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_accepts_youtu_be_short(self):
        self.assertTrue(is_allowed_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_rejects_http_scheme(self):
        self.assertFalse(is_allowed_url("http://www.tiktok.com/@x/video/1"))

    def test_rejects_ftp_scheme(self):
        self.assertFalse(is_allowed_url("ftp://www.tiktok.com/video"))

    def test_rejects_file_scheme(self):
        self.assertFalse(is_allowed_url("file:///etc/passwd"))

    def test_rejects_unknown_host(self):
        self.assertFalse(is_allowed_url("https://evil.com/video"))

    def test_rejects_aws_metadata(self):
        self.assertFalse(is_allowed_url("https://169.254.169.254/latest/meta-data/"))

    def test_rejects_localhost(self):
        self.assertFalse(is_allowed_url("https://localhost/abc"))

    def test_rejects_raw_ip(self):
        self.assertFalse(is_allowed_url("https://10.0.0.1/video"))

    def test_rejects_empty(self):
        self.assertFalse(is_allowed_url(""))

    def test_rejects_none(self):
        self.assertFalse(is_allowed_url(None))  # type: ignore[arg-type]

    def test_rejects_malformed(self):
        self.assertFalse(is_allowed_url("not a url"))

    def test_rejects_userinfo_smuggling(self):
        # urlparse hostname → evil.com, but belt-and-suspenders
        self.assertFalse(is_allowed_url("https://tiktok.com@evil.com/video"))

    def test_rejects_subdomain_of_allowlisted(self):
        # 'attacker.tiktok.com.evil.net' host is evil.net, correctly rejected
        self.assertFalse(is_allowed_url("https://tiktok.com.evil.net/video"))

    def test_rejects_hostname_case_insensitively_matches(self):
        self.assertTrue(is_allowed_url("https://WWW.TikTok.com/@x/video/1"))


if __name__ == "__main__":
    unittest.main()
