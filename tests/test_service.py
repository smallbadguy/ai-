import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service import server


class ReplayHomeTests(unittest.TestCase):
    def setUp(self):
        self.campaign_list = {
            "campaigns": [{"id": "5023", "name": "Example Campaign"}],
            "queried_at": "2026-10-19T00:00:00Z",
        }
        self.patches = [
            patch.object(server, "ensure_list"),
            patch.object(server.campaign, "load_list", return_value=self.campaign_list),
            patch.object(server.pipeline, "known_numbers", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_homepage_is_available_in_chinese_and_english(self):
        zh = server.index_page()
        en = server.index_page(lang="en")
        self.assertIn('<html lang="zh-CN">', zh)
        self.assertIn("选择活动", zh)
        self.assertIn("<html lang=\"en\">", en)
        self.assertIn("Choose a campaign", en)
        self.assertIn("Build replay", en)
        self.assertNotIn("选择活动", en)
        self.assertIn('name="lang" value="en"', en)
        self.assertIn('href="/?lang=zh"', en)

    def test_homepage_messages_escape_campaign_names(self):
        en = server.index_page(msg="started", lang="en", campaign_name="<script>alert(1)</script>")
        self.assertIn("Building “&lt;script&gt;alert(1)&lt;/script&gt;”", en)
        self.assertNotIn("<script>alert(1)</script>", en)

    def test_english_homepage_has_no_chinese_ui_text(self):
        en = server.index_page(lang="en")
        self.assertFalse(any("\u3400" <= ch <= "\u9fff" for ch in en))

    def test_each_campaign_row_offers_business_and_detail_editions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "5023").mkdir()
            (out / "5023" / "replay.en.html").write_text("detail")
            camp = out / "raw"
            with patch.object(server, "OUT", out), patch.object(server, "camp_dir", lambda n: camp / n), \
                    patch.object(server.pipeline, "known_numbers", return_value=["5023"]), \
                    patch.object(server.pipeline, "read_state", return_value={}):
                only_detail = server.index_page(lang="en")
                (out / "5023" / "business.en.html").write_text("business")
                both = server.index_page(lang="en")
                zh = server.index_page(lang="zh")
        self.assertIn('href="/c/5023/en/detail"', only_detail)
        self.assertIn('href="/c/5023/zh/detail"', only_detail)
        self.assertNotIn('href="/c/5023/en"', only_detail)
        self.assertIn("Not ready", only_detail)
        self.assertIn('href="/c/5023/en"', both)
        self.assertIn('href="/c/5023/zh"', both)
        self.assertNotIn("Not ready", both)
        self.assertNotIn("/progress", both)                   # 进行中事项是业务版里的页签，首页不再单列
        self.assertIn("业务版", zh)
        self.assertIn("详细版", zh)

    def rows_page(self, state, links, lang="en"):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "5023").mkdir()
            (out / "5023" / "replay.en.html").write_text("detail")
            # 「处理中」要有活动锁作证：只有状态里的 running 标记（崩溃后残留）不算
            with patch.object(server, "OUT", out), patch.object(server.pipeline, "known_numbers", return_value=["5023"]), \
                    patch.object(server.pipeline, "read_state", return_value=state), \
                    patch.object(server, "_root_lock_busy", lambda n: bool(state.get("running"))), \
                    patch.object(server.campaign, "info", return_value={"links": links} if links is not None else None):
                return server.index_page(lang=lang), server.table_rows(lang)

    def test_rows_show_the_click_url_ids_not_the_admin_campaign_number(self):
        page, _ = self.rows_page({}, ["24390", "24388"])
        self.assertIn("<small>#24390 #24388</small>", page)       # 业务同事认 click URL ID
        self.assertNotIn("#5023", page)                           # 后台的活动编号不显示（链接地址里还在用）
        self.assertIn('href="/c/5023/en/detail"', page)
        page, _ = self.rows_page({}, None)                        # 还没取过活动信息：名字下面不写编号
        self.assertNotIn("#5023", page)

    def test_running_row_says_processing_without_a_clock(self):
        state = {"running": True, "started_at": "2026-10-30T08:36:00Z"}
        zh, _ = self.rows_page(state, ["24388"], "zh")
        en, _ = self.rows_page(state, ["24388"], "en")
        self.assertIn('<span class="badge text-bg-primary">处理中</span>', zh)
        self.assertIn('<span class="badge text-bg-primary">Processing</span>', en)
        for page in (zh, en):
            self.assertNotIn("已运行", page)
            self.assertNotIn("running for", page)
            self.assertIn("<tr data-running>", page)              # 轮询靠这个判断还要不要接着取
        done, _ = self.rows_page({"last_ok": {"finished_at": "2026-10-30T08:38:18Z"}}, ["24388"])
        self.assertNotIn("<tr data-running>", done)

    def test_campaigns_being_generated_are_listed_first(self):
        """正在生成的排在最上面（2026-10-31 PA 反馈：要往下翻到 C 开头才找到进行中的那个）；其余照旧按名字排。"""
        import tempfile
        done = {"last_ok": {"finished_at": "2026-10-30T08:38:18Z"}}
        states = {"1": done, "2": {"running": True}, "3": done}
        names = {"campaigns": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Zeta"}, {"id": 3, "name": "Beta"}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "OUT", pathlib.Path(tmp)), \
                patch.object(server.pipeline, "known_numbers", return_value=["1", "2", "3"]), \
                patch.object(server.pipeline, "read_state", side_effect=lambda n: states[n]), \
                patch.object(server, "_root_lock_busy", lambda n: bool(states[n].get("running"))), \
                patch.object(server.campaign, "load_list", return_value=names), \
                patch.object(server.campaign, "info", return_value=None):
            rows, _ = server.table_rows("en")
            order = [rows.index(f"<b>{x}</b>") for x in ("Zeta", "Alpha", "Beta")]
            self.assertEqual(order, sorted(order))
            states["2"] = done                                     # 生成完了：回到按名字排
            rows, _ = server.table_rows("en")
            order = [rows.index(f"<b>{x}</b>") for x in ("Alpha", "Beta", "Zeta")]
            self.assertEqual(order, sorted(order))

    def test_campaigns_with_only_source_data_are_not_listed(self):
        """清空重来后（只剩后台资料，没有页面和运行记录）首页是空的；跑起来的才列出来，已生成只数有页面的（2026-11-02）。"""
        import tempfile
        states = {"1": {}, "2": {"running": True, "started_at": "2026-11-02T08:44:43Z"}}
        names = {"campaigns": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Zeta"}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "OUT", pathlib.Path(tmp)), \
                patch.object(server.pipeline, "known_numbers", return_value=["1", "2"]), \
                patch.object(server.pipeline, "read_state", side_effect=lambda n: states[n]), \
                patch.object(server.campaign, "load_list", return_value=names), \
                patch.object(server.campaign, "info", return_value=None):
            rows, count = server.table_rows("zh")
            self.assertNotIn("<b>Alpha</b>", rows)
            self.assertIn("<b>Zeta</b>", rows)
            self.assertEqual(count, "0 个已生成")
            states["2"] = {}
            rows, count = server.table_rows("zh")
            self.assertIn("还没有生成过活动回放", rows)
            self.assertEqual(count, "0 个已生成")

    def test_deactivated_campaign_row_is_marked(self):
        """后台停用的活动：首页那一行的名字后面标「已停用」，悬停有说明；在用的不标（2026-11-01）。"""
        import tempfile
        info = {"links": ["23702"], "campaign": {"id": 5023, "active": False}}
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "5023").mkdir()
            (out / "5023" / "replay.en.html").write_text("detail")
            with patch.object(server, "OUT", out), patch.object(server.pipeline, "known_numbers", return_value=["5023"]), \
                    patch.object(server.pipeline, "read_state", return_value={"campaign": "Chime_CTV"}), \
                    patch.object(server.campaign, "info", return_value=info):
                zh, _ = server.table_rows("zh")
                en, _ = server.table_rows("en")
                info["campaign"]["active"] = True
                live, _ = server.table_rows("zh")
        self.assertIn('<b>Chime_CTV</b><span class="stop" title="FeedMob Admin 后台已停用此活动', zh)
        self.assertIn(">已停用</span><small>#23702</small>", zh)
        self.assertIn(">Deactivated</span>", en)
        import re
        tag = re.search(r'<span class="stop"[^>]*>[^<]*</span>', en).group(0)  # 英文行里本来就有「中文」切换链接，只查这个标签
        self.assertFalse(any("\u3400" <= ch <= "\u9fff" for ch in tag))
        self.assertNotIn('class="stop"', live)

    def test_page_polls_the_rows_while_something_is_processing(self):
        page, (rows, count) = self.rows_page({"running": True}, ["24388"], "zh")
        self.assertIn(f'<tbody id="rows">{rows}</tbody>', page)   # 轮询取回的行和首页的一样
        self.assertIn(f'<span class="count" id="built-count">{count}</span>', page)
        self.assertEqual(count, "1 个已生成")
        self.assertIn("fetch('/rows.json?lang=zh'", page)
        self.assertIn("tr[data-running]", page)
        self.assertIn("setTimeout(poll, 5000)", page)

    def test_signed_in_user_is_shown_with_a_sign_out_link(self):
        en = server.index_page(lang="en", user="liwei@feedmob.com")
        self.assertIn("liwei@feedmob.com", en)
        self.assertIn('href="/auth/logout"', en)
        self.assertNotIn('href="/auth/logout"', server.index_page(lang="en"))


class UiHints(unittest.TestCase):
    def test_first_build_duration_is_stated_in_both_languages(self):
        self.assertIn("10 minutes to 1.5 hours", server.FIRST_RUN["en"])
        self.assertIn("10 分钟到 1 个半小时", server.FIRST_RUN["zh"])

    def test_homepage_says_updates_are_manual_and_has_no_refresh_all(self):
        """2026-11-02 用户定：不再每天自动更新、去掉「刷新全部」；首页说明改成要最新就点该活动的「刷新」。"""
        with patch.object(server, "ensure_list"), patch.object(server.pipeline, "known_numbers", return_value=[]), \
                patch.object(server.campaign, "load_list", return_value={"campaigns": [], "queried_at": "2026-11-02T00:00:00Z"}):
            zh, en = server.index_page(lang="zh"), server.index_page(lang="en")
        self.assertIn("回放不会自动更新：需要最新内容时，点该活动的「刷新」", zh)
        self.assertIn("Replays do not update automatically. Press Refresh on a campaign", en)
        for page in (zh, en):
            for gone in ("/refresh-all", "刷新全部", "Refresh all", "每天自动更新", "updates daily", "依次自动开始", "start automatically"):
                self.assertNotIn(gone, page)


class PageNav(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (server.OUT, server.camp_dir)
        root = pathlib.Path(self.tmp.name)
        server.OUT = root / "out"
        server.camp_dir = lambda n: root / "raw" / n
        (server.OUT / "7").mkdir(parents=True)
        (root / "raw" / "7").mkdir(parents=True)

    def tearDown(self):
        server.OUT, server.camp_dir = self.saved
        self.tmp.cleanup()

    def test_nav_has_home_both_editions_with_the_current_one_marked_and_language(self):
        (server.OUT / "7" / "business.en.html").write_text("x")
        (server.OUT / "7" / "replay.en.html").write_text("x")
        html = server.nav_bar("7", "en", "business")
        self.assertIn('href="/?lang=en"', html)
        self.assertIn('href="/c/7/zh">中文', html)
        self.assertIn('<a href="/c/7/en" class=cur>Business</a>', html)
        self.assertIn('<a href="/c/7/en/detail">Detail</a>', html)
        self.assertIn('<a href="/c/7/zh/detail" class=cur>详细版</a>', server.nav_bar("7", "zh", "detail"))
        self.assertNotIn("/progress", html)

    def test_nav_lists_only_the_editions_that_exist(self):
        (server.OUT / "7" / "replay.en.html").write_text("x")
        html = server.nav_bar("7", "en", "detail")
        self.assertIn(">Detail<", html)
        self.assertNotIn(">Business<", html)

    def test_nav_keeps_the_page_when_switching_language(self):
        (server.OUT / "7" / "business.en.html").write_text("x")
        (server.OUT / "7" / "replay.en.html").write_text("x")
        self.assertIn('href="/c/7/en/detail">English', server.nav_bar("7", "zh", "detail"))

    def test_campaign_list_has_only_built_pages_sorted_by_name(self):
        from unittest import mock
        (server.OUT / "7" / "business.en.html").write_text("x")
        (server.OUT / "8").mkdir()
        (server.OUT / "8" / "replay.en.html").write_text("x")
        lst = {"campaigns": [{"id": 7, "name": "Zeta_CTV"}, {"id": 8, "name": "Alpha_CTV"}, {"id": 9, "name": "Beta"}]}
        with mock.patch.object(server.pipeline, "known_numbers", lambda: ["7", "8", "9"]), \
                mock.patch.object(server.campaign, "load_list", lambda: lst), \
                mock.patch.object(server.campaign, "info", lambda n: {"links": [23702]} if n == "8" else None):
            got = server.built_campaigns()
        # 带上 click URL ID（切换栏上显示它）；还没取过链接的活动给空列表
        self.assertEqual(got, [dict(id="8", name="Alpha_CTV", links=["23702"], business=False, detail=True),
                               dict(id="7", name="Zeta_CTV", links=[], business=True, detail=False)])

    def test_campaign_list_names_deactivated_campaigns_too(self):
        """停用的活动不在后台「在用」清单里：切换栏用上次记下的名称，不只显示编号。"""
        from unittest import mock
        (server.OUT / "7" / "business.en.html").write_text("x")
        with mock.patch.object(server.pipeline, "known_numbers", lambda: ["7"]), \
                mock.patch.object(server.campaign, "load_list", lambda: {"campaigns": []}), \
                mock.patch.object(server.pipeline, "read_state", lambda n: {}), \
                mock.patch.object(server.campaign, "info", lambda n: {"links": ["23702"], "campaign": {"name": "Chime_CTV_US", "active": False}}):
            got = server.built_campaigns()
        self.assertEqual([c["name"] for c in got], ["Chime_CTV_US"])

    def test_deactivated_campaign_pages_show_the_tag_next_to_the_name(self):
        """两版页面顶栏的活动名后面标「已停用」：返回页面时补上，已生成的页面不用重做；活动在用时不加。"""
        from unittest import mock
        import re
        (server.OUT / "7" / "business.en.html").write_text("x")
        page = b"<html><body><header id=top></header></body></html>"
        info = {"links": ["23702"], "campaign": {"active": False}}
        with mock.patch.object(server.campaign, "info", lambda n: info):
            zh = server.with_nav(page, "7", "zh", "business").decode()
            en = server.with_nav(page, "7", "en", "detail").decode()
            info["campaign"]["active"] = True
            live = server.with_nav(page, "7", "en", "business").decode()
        self.assertTrue(zh.index("rp-stop") < zh.index("rp-nav") < zh.index("</body>"))  # 先补标签，导航条再量顶栏的宽度
        self.assertIn('"已停用"', zh)
        self.assertIn("FeedMob Admin 后台已停用此活动", zh)
        tag = re.search(r"<style>\.rp-stop.*?</script>", en).group(0)
        self.assertIn('"Deactivated"', tag)
        self.assertFalse(any("\u3400" <= ch <= "\u9fff" for ch in tag))
        self.assertNotIn("rp-stop", live)

    def test_no_in_progress_link_without_a_business_edition(self):
        (server.OUT / "7" / "replay.en.html").write_text("x")
        (server.camp_dir("7") / "progress.json").write_text("{}")
        self.assertNotIn("progress", server.nav_bar("7", "en", "detail"))

    def test_work_in_progress_is_not_repeated_in_the_nav_bar(self):
        (server.OUT / "7" / "business.en.html").write_text("x")
        (server.OUT / "7" / "replay.en.html").write_text("x")
        (server.camp_dir("7") / "progress.json").write_text("{}")
        for current in ("business", "detail"):          # 业务版里本来就有这个页签和底部条的按钮
            self.assertNotIn("progress", server.nav_bar("7", "en", current))

    def test_with_nav_goes_before_closing_body_and_leaves_other_text_alone(self):
        out = server.with_nav(b"<html><body>hi</body></html>", "7", "en", "business").decode()
        self.assertTrue(out.index("rp-nav") < out.index("</body>"))
        self.assertEqual(server.with_nav(b"no body tag", "7", "en", "business"), b"no body tag")


if __name__ == "__main__":
    unittest.main()
