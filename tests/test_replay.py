"""按活动通用流程的测试：用一份虚构的小活动（临时目录），不联网、不碰真实数据。"""
import fcntl
import json
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import build, campaign, events, github, pipeline, util  # noqa: E402

HAN = re.compile(r"[\u3400-\u9fff]")
CID = "5555"      # 活动编号
N = "777777"      # 活动下的一条链接
N2 = "888888"     # 同一活动下的另一条链接


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class Sandbox(unittest.TestCase):
    """把 RAW / OUT / STATE 都指到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.saved = []
        for mod, name, val in [(util, "RAW", t / "raw"), (util, "OUT", t / "out"), (util, "STATE", t / "state"),
                               (events, "RAW", t / "raw"), (github, "RAW", t / "raw"), (pipeline, "STATE", t / "state")]:
            self.saved.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)
        (t / "state").mkdir()
        self.t = t

    def tearDown(self):
        for mod, name, val in self.saved:
            setattr(mod, name, val)
        self.tmp.cleanup()

    def seed(self, with_github=True, second_link=False):
        raw = self.t / "raw"
        camp = {"id": int(CID), "name": "Acme_CTV_US", "country_code": "US", "os": "iOS"}
        links = [N] + ([N2] if second_link else [])
        write(raw / "campaigns" / CID / "campaign.json", {"queried_at": "2026-10-01T00:00:00Z", "campaign": camp,
                                                          "links": links, "links_not_expanded": []})
        for n, base in ((N, 10), (N2, 50)):
            if n not in links:
                continue
            write(raw / "links" / n / "femini.json", {"queried_at": "2026-10-01T00:00:00Z", "click_url": {
                "name": n, "campaign": {"id": int(CID), "name": "Acme_CTV_US"}, "partner": {"name": "PartnerX"},
                "track_party": {"name": "AppsFlyer"},
                "proposed_changes": [
                    {"id": base + 1, "approve_status": "approved", "updated_at": "2026-09-02 10:00:30 UTC",
                     "created_at": "2026-09-02 10:00:00 UTC", "user_name": "Some Person", "new_margin": 20,
                     "change_logs": [{"action": "create", "created_at": "2026-09-02 10:00:00 UTC"}]}]
                + ([{"id": base + 2, "approve_status": "approved", "updated_at": "2026-09-05 10:00:30 UTC",
                     "created_at": "2026-09-05 10:00:00 UTC", "user_name": "Some Person", "new_margin": 30,
                     "change_logs": [{"action": "create", "created_at": "2026-09-05 10:00:00 UTC"}]},
                    {"id": base + 3, "approve_status": "pending", "updated_at": "2026-09-06 10:00:30 UTC",
                     "new_margin": 99}] if n == N else [])}, "campaign": camp})
            write(raw / "links" / n / "brain.json", {"queried_at": "2026-10-01T00:00:00Z", "tickets": [], "channels": [],
                                                      "slack_synced_until": None})
        if not with_github:
            return
        write(raw / "links" / N / "github.json", {"queried_at": "2026-10-01T00:00:00Z", "selected": [
            {"repo": "feed-mob/feedmob", "number": 1}], "prs": [], "skipped": [], "mentions_only": []})
        write(raw / "github" / "feed-mob__feedmob" / "issue_1.json", {
            "repo": "feed-mob/feedmob", "number": 1, "prs": [], "fetched_at": "2026-10-01T00:00:00Z",
            "issue": {"html_url": "https://github.com/feed-mob/feedmob/issues/1", "title": "测试标题 [V:Summer|D:mandy]",
                      "state": "open", "labels": [{"name": "Code Review"}], "user": {"login": "alice"},
                      "created_at": "2026-09-01T08:00:00Z", "updated_at": "2026-09-03T08:00:00Z", "closed_at": None,
                      "body": "请看一下 777777 的花费"},
            "events": [{"id": 1, "event": "labeled", "created_at": "2026-09-01T09:00:00Z", "actor": {"login": "alice"},
                        "label": {"name": "Code Review"}}],
            "comments": [{"id": 5, "user": {"login": "bob"}, "created_at": "2026-09-01T10:00:00Z",
                          "body": "Looks fine, thanks @alice", "html_url": "https://github.com/x#c5"},
                         {"id": 6, "user": {"login": "bob"}, "created_at": "2026-09-01T11:00:00Z",
                          "body": "这里是中文评论", "html_url": "https://github.com/x#c6"}]})


class TestBuild(Sandbox):
    def test_builds_both_languages_without_han_in_english(self):
        self.seed()
        info = build.build(CID)
        self.assertEqual(info["events"], 6)  # 建单 + 标签 + 2 条评论 + 2 次已批准的调整（pending 的不算）
        od = self.t / "out" / CID
        zh = (od / "replay.zh.html").read_text(encoding="utf-8")
        en = (od / "replay.en.html").read_text(encoding="utf-8")
        self.assertIsNone(HAN.search(en), "英文页不能有汉字")
        self.assertTrue(HAN.search(zh))
        for page in (zh, en):
            self.assertNotIn("__CASE_DATA__", page)
            self.assertNotIn("__CASE_NO__", page)
            self.assertNotIn("\x00", page)
        case = json.loads((od / "case.en.json").read_text(encoding="utf-8"))
        self.assertEqual(case["meta"]["no"], CID)
        self.assertEqual(case["meta"]["campaign"], "Acme_CTV_US")
        self.assertEqual([e["margin"] for e in case["events"] if e["margin"] is not None], [20, 30])
        ids = [e["id"] for e in case["events"]]
        self.assertEqual(ids, sorted(ids))
        ts = [e["ts"] for e in case["events"]]
        self.assertEqual(ts, sorted(ts))

    def test_3d_view_is_in_both_pages_and_library_is_inlined(self):
        self.seed()
        build.build(CID)
        od = self.t / "out" / CID
        for name in ("replay.zh.html", "replay.en.html"):
            page = (od / name).read_text(encoding="utf-8")
            self.assertIn("function render3d()", page)
            self.assertIn("OrbitControls", page)
            self.assertNotIn("__THREE_JS__", page)
            self.assertNotIn("__ORBIT_JS__", page)
            self.assertNotIn("<script src=", page)
            encoded = re.search(r'<script id="scene-data" type="application/json">(.*?)</script>', page, flags=re.S)
            self.assertIsNotNone(encoded, "完整 3D 场景必须内嵌在单文件页面里")
            scene = json.loads(encoded.group(1))
            self.assertNotIn("FeedOS", scene)
            self.assertNotIn("fonts.googleapis.com", scene)
            self.assertNotIn("<script src=", scene)
            self.assertIn("phoneId", scene)
            self.assertIn("avatarInitials", scene)
            self.assertIn("avatar: initials", scene)
            self.assertIn("makeMember(id, p, 1.55)", scene)
            self.assertIn("id === HUB_ID ? 1.95 : 1.7", scene)
            self.assertIn("o.ring.position.copy(o.pic.position)", scene)
            self.assertIn("o.pic.position.set(pos.x, py + bob, pos.z)", scene)
            self.assertIn("m.rp = sp(m.back.tex, 0, false, 0.2)", scene)
            self.assertIn("o.rp.position.set(pos.x, -(py + bob), pos.z)", scene)
            self.assertIn("ROOM_BOUNDS", scene)
            self.assertIn("PEOPLE_BOUNDS", scene)
            self.assertIn("setReplayPlayback", scene)
            self.assertIn("frame = k < 1 ? requestAnimationFrame(draw) : 0", scene)
            self.assertIn('preserveDrawingBuffer: true', scene)
            self.assertNotIn("CAST.you", scene)
            self.assertNotIn('castN("atlas")', scene)
            if name.endswith("en.html"):
                self.assertIsNone(HAN.search(scene), "英文 3D 场景不能有汉字")
            else:
                self.assertIn("场景图形说明", scene)
                self.assertIn("：同事工作位。", scene)
                self.assertIn("计划中", scene)
                self.assertNotIn("a person at work", scene)
                self.assertNotIn("MCP server. Plug seated", scene)
                self.assertNotIn("One tile per step", scene)
                self.assertNotIn("approval gate", scene)
                self.assertNotIn("notifications planned", scene)

    def test_avatar_monograms_follow_person_names(self):
        import shutil
        import subprocess
        if shutil.which("node") is None:
            self.skipTest("本机没有 node，跳过头像缩写映射检查")
        case = {
            "lang": "en", "meta": {"no": "1", "title": "test"}, "events": [], "sources": [],
            "people": [
                {"id": "richard", "name": "0xRichardH", "short": "0xRichardH", "kind": "human", "color": "#112233"},
                {"id": "chin", "name": "fmb-chin", "short": "fmb-chin", "kind": "human", "color": "#223344"},
                {"id": "yi", "name": "Yi Han", "short": "Yi Han", "kind": "human", "color": "#334455"},
                {"id": "bot", "name": "派单机器人", "short": "派单", "kind": "bot", "color": "#445566"},
            ],
        }
        adapter = ROOT / "src" / "feedos" / "replay-data.js"
        script = (
            "global.window={REPLAY_CASE:" + json.dumps(case, ensure_ascii=False) + "};"
            "global.document={createElement:()=>({getContext:()=>({fillStyle:''})})};"
            "require(" + json.dumps(str(adapter)) + ");"
            "console.log(JSON.stringify(Object.fromEntries(Object.entries(window.CAST).map(([id,p])=>[id,{initials:p.avatar,photo:p.photo}]))));"
        )
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        avatars = json.loads(result.stdout)
        self.assertEqual({key: value["initials"] for key, value in avatars.items()}, {"richard": "R", "chin": "C", "yi": "YH", "bot": "派"})
        from urllib.parse import unquote
        from xml.etree import ElementTree
        for person in avatars.values():
            svg = ElementTree.fromstring(unquote(person["photo"].split(",", 1)[1]))
            self.assertEqual(svg.get("width"), "96", "SVG 必须声明固有宽度，防止画布裁切偏移")
            self.assertEqual(svg.get("height"), "96", "SVG 必须声明固有高度，防止画布裁切偏移")
            self.assertEqual(svg.get("viewBox"), "0 0 96 96")
            self.assertEqual(svg.find("{http://www.w3.org/2000/svg}text").text, person["initials"])

    def test_inline_scripts_are_valid_javascript(self):
        import shutil
        import subprocess
        if shutil.which("node") is None:
            self.skipTest("本机没有 node，跳过脚本语法检查")
        self.seed()
        build.build(CID)
        od = self.t / "out" / CID
        for name in ("replay.zh.html", "replay.en.html"):
            page = (od / name).read_text(encoding="utf-8")
            blocks = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*\btype=)[^>]*>(.*?)</script>", page, flags=re.S)
            self.assertTrue(blocks)
            for i, code in enumerate(blocks):
                path = self.t / f"block-{name}-{i}.js"
                path.write_text(code, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name} 第 {i + 1} 段脚本语法错误：{r.stderr[:300]}")
            encoded = re.search(r'<script id="scene-data" type="application/json">(.*?)</script>', page, flags=re.S)
            scene = json.loads(encoded.group(1))
            for i, code in enumerate(re.findall(r"<script>(.*?)</script>", scene, flags=re.S)):
                path = self.t / f"scene-{name}-{i}.js"
                path.write_text(code, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name} 的 3D 场景第 {i + 1} 段脚本语法错误：{r.stderr[:300]}")

    def test_chinese_text_is_not_invented_in_english(self):
        self.seed()
        build.build(CID)
        zh = json.loads((self.t / "out" / CID / "case.zh.json").read_text(encoding="utf-8"))
        en = json.loads((self.t / "out" / CID / "case.en.json").read_text(encoding="utf-8"))
        zq = [e["quote"] for e in zh["events"] if e["quote"]]
        eq = [e["quote"] for e in en["events"] if e["quote"]]
        self.assertIn("这里是中文评论", zq)
        self.assertIn("Looks fine, thanks @alice", eq)
        self.assertEqual(len(zq) - len(eq), 2)  # 两条中文原话（建单正文、中文评论）英文页不显示，也不翻译
        notes = [e["text"] for e in en["events"] if "Chinese" in (e["text"] or "")]
        self.assertEqual(len(notes), 2)

    def test_no_records_raises_and_keeps_old_pages(self):
        self.seed(with_github=False)
        write(self.t / "raw" / "links" / N / "femini.json", {"click_url": {
            "name": N, "campaign": {"id": int(CID), "name": "Acme"}, "partner": {"name": "P"}, "proposed_changes": []}})
        with self.assertRaises(RuntimeError):
            build.build(CID)
        self.assertFalse((self.t / "out" / CID / "replay.en.html").exists())

    def test_two_links_are_merged_into_one_campaign_timeline(self):
        self.seed(second_link=True)
        info = build.build(CID)
        self.assertEqual(info["events"], 7)  # 第一条链接 6 个 + 第二条链接的 1 次调整
        case = json.loads((self.t / "out" / CID / "case.en.json").read_text(encoding="utf-8"))
        fm = [e for e in case["events"] if e["src"] == "femini"]
        self.assertEqual(len(fm), 3)
        self.assertTrue(any(N2 in e["title"] for e in fm) and any(N in e["title"] for e in fm))
        self.assertTrue(all(e["margin"] is None for e in case["events"]), "多条链接都调过利润率时不画走势图")
        titles = [c["title"] for c in case["checks"]]
        self.assertIn("Margin chart not drawn", titles)
        self.assertIn("1 links have no GitHub data", titles)  # 第二条链接没有 GitHub 数据
        github_src = next(x for x in case["sources"] if x["id"] == "github")
        self.assertEqual(github_src["status"], "limited")

    def test_single_link_keeps_margin_chart(self):
        self.seed()
        build.build(CID)
        case = json.loads((self.t / "out" / CID / "case.en.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(e["margin"] for e in case["events"] if e["margin"] is not None), [20, 30])
        self.assertNotIn("Margin chart not drawn", [c["title"] for c in case["checks"]])

    def test_holders_follow_assign_and_close(self):
        evs = [dict(ticket="1", hop=("create", "a"), clear=[]), dict(ticket="1", hop=("assign", "b"), clear=[]),
               dict(ticket="1", hop=("request", "c"), clear=[]), dict(ticket="1", hop=("reviewed", "c"), clear=[]),
               dict(ticket="1", hop=("close",), clear=[]), dict(ticket=None, hop=None, clear=[])]
        events.apply_holders(evs)
        self.assertEqual(set(evs[1]["holders_pairs"]), {"b"})  # 派给 b 后，建单人 a 不再算手上有单
        self.assertEqual(set(evs[2]["holders_pairs"]), {"b", "c"})
        self.assertEqual(set(evs[3]["holders_pairs"]), {"b"})
        self.assertEqual(evs[4]["holders_pairs"], {})
        self.assertEqual(evs[4]["clear"], ["1"])
        self.assertIsNone(evs[5]["holders_pairs"])


class TestSmall(unittest.TestCase):
    def test_number_validation(self):
        for bad in ("abc", "../../etc", "1234567890", "12 34", "", "-123", "12;rm", "1e5"):
            with self.assertRaises(ValueError, msg=bad):
                util.valid_number(bad)
        self.assertEqual(util.valid_number(" 23959 "), "23959")

    def test_number_matching_ignores_longer_numbers_and_money(self):
        p = util.num_re("23959")
        self.assertTrue(p.search("click URL 23959 ok"))
        self.assertFalse(p.search("id 123959"))
        self.assertFalse(p.search("id 239590"))
        self.assertFalse(p.search("cost $23959"))
        self.assertFalse(p.search("1.23959"))
        q = util.issue_re("14633")
        self.assertTrue(q.search("see #14633") and q.search("issues/14633") and q.search("GH#14633"))
        self.assertFalse(q.search("#146331"))

    def test_english_title_keeps_only_latin_tags(self):
        self.assertEqual(events.en_title("Fix 修复 [V:A|D:b]"), "[V:A|D:b]")
        self.assertEqual(events.en_title("Plain English title"), "Plain English title")
        self.assertEqual(events.en_title("全是中文"), "")

    def test_acts_split_on_long_silence_and_cap_at_six(self):
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts = [(base + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ") for d in (0, 1, 2, 10, 11, 30)]
        acts = build._acts([{"ts": t} for t in ts])
        self.assertEqual(acts, [(0, 2), (3, 4), (5, 5)])
        many = [(base + timedelta(days=i * 6)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(30)]
        self.assertLessEqual(len(build._acts([{"ts": t} for t in many])), 6)


class FakeFemini:
    def __init__(self, campaigns=None, links=None, camp=None):
        self.campaigns, self.links, self.camp = campaigns, links, camp
        self.calls = []

    def call(self, name, args):
        self.calls.append(name)
        if name == "list_campaigns":
            return {"campaigns": self.campaigns}
        if name == "get_campaign":
            return {"campaign": self.camp}
        if name == "list_click_urls":
            return {"click_urls": [{"name": x} for x in self.links]}
        raise AssertionError("只允许读取类调用：" + name)


class TestCampaign(Sandbox):
    def use(self, fake):
        saved = campaign.collect_mcp.mcp_client.femini
        campaign.collect_mcp.mcp_client.femini = lambda: fake
        self.addCleanup(setattr, campaign.collect_mcp.mcp_client, "femini", saved)

    def test_list_is_sorted_and_only_reads(self):
        fake = FakeFemini(campaigns=[{"id": 2, "name": "beta"}, {"id": 1, "name": "Alpha"}, {"id": 3}])
        self.use(fake)
        self.assertEqual(campaign.refresh_list(), {"count": 2})
        self.assertEqual([c["name"] for c in campaign.load_list()["campaigns"]], ["Alpha", "beta"])
        self.assertEqual(campaign.find("2")["name"], "beta")
        self.assertIsNone(campaign.find("999"))
        self.assertEqual(fake.calls, ["list_campaigns"])

    def test_empty_list_does_not_overwrite_old_one(self):
        self.use(FakeFemini(campaigns=[{"id": 1, "name": "Alpha"}]))
        campaign.refresh_list()
        self.use(FakeFemini(campaigns=[]))
        with self.assertRaises(RuntimeError):
            campaign.refresh_list()
        self.assertEqual(len(campaign.load_list()["campaigns"]), 1)

    def test_discover_caps_links_newest_first(self):
        names = [str(100000 + i) for i in range(campaign.MAX_LINKS + 3)]
        self.use(FakeFemini(links=names, camp={"id": 7, "name": "Acme"}))
        doc = campaign.discover("7", log=lambda *_: None)
        self.assertEqual(len(doc["links"]), campaign.MAX_LINKS)
        self.assertEqual(doc["links"][0], max(names))
        self.assertEqual(len(doc["links_not_expanded"]), 3)
        self.assertEqual(campaign.links("7"), doc["links"])

    def test_campaign_without_links_fails_clearly(self):
        self.use(FakeFemini(links=[], camp={"id": 7, "name": "Acme"}))
        with self.assertRaises(RuntimeError) as cm:
            campaign.discover("7", log=lambda *_: None)
        self.assertIn("没有在用的链接", str(cm.exception))


    def test_deactivated_campaign_keeps_its_last_links(self):
        """后台停用活动后只列在用的链接（列表是空的）：沿用上次的链接照常取数，并记下已停用（2026-11-01 5023、5145）。"""
        names = [str(100000 + i) for i in range(campaign.MAX_LINKS + 2)]
        self.use(FakeFemini(links=names, camp={"id": 7, "name": "Acme", "active": True}))
        before = campaign.discover("7", log=lambda *_: None)
        self.assertFalse(campaign.inactive("7"))
        fake = FakeFemini(links=[], camp={"id": 7, "name": "Acme", "active": False})
        self.use(fake)
        doc = campaign.discover("7", log=lambda *_: None)
        self.assertEqual(doc["links"], before["links"])                      # 上次展开的、没展开的都沿用
        self.assertEqual(doc["links_not_expanded"], before["links_not_expanded"])
        self.assertTrue(doc["links_reused"])
        self.assertTrue(campaign.inactive("7"))
        self.assertEqual(campaign.links("7"), before["links"])
        self.assertEqual(fake.calls, ["get_campaign", "list_click_urls"])   # 仍然只读
        self.use(FakeFemini(links=["200000"], camp={"id": 7, "name": "Acme", "active": True}))
        again = campaign.discover("7", log=lambda *_: None)                  # 重新启用也不能丢掉已知历史链接
        self.assertEqual(again["links"][0], "200000")
        self.assertEqual(set(again["links"] + again["links_not_expanded"]), set(names + ["200000"]))
        self.assertNotIn("links_reused", again)
        self.assertFalse(campaign.inactive("7"))

    def test_deactivated_campaign_never_fetched_before_fails_clearly(self):
        self.use(FakeFemini(links=[], camp={"id": 7, "name": "Acme", "active": False}))
        with self.assertRaises(RuntimeError) as cm:
            campaign.discover("7", log=lambda *_: None)
        self.assertIn("已在后台停用", str(cm.exception))
        self.assertFalse(campaign.inactive("7"))                             # 什么都没写下

class TestPipeline(Sandbox):
    def test_server_side_steps_wait_their_turn_across_campaigns(self):
        """取数据这类不调 AI 的活儿，整台服务器一次一个活动：别人占着时要等。"""
        order = []
        saved = campaign.discover

        def discover(c, log=None):
            order.append("fetch")
            raise RuntimeError("到这里就够了")
        campaign.discover = discover
        try:
            with pipeline._server_turn():
                t = threading.Thread(target=pipeline.refresh, args=(CID,))
                t.start()
                time.sleep(0.3)
                order.append("released")                   # 另一个活动做完了它那一段
            t.join(10)
        finally:
            campaign.discover = saved
        self.assertEqual(order, ["released", "fetch"])
        self.assertFalse(pipeline.read_state(CID)["running"])

    def test_second_refresh_of_same_campaign_is_refused_while_running(self):
        lock = open(self.t / "state" / f"{CID}.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = pipeline.refresh(CID)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("busy"))

    def test_failed_refresh_is_recorded_and_keeps_last_success(self):
        old = {"ok": True, "finished_at": "2026-10-01T00:00:00Z", "steps": []}
        write(self.t / "state" / f"{CID}.json", {"number": CID, "last_ok": old, "campaign": "Acme"})
        saved = (campaign.discover, github.collect, pipeline.collect_mcp.collect)
        campaign.discover = lambda c, log=None: {"links": [N]}
        github.collect = lambda n, log=None: {"status": "skipped"}

        def boom(n, log=None):
            raise RuntimeError("后台查不到链接")
        pipeline.collect_mcp.collect = boom
        try:
            res = pipeline.refresh(CID)
        finally:
            campaign.discover, github.collect, pipeline.collect_mcp.collect = saved
        self.assertFalse(res["ok"])
        st = pipeline.read_state(CID)
        self.assertFalse(st["running"])
        self.assertFalse(st["last"]["ok"])
        self.assertEqual(st["last_ok"], old)
        self.assertEqual(st["campaign"], "Acme")
        self.assertIn(CID, pipeline.known_numbers())
        self.assertIn(CID, pipeline.refreshable_numbers())  # 成功过的，仍参与定时刷新

    def _fill_with_fakes(self, times=1, fail_business=False):
        """只补 AI：取数的函数一调就报错；AI 两步和生成页面换成假的。→ (每次结果, AI 调用顺序, 生成页面收到的状态)"""
        calls, seen = [], []

        def boom(*a, **k):
            raise AssertionError("只补 AI 不应重新取数")

        def business_build(n, log=None, ask=None):
            calls.append("business")
            if fail_business:
                raise RuntimeError("网关限流")
        mods = [(campaign, "discover"), (github, "collect"), (pipeline.collect_mcp, "collect"), (pipeline.corpus, "build"),
                (pipeline.progress, "build"), (pipeline.business, "build"), (pipeline.build, "build")]
        saved = [(m, a, getattr(m, a)) for m, a in mods]
        campaign.discover = github.collect = pipeline.collect_mcp.collect = pipeline.corpus.build = boom
        pipeline.progress.build = lambda n, log=None, ask=None: calls.append("progress")
        pipeline.business.build = business_build
        pipeline.build.build = lambda n, status=None: seen.append(status) or {"number": n}
        try:
            results = [pipeline.fill_ai(CID) for _ in range(times)]
        finally:
            for m, a, v in saved:
                setattr(m, a, v)
        return results, calls, seen

    def test_fill_ai_redoes_only_the_ai_steps_and_keeps_the_data_source_status(self):
        prior = {"ok": True, "finished_at": "2026-10-29T00:00:00Z", "steps": [
            {"step": "GitHub", "ok": True, "info": {"status": "ok", "links": 1, "failed": {}}},
            {"step": "后台与大脑", "ok": True, "info": {"status": "ok", "links": 1, "failed": {}}}]}
        write(self.t / "state" / f"{CID}.json", {"number": CID, "last": prior, "last_ok": prior, "campaign": "Acme"})
        write(self.t / "raw" / "campaigns" / CID / "corpus.json", {"records": []})
        results, calls, seen = self._fill_with_fakes(times=2)        # 第二次沿用第一次带下来的状态
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(calls, ["progress", "business"] * 2)
        expected = {"github": prior["steps"][0]["info"], "links": prior["steps"][1]["info"]}
        self.assertEqual(seen, [expected, expected])
        st = pipeline.read_state(CID)
        self.assertFalse(st["running"])
        self.assertEqual((st["last_ok"]["mode"], st["campaign"]), ("fill_ai", "Acme"))

    def test_fill_ai_failure_keeps_the_last_success_and_needs_material(self):
        prior = {"ok": True, "finished_at": "2026-10-29T00:00:00Z", "steps": []}
        write(self.t / "state" / f"{CID}.json", {"number": CID, "last_ok": prior, "campaign": "Acme"})
        results, calls, _ = self._fill_with_fakes()                     # 还没有 AI 原料
        self.assertFalse(results[0]["ok"])
        self.assertIn("原料", results[0]["error"])
        self.assertEqual(calls, [])
        write(self.t / "raw" / "campaigns" / CID / "corpus.json", {"records": []})
        results, calls, seen = self._fill_with_fakes(fail_business=True)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(len(seen), 1)                                  # 页面照样重生成（保留上次的 AI 结果）
        st = pipeline.read_state(CID)
        self.assertEqual((st["last_ok"], st["campaign"], st["running"]), (prior, "Acme", False))

    def test_one_failed_link_does_not_sink_the_campaign(self):
        status = {}

        def fn(n, log=None):
            if n == "2":
                raise RuntimeError("boom")
            return {"status": "ok"}
        out = pipeline._each(["1", "2", "3"], fn, status, "links")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(list(out["failed"]), ["2"])
        with self.assertRaises(RuntimeError):
            pipeline._each(["2"], fn, status, "links")

    def test_github_skipped_only_when_every_link_skipped(self):
        st = {}
        pipeline._each(["1", "2"], lambda n, log=None: {"status": "skipped"}, st, "github")
        self.assertEqual(st["github"]["status"], "skipped")
        pipeline._each(["1", "2"], lambda n, log=None: {"status": "skipped" if n == "1" else "ok"}, st, "github")
        self.assertEqual(st["github"]["status"], "ok")

    def test_never_successful_campaign_is_not_refreshed_daily(self):
        write(self.t / "state" / "888888.json", {"number": "888888", "last": {"ok": False}, "last_ok": None})
        self.assertIn("888888", pipeline.known_numbers())
        self.assertNotIn("888888", pipeline.refreshable_numbers())


class TestServer(Sandbox):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("replay_server", ROOT / "service" / "server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        super().setUp()
        write(self.t / "raw" / "campaign_list.json", {"queried_at": "2099-01-01T00:00:00Z", "campaigns": [
            {"id": 11, "name": "Same_Name"}, {"id": 12, "name": "Same_Name"}, {"id": 13, "name": "Other <b>"}]})
        self.server.RUNNING.clear()
        for name, value in (("OUT", self.t / "out"), ("CURATED_ROOT", self.t / "showcase")):
            self.addCleanup(setattr, self.server, name, getattr(self.server, name))
            setattr(self.server, name, value)

    def _http_request(self):
        import http.client
        import threading
        srv = self.server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(srv.shutdown)

        def request(method, path):
            connection = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            try:
                connection.request(method, path)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read().decode("utf-8")
            finally:
                connection.close()
        return request

    def test_curated_route_reads_its_own_fixed_pages(self):
        from unittest.mock import patch
        fixed = self.server.CURATED_ROOT / "chime" / "current"
        fixed.mkdir(parents=True)
        auto = self.server.OUT / "5023"
        auto.mkdir(parents=True)
        for lang in ("en", "zh"):
            (fixed / f"replay.{lang}.html").write_text(f"CURATED_{lang}")
            (auto / f"replay.{lang}.html").write_text(f"AUTOMATIC_{lang}")
        request = self._http_request()
        with patch.object(self.server, "start_refresh") as start, patch.object(pipeline, "refresh") as collect:
            for lang in ("en", "zh"):
                status, headers, body = request("GET", f"/showcase/chime/{lang}")
                self.assertEqual(status, 200)
                self.assertEqual(body, f"CURATED_{lang}")
                self.assertEqual(headers["X-Replay-Edition"], "curated")
                self.assertEqual(request("GET", f"/c/5023/{lang}/detail")[2], f"AUTOMATIC_{lang}")
            self.assertEqual(request("GET", "/showcase/chime/")[0], 303)
            self.assertEqual(request("GET", "/showcase/chime")[1]["Location"], "/showcase/chime/en")
            start.assert_not_called()
            collect.assert_not_called()

    def test_business_and_detail_editions_have_their_own_pages(self):
        out = self.server.OUT / "5023"
        out.mkdir(parents=True)
        (out / "replay.zh.html").write_text("DETAIL_zh")
        request = self._http_request()
        for lang in ("zh", "en"):
            status, headers, body = request("GET", f"/c/5023/{lang}")
            self.assertEqual(status, 404)  # 业务版还没生成：给说明页，不能退回到详细版
            self.assertNotIn("DETAIL", body)
            self.assertIn("业务版还没有生成" if lang == "zh" else "not ready yet", body)
            self.assertIn(f'href="/?lang={lang}"', body)
        self.assertFalse(any("\u3400" <= ch <= "\u9fff" for ch in request("GET", "/c/5023/en")[2]))
        (out / "business.zh.html").write_text("BUSINESS_zh")
        status, headers, body = request("GET", "/c/5023/zh")
        self.assertEqual((status, body, headers["X-Replay-Edition"]), (200, "BUSINESS_zh", "business"))
        status, headers, body = request("GET", "/c/5023/zh/detail")
        self.assertEqual((status, body, headers["X-Replay-Edition"]), (200, "DETAIL_zh", "detail"))
        self.assertEqual(request("GET", "/c/5023/zh/detail/")[2], "DETAIL_zh")
        self.assertEqual(request("GET", "/c/5023/en/detail")[0], 404)
        for path in ("/c/5023/status/detail", "/c/5023/zh/other", "/c/5023/zh/detail/x", "/c/abc/zh"):
            self.assertEqual(request("GET", path)[0], 404, path)

    def test_old_progress_address_jumps_to_the_business_tab_or_says_it_is_not_ready(self):
        request = self._http_request()
        self.assertEqual(request("GET", "/c/5023/zh/progress")[0], 404)         # 业务版还没生成
        write_text = util.out_dir("5023") / "business.en.html"
        write_text.parent.mkdir(parents=True, exist_ok=True)
        write_text.write_text("business")
        status, headers, _ = request("GET", "/c/5023/en/progress")
        self.assertEqual((status, headers["Location"]), (303, "/c/5023/en#t=progress"))
        for path in ("/c/5023/status/progress", "/c/abc/zh/progress", "/c/5023/zh/progress/x"):
            self.assertEqual(request("GET", path)[0], 404, path)

    def test_curated_version_query_reads_immutable_release_and_rejects_bad_versions(self):
        releases = self.server.CURATED_ROOT / "chime" / "releases"
        current = self.server.CURATED_ROOT / "chime" / "current"
        current.mkdir(parents=True)
        (current / "replay.zh.html").write_text("CURRENT")
        for version in ("20261016-v3", "20261016-v4"):
            release = releases / version
            release.mkdir(parents=True)
            for lang in ("en", "zh"):
                (release / f"replay.{lang}.html").write_text(f"{version}_{lang}")
        request = self._http_request()
        for version in ("20261016-v3", "20261016-v4"):
            for lang in ("en", "zh"):
                status, headers, body = request("GET", f"/showcase/chime/{lang}?v={version}")
                self.assertEqual(status, 200)
                self.assertEqual(body, f"{version}_{lang}")
                self.assertEqual(headers["X-Replay-Edition"], "curated")
        self.assertEqual(request("GET", "/showcase/chime/zh")[2], "CURRENT")
        for path in ("/showcase/chime/zh?v=../../out", "/showcase/chime/zh?v=20261016-v3&v=20261016-v4",
                     "/showcase/chime/zh?v=20261016-v9"):
            self.assertEqual(request("GET", path)[0], 404)

    def test_unpublished_curated_route_never_falls_back_to_auto(self):
        auto = self.server.OUT / "5023"
        auto.mkdir(parents=True)
        (auto / "replay.en.html").write_text("AUTOMATIC_DATA_MUST_NOT_LEAK")
        request = self._http_request()
        for path in ("/showcase/chime/en", "/showcase/chime/zh", "/showcase/chime/current/case.en.json", "/showcase/chime/../../out/5023/replay.en.html"):
            status, _, body = request("GET", path)
            self.assertEqual(status, 404)
            self.assertNotIn("AUTOMATIC_DATA", body)

    def test_curated_route_rejects_refresh_and_writes(self):
        from unittest.mock import patch
        request = self._http_request()
        with patch.object(self.server, "start_refresh") as start:
            for path in ("/showcase/chime", "/showcase/chime/en", "/showcase/chime/refresh"):
                status, headers, _ = request("POST", path)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET")
            start.assert_not_called()

    def test_there_is_no_refresh_all_and_curated_files_stay_untouched(self):
        """首页的「刷新全部」2026-11-02 按用户要求去掉了：这个入口不再存在，不会刷新任何活动，精选版也不受影响。"""
        from unittest.mock import patch
        auto = self.server.OUT / "5023"
        auto.mkdir(parents=True)
        (auto / "replay.en.html").write_text("AUTO")
        fixed = self.server.CURATED_ROOT / "chime" / "current"
        fixed.mkdir(parents=True)
        page = fixed / "replay.en.html"
        page.write_text("MANUALLY_REVIEWED_SNAPSHOT")
        request = self._http_request()
        self.assertEqual(pipeline.refreshable_numbers(), ["5023"])
        with patch.object(self.server, "start_refresh") as start:
            self.assertEqual(request("POST", "/refresh-all")[0], 404)
            start.assert_not_called()
        self.assertEqual(page.read_text(), "MANUALLY_REVIEWED_SNAPSHOT")

    def test_dropdown_options_disambiguate_duplicate_names(self):
        opts = dict(self.server.options(campaign.load_list()["campaigns"]))
        self.assertEqual(opts[13], "Other <b>")
        self.assertIn("#11", opts[11])
        self.assertIn("#12", opts[12])

    def test_home_page_has_dropdown_and_no_number_input(self):
        page = self.server.index_page()
        self.assertIn('<select name="campaign_id"', page)
        self.assertNotIn('name="number"', page)
        self.assertNotIn("<b>Other <b>", page)  # 名字里的标签不会被当成网页代码

    def test_add_only_accepts_campaigns_from_the_list(self):
        import http.client
        import threading
        srv = self.server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        started = []
        self.server.start_refresh = lambda n: started.append(n) or True
        for body, expect_start in (("campaign_id=abc", False), ("campaign_id=999", False), ("campaign_id=", False),
                                   ("campaign_id=13", True)):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            c.request("POST", "/add", body, {"Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            self.assertEqual(r.status, 303, body)
            r.read()
            self.assertEqual(bool(started), expect_start, body)
            started.clear()



class TestConcurrentWrites(unittest.TestCase):
    """几个活动同时跑时会写同一批共用文件（缓存、凭证），不能互相踩。"""

    def test_parallel_writes_to_one_file_leave_valid_json_and_no_temp_files(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "shared.json"
            errors = []

            def worker(i):
                try:
                    for j in range(40):
                        util.write_json(path, {"by": i, "n": j, "pad": "x" * 2000})
                except Exception as e:  # 以前固定的临时文件名在这里会撞：改名时找不到文件，或写进半截内容
                    errors.append(e)
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(path.read_text())["n"], 39)
            self.assertEqual([p.name for p in pathlib.Path(tmp).iterdir()], ["shared.json"])

    def test_token_refresh_reuses_a_token_another_task_just_got(self):
        from unittest.mock import patch
        from collect import mcp_client
        with tempfile.TemporaryDirectory() as tmp:
            creds = pathlib.Path(tmp) / "mcp-credentials.json"
            entry = {"url": mcp_client.BRAIN + "/mcp", "tokens": {"access_token": "new", "refresh_token": "r2"}, "clientInformation": {"client_id": "c"}}
            creds.write_text(json.dumps({"servers": {"b": entry}}))
            with patch.object(mcp_client, "CREDS", creds), \
                    patch.object(mcp_client, "_exchange_brain_token", side_effect=AssertionError("不该再换")):
                self.assertEqual(mcp_client._refresh_brain_token("old"), "new")   # 别人已经换过：直接用新的
            with patch.object(mcp_client, "CREDS", creds), \
                    patch.object(mcp_client, "_exchange_brain_token", return_value="fresh") as ex:
                self.assertEqual(mcp_client._refresh_brain_token("new"), "fresh")  # 手里的就是过期的那个：才去换
                ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
