import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from service import versions


class VersionSiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.out = root / "out"
        for folder, engine in ((self.out / "1", None), (self.out / "1/vendors/9", "pi-online-v3")):
            folder.mkdir(parents=True)
            (folder / "business.zh.html").write_text("page " + str(engine))
            (folder / "business.case.zh.json").write_text(json.dumps({"title": "T", "meta": {"engine": engine}}))
        self.cfg_file = root / "versions.json"
        self.cfg_file.write_text(json.dumps({"v1": {"out": str(self.out)}, "v3": {"out": str(self.out), "engine": "pi-online-v3"},
                                             "bad": {"out": "x"}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_versions_list_and_filter_by_engine(self):
        with patch.object(versions, "CONFIG", self.cfg_file):
            got = versions.load()
        self.assertEqual(sorted(got), ["v1", "v3"])
        self.assertEqual(len(versions.scopes(got["v1"])), 2)
        self.assertEqual([(n, v) for n, v, _, _ in versions.scopes(got["v3"])], [("1", "9")])
        self.assertIsNone(versions.page(got["v3"], "1", None, "business.zh.html"))
        self.assertIsNotNone(versions.page(got["v3"], "1", "9", "business.zh.html"))
        self.assertIsNone(versions.page(got["v1"], "1", "../x", "business.zh.html"))
        self.assertIn("/v3/c/1/zh?vendor=9", versions.index("v3", got["v3"], got, "zh"))


    def test_routes_over_http(self):
        import http.server
        import threading
        import urllib.request
        from service import server
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with patch.object(versions, "CONFIG", self.cfg_file), patch.object(server, "AUTH", type("NoAuth", (), {"enabled": False})()):
                for path, expect in (("/v1", "V1"), ("/v3/c/1/zh?vendor=9", "page pi-online-v3")):
                    with urllib.request.urlopen(base + path) as r:
                        self.assertEqual(r.status, 200)
                        self.assertIn(expect, r.read().decode())
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
