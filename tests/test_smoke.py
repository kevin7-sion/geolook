import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import geolib as G
import smoke


class Response:
    def __init__(self, payload=None, text="", status_code=200):
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self.payload


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_work = G.WORK
        G.WORK = Path(self.tmp.name) / "work"
        self.addCleanup(setattr, G, "WORK", self.old_work)
        p = G.project_dir("acme")
        p.mkdir(parents=True)
        G.write_json(p / "geo.json", {"slug": "acme", "questions": [{"id": "q001"}]})

    @mock.patch.object(smoke.requests, "post")
    @mock.patch.object(smoke.requests, "get")
    def test_read_only_post_deploy_checks_pass(self, get, post):
        get.side_effect = [
            Response(text="<script>wbCompleteBlocks();正在补齐</script>"),
            Response({"slug": "acme"}),
            Response({"question": {"id": "q001"}, "sources": [{"kind": "outline"}]}),
        ]
        post.return_value = Response({"blocks": {"定义": True, "数字事实": False,
                                                   "对比": False, "操作步骤": False, "FAQ": True}})

        result = smoke.run("acme", "http://127.0.0.1:8768")

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 4)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
