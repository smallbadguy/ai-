"""业务版（replay/business.py）的测试：假的模型，不联网。"""
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import business, corpus, progress, util  # noqa: E402

CID = "5555"
TODAY = dt.date(2026, 10, 22)
TERMS = dict(name="Acme_CTV_LG", advertiser="Acme", context=["LG"], links=["777777"], issues=[])


def slack(i, ts, who, text, why="channel"):
    return dict(kind="slack", ts=ts, channel="#acme", who=who, text=text, why=why, id=f"R{i:03d}")


def meeting(i, ts, title, text, tier="title"):
    return dict(kind="meeting", ts=ts, title=title, text=text, todos="", partial=False, tier=tier, id=f"R{i:03d}")


class Scripted:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, messages):
        self.calls.append(messages)
        a = self.answers.pop(0)
        return (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)), {"prompt_tokens": 1, "completion_tokens": 1}


def composed(events, **kw):
    """汇总现在是两种调用：每个时间窗一次（挑节点）+ 一次总框架（标题、概要、阶段、业务推进、人物分工）。"""
    meta = dict(title_zh="LG投放历程", title_en="LG campaign journey", summary_zh="概要。", summary_en="Summary.",
                chapters=[dict(name_zh="准备", name_en="Preparation", start="2026-10-01")],
                workstreams=[dict(title_zh="准备上线", title_en="Getting ready", text_zh="先准备。", text_en="Prepare first.", people=["Ann"])])
    meta.update(kw)
    return {"events": events}, meta


def ev(cand, **kw):
    base = dict(candidate=cand, action_zh="请求跟踪", action_en="Ask for a tracker", context_zh="Ann 请 Bob 准备跟踪。",
                context_en="Ann asks Bob to prepare tracking.", translation="请帮忙创建跟踪", to=["Bob"])
    base.update(kw)
    return base


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.saved = [(util, "RAW", util.RAW), (util, "OUT", util.OUT)]
        util.RAW, util.OUT = t / "raw", t / "out"

    def tearDown(self):
        for mod, name, val in self.saved:
            setattr(mod, name, val)
        self.tmp.cleanup()

    def put_corpus(self, records):
        util.write_json(util.camp_dir(CID) / "corpus.json", dict(campaign=dict(id=5555, name="Acme_CTV_LG"), terms=TERMS, records=records))


class Compose(Sandbox):
    """分时间窗汇总：每个时间段都要有代表，窗结果按内容缓存。"""

    def cands(self, n):
        return [dict(id=f"C{i:03d}", fp=f"fp{i:04d}", kind="slack", ts=f"2026-{7 + i // 40:02d}-{1 + i % 28:02d}T10:00:00Z", speaker="Ann",
                     quote=f"message number {i} about Acme", scope="case") for i in range(n)]

    def smart_ask(self):
        calls = {"window": 0, "meta": 0}

        def ask(messages):
            user = messages[1]["content"]
            if "Candidates" in user:                      # 时间窗：每窗选头两条
                calls["window"] += 1
                ids = re.findall(r"\[(C\d{3})\]", user)[:2]
                events = [ev(i, to=[]) for i in ids]
                return json.dumps({"events": events}), {"prompt_tokens": 1}
            calls["meta"] += 1
            return json.dumps(composed([])[1]), {"prompt_tokens": 1}
        return ask, calls

    def test_every_time_window_is_represented_and_chapters_follow_dates(self):
        ask, calls = self.smart_ask()
        W = business.WINDOW
        cands = self.cands(2 * W + 10)                    # 3 个窗：W + W + 10
        plan, _ = business.compose(cands, TERMS, ask, {}, log=lambda *_: None)
        self.assertEqual((calls["window"], calls["meta"]), (3, 1))
        windows = {int(e["candidate"][1:]) // W for e in plan["events"]}
        self.assertEqual(windows, {0, 1, 2})                              # 每个时间窗都有代表（不是都挤在最早的一段）
        self.assertGreaterEqual(len(plan["events"]), 6)                   # 每个窗都贡献了节点，不会前重后轻
        self.assertEqual([e["candidate"] for e in plan["events"]], sorted(e["candidate"] for e in plan["events"]))
        self.assertTrue(all(e["chapter"] == "c1" for e in plan["events"]))

    def test_only_changed_windows_are_recomposed(self):
        ask, calls = self.smart_ask()
        cache = {}
        n = 2 * business.WINDOW + 10
        cands = self.cands(n)
        business.compose(cands, TERMS, ask, cache, log=lambda *_: None)
        self.assertEqual(calls["window"], 3)
        more = cands + self.cands(n + 5)[n:]              # 末尾多了 5 条：前两个窗不变，只有最后一个窗要重算
        business.compose(more, TERMS, ask, cache, log=lambda *_: None)
        self.assertEqual(calls["window"], 4)
        business.compose(more, TERMS, ask, cache, log=lambda *_: None)
        self.assertEqual(calls["window"], 4)              # 完全没变就一次窗都不调（总框架照常重写）

    def test_chapter_start_dates_decide_each_moments_chapter(self):
        ask, _ = self.smart_ask()
        cands = self.cands(130)
        original = composed([])[1]
        original["chapters"] = [dict(name_zh="一", name_en="One", start="2026-07-01"), dict(name_zh="二", name_en="Two", start="2026-08-01")]

        def ask2(messages):
            if "Candidates" not in messages[1]["content"]:
                return json.dumps(original), {}
            return ask(messages)
        plan, _ = business.compose(cands, TERMS, ask2, {}, log=lambda *_: None)
        by_id = {c["id"]: c for c in cands}
        for e in plan["events"]:
            self.assertEqual(e["chapter"], "c2" if by_id[e["candidate"]]["ts"][:10] >= "2026-08-01" else "c1")


class Attribution(unittest.TestCase):
    def cand(self, i, ts, quote, source="#acme", scope="case"):
        return dict(id=f"C{i:03d}", fp=f"fp{i}", kind="slack", ts=ts, speaker="Ann", quote=quote, scope=scope, source=source)

    def test_gate_keeps_named_messages_and_immediate_replies_only(self):
        filler = [self.cand(i, f"2026-10-20T10:{i:02d}:00Z", f"filler {i}") for i in range(10)]      # 没点到特征词、也不是回复
        named = self.cand(30, "2026-10-21T09:00:00Z", "please set up the LG deal")                  # 点到特征词
        reply = self.cand(31, "2026-10-21T09:10:00Z", "done, sending now")                          # 同一频道 10 分钟内的回复
        late = self.cand(32, "2026-10-21T12:00:00Z", "Samsung needs a bigger budget")               # 3 小时后，没点到：丢
        other = self.cand(33, "2026-10-21T09:05:00Z", "ok great", source="#other")                  # 别的频道：丢
        extra = [self.cand(100 + i, f"2026-10-22T10:{i:02d}:00Z", f"LG item {i}") for i in range(14)]  # 凑够数量，越过「太少不启用」
        allc = filler + [named, reply, late, other] + extra
        by = {business._ckey(c): c for c in allc}
        chosen = [dict(ckey=business._ckey(c)) for c in allc]
        kept = business.attributable(chosen, allc, by, TERMS, log=lambda *_: None)
        got = {by[e["ckey"]]["quote"] for e in kept}
        self.assertIn("please set up the LG deal", got)
        self.assertIn("done, sending now", got)
        self.assertNotIn("Samsung needs a bigger budget", got)
        self.assertNotIn("ok great", got)
        self.assertFalse(any(q.startswith("filler") for q in got))
        self.assertEqual(len(got), 16)

    def test_meeting_turn_counts_when_the_talk_around_it_is_about_this_platform(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = util.RAW
        util.RAW = pathlib.Path(tmp.name)
        self.addCleanup(setattr, util, "RAW", saved)
        util.write_json(util.list_file(), dict(campaigns=[dict(name="Acme_CTV_LG_Freewheel"), dict(name="Acme_CTV_Samsung_Freewheel")]))
        terms = dict(name="Acme_CTV_LG_Freewheel", advertiser="Acme", context=["LG", "Freewheel"], links=[], issues=[])
        lines = ["**Ann** (10:00:00): ok next one, the Acme LG deal",
                 "**Bob** (10:00:10): we should raise the bid a bit"]                      # 前一行在说 LG：算
        lines += [f"**Eve** (10:01:{i:02d}): filler {i}" for i in range(10)]
        lines += ["**Cat** (10:05:00): Freewheel LI 79 for Samsung",                         # 共用的 Freewheel + 别的平台
                  "**Dan** (10:05:10): their budget is too small"]                          # 离 Samsung 更近：不算
        rec = dict(kind="meeting", text="\n".join(lines))
        meet = [dict(id="C900", fp="mtg", kind="meeting", ts="2026-10-21T10:00:10Z", speaker="Bob", quote="we should raise the bid a bit",
                     scope="case", source="Weekly"),
                dict(id="C901", fp="mtg", kind="meeting", ts="2026-10-21T10:05:10Z", speaker="Dan", quote="their budget is too small",
                     scope="case", source="Weekly")]
        extra = [self.cand(100 + i, f"2026-10-22T10:{i:02d}:00Z", f"LG item {i}") for i in range(15)]   # 凑够数量，越过「太少不启用」
        allc = meet + extra
        by = {business._ckey(c): c for c in allc}
        kept = business.attributable([dict(ckey=business._ckey(c)) for c in allc], allc, by, terms, log=lambda *_: None, meetings={"mtg": rec})
        got = {by[e["ckey"]]["quote"] for e in kept}
        self.assertIn("we should raise the bid a bit", got)
        self.assertNotIn("their budget is too small", got)
        rec["text"] = rec["text"].replace("the Acme LG deal", "the LG deal")                     # 没提到客户、标题也没有：不算
        kept = business.attributable([dict(ckey=business._ckey(c)) for c in allc], allc, by, terms, log=lambda *_: None, meetings={"mtg": rec})
        self.assertNotIn("we should raise the bid a bit", {by[e["ckey"]]["quote"] for e in kept})
        rec["title"] = "Acme <> FeedMob weekly"                                                   # 标题有客户名：算
        kept = business.attributable([dict(ckey=business._ckey(c)) for c in allc], allc, by, terms, log=lambda *_: None, meetings={"mtg": rec})
        self.assertIn("we should raise the bid a bit", {by[e["ckey"]]["quote"] for e in kept})

    def test_meeting_market_is_told_by_country_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = util.RAW
        util.RAW = pathlib.Path(tmp.name)
        self.addCleanup(setattr, util, "RAW", saved)
        util.write_json(util.list_file(), dict(campaigns=[dict(name="Acme_CTV_VN_CPM_Kayzen"), dict(name="Acme_CTV_VE_CPM_Kayzen")]))
        terms = corpus.build_terms(dict(name="Acme_CTV_VN_CPM_Kayzen", mobile_app=dict(name="Acme")), [], [], [])
        meet = dict(id="C900", fp="mtg", kind="meeting", ts="2026-10-21T10:00:10Z", speaker="Bob", quote="let us add budget there", scope="case", source="Acme sync")
        extra = [self.cand(100 + i, f"2026-10-22T10:{i:02d}:00Z", f"VN item {i}") for i in range(15)]
        allc = [meet] + extra
        by = {business._ckey(c): c for c in allc}

        def kept(first_line):
            rec = dict(kind="meeting", title="Acme sync", text=f"**Ann** (10:00:00): {first_line}\n**Bob** (10:00:10): let us add budget there")
            got = business.attributable([dict(ckey=business._ckey(c)) for c in allc], allc, by, terms, log=lambda *_: None, meetings={"mtg": rec})
            return "let us add budget there" in {by[e["ckey"]]["quote"] for e in got}
        self.assertTrue(kept("Vietnam is doing well on Kayzen"))       # 本活动的国家（VN）
        self.assertTrue(kept("越南这边量不错"))                          # 中文国家名也认
        self.assertFalse(kept("Venezuela is doing well on Kayzen"))    # 同客户别的国家：不算

    def test_meeting_with_another_client_or_naming_another_client_does_not_count(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = util.RAW
        util.RAW = pathlib.Path(tmp.name)
        self.addCleanup(setattr, util, "RAW", saved)
        util.write_json(util.list_file(), dict(campaigns=[dict(name="Acme_CTV_LG"), dict(name="Zeta_CTV_LG"), dict(name="AcmeUS_CTV_LG")]))
        meet = dict(id="C900", fp="mtg", kind="meeting", ts="2026-10-21T10:00:10Z", speaker="Bob", quote="we should raise the bid a bit",
                    scope="case", source="Weekly")
        extra = [self.cand(100 + i, f"2026-10-22T10:{i:02d}:00Z", f"LG item {i}") for i in range(15)]
        allc = [meet] + extra
        by = {business._ckey(c): c for c in allc}

        def kept(rec):
            got = business.attributable([dict(ckey=business._ckey(c)) for c in allc], allc, by, TERMS, log=lambda *_: None, meetings={"mtg": rec})
            return "we should raise the bid a bit" in {by[e["ckey"]]["quote"] for e in got}
        talk = "**Ann** (10:00:00): {}\n**Bob** (10:00:10): we should raise the bid a bit"
        self.assertTrue(kept(dict(kind="meeting", title="Weekly", text=talk.format("the Acme LG deal"))))
        self.assertFalse(kept(dict(kind="meeting", title="Zeta <> FeedMob", text=talk.format("the Acme LG deal"))))   # 和别的客户开的会
        self.assertFalse(kept(dict(kind="meeting", title="Weekly", text=talk.format("we launched Acme US on LG"))))   # 其实是 AcmeUS

    def test_gate_is_off_without_context_terms_or_when_it_would_empty_the_replay(self):
        c = self.cand(1, "2026-10-21T09:00:00Z", "nothing special")
        by = {business._ckey(c): c}
        chosen = [dict(ckey=business._ckey(c))]
        self.assertEqual(business.attributable(chosen, [c], by, dict(TERMS, context=[]), log=lambda *_: None), chosen)   # 没有特征词
        self.assertEqual(business.attributable(chosen, [c], by, TERMS, log=lambda *_: None), chosen)                     # 过滤后太少


class Locate(unittest.TestCase):
    def test_slack_quote_is_found_verbatim_and_speaker_and_time_come_from_the_record(self):
        r = slack(1, "2026-10-20T10:00:00Z", "Ann", "Hi Bob,   can you create a tracker for Acme?  thanks")
        hit = business.locate(r, "can you create a tracker for acme? thanks")
        self.assertEqual((hit["speaker"], hit["ts"]), ("Ann", "2026-10-20T10:00:00Z"))
        self.assertEqual(hit["text"], "can you create a tracker for Acme?  thanks")  # 取原文的真实片段，不是模型的写法
        self.assertIsNone(business.locate(r, "can you delete the tracker"))

    def test_meeting_turns_merge_same_speaker_and_time_is_the_turns_clock(self):
        r = meeting(2, "2026-10-13T15:28:23Z", "Acme sync",
                    "**Ara** (15:30:43): First part.\n**Ara** (15:30:50): second part of the same turn.\n**Bob** (15:31:00): Different person.")
        hit = business.locate(r, "First part. second part of the same turn.")
        self.assertEqual((hit["speaker"], hit["ts"]), ("Ara", "2026-10-13T15:30:43Z"))
        self.assertIsNone(business.locate(r, "second part of the same turn. Different person."))  # 不能跨人

    def test_meeting_past_midnight_rolls_to_next_day(self):
        r = meeting(3, "2026-10-13T23:50:00Z", "Late", "**Ara** (00:05:10): after midnight")
        self.assertEqual(business.locate(r, "after midnight")["ts"], "2026-10-14T00:05:10Z")


class Build(Sandbox):
    def corpus_records(self):
        return [slack(1, "2026-10-20T10:00:00Z", "Ann", "Bob, please create a tracker for the LG campaign"),
                slack(2, "2026-10-20T10:05:00Z", "Bob", "Tracker is ready, sending now"),
                slack(3, "2026-10-20T11:00:00Z", "Eve", "Samsung needs a new report format", why="match"),   # 共用频道、没提广告主
                slack(4, "2026-10-21T09:00:00Z", "Cat", "Acme asked us to pause the LG video", why="match"),
                meeting(5, "2026-10-21T15:00:00Z", "Weekly", "**Speaker 2** (15:01:00): Acme is live now", tier="mention")]

    def picks(self):
        return {"picks": [
            dict(record="R001", speaker="Ann", quote="please create a tracker for the LG campaign", scope="case", why="ask"),
            dict(record="R002", speaker="Bob", quote="Tracker is ready, sending now", scope="case", why="answer"),
            dict(record="R003", speaker="Eve", quote="Samsung needs a new report format", scope="case", why="x"),    # 与本客户无关 → 丢
            dict(record="R004", speaker="Cat", quote="Acme asked us to pause the LG video", scope="case", why="pause"),
            dict(record="R004", speaker="Cat", quote="Acme asked us to PAUSE and delete everything", scope="case", why="invented"),  # 查无 → 丢
            dict(record="R005", speaker="Speaker 2", quote="Acme is live now", scope="case", why="live"),                # 说话人不明 → 丢
        ]}

    def test_end_to_end_writes_both_pages_with_only_verified_originals(self):
        self.put_corpus(self.corpus_records())
        cands_ids = ["C001", "C002", "C003"]
        ask = Scripted(self.picks(), *composed([ev("C001"), ev("C002", action_zh="回复", action_en="Reply", translation="跟踪已准备好", to=["Ann"]),
                                           ev("C003", action_zh="暂停", action_en="Pause", translation="Acme 要求我们暂停 LG 视频", to=[])]))
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual((res["candidates"], res["events"]), (3, 3))
        od = util.out_dir(CID)
        zh, en = (od / "business.zh.html").read_text(encoding="utf-8"), (od / "business.en.html").read_text(encoding="utf-8")
        self.assertIn("LG投放历程", zh)
        self.assertIn("LG campaign journey", en)
        case = util.read_json(od / "business.case.en.json")
        self.assertEqual([e["original"]["speaker_label"] for e in case["events"]], ["Ann", "Bob", "Cat"])
        self.assertTrue(all(e["original"]["text"] in " ".join(r["text"] for r in self.corpus_records()) for e in case["events"]))
        self.assertEqual([e["ts"] for e in case["events"]], sorted(e["ts"] for e in case["events"]))
        self.assertNotIn("Samsung", json.dumps(case))                 # 无关内容进不来
        self.assertNotIn("delete everything", json.dumps(case))       # 编造的原话进不来
        self.assertEqual(case["meta"]["presentation_mode"], True)
        self.assertEqual(len(ask.calls), 3)                            # 1 次挑选 + 1 个时间窗 + 1 次总框架

    def test_pick_blocks_are_asked_at_the_same_time_with_the_same_result(self):
        import threading
        import time
        self.put_corpus(self.corpus_records())
        saved = business._chunks
        business._chunks = lambda recs, limit=None: [[r] for r in recs]       # 每条记录一块：5 块
        self.addCleanup(setattr, business, "_chunks", saved)
        live, peak, lock = [0], [0], threading.Lock()

        def ask(messages):
            user = messages[1]["content"]
            if user.startswith("Records"):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.05)
                with lock:
                    live[0] -= 1
                rid = re.search(r"\[(R\d{3})\]", user).group(1)
                return json.dumps({"picks": [p for p in self.picks()["picks"] if p["record"] == rid]}), {}
            if "Candidates" in user:
                return json.dumps({"events": [ev(i, to=[]) for i in re.findall(r"\[(C\d{3})\]", user)]}), {}
            return json.dumps(composed([])[1]), {}
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertGreater(peak[0], 1)                                    # 各块同时问
        self.assertEqual(res["candidates"], 3)                            # 和一块一块问的结果一样（同一组原话）

    def test_english_page_has_no_chinese_outside_original_speech(self):
        self.put_corpus(self.corpus_records())
        ask = Scripted(self.picks(), *composed([ev("C001", action_en="请求跟踪")]))   # 英文字段里混了汉字 → 这条被忽略
        with self.assertRaises(RuntimeError):                                   # 没有任何有效节点
            business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)

    def test_chinese_original_gets_english_translation_on_english_page(self):
        recs = [slack(1, "2026-10-20T10:00:00Z", "Ann", "请帮忙为 Acme 的 LG 投放创建跟踪")]
        self.put_corpus(recs)
        ask = Scripted({"picks": [dict(record="R001", speaker="Ann", quote="请帮忙为 Acme 的 LG 投放创建跟踪", scope="case", why="ask")]},
                       *composed([ev("C001", translation="Please create a tracker for Acme's LG campaign", to=[])]))
        business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        en = util.read_json(util.out_dir(CID) / "business.case.en.json")["events"][0]
        zh = util.read_json(util.out_dir(CID) / "business.case.zh.json")["events"][0]
        self.assertEqual(en["original"]["language"], "zh")
        self.assertEqual(en["translation"], "Please create a tracker for Acme's LG campaign")
        self.assertEqual(zh["translation"], "请帮忙为 Acme 的 LG 投放创建跟踪")   # 中文页：原话就是中文

    def test_rerun_without_new_records_does_not_call_the_model(self):
        self.put_corpus(self.corpus_records())
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        ask = Scripted()
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual((res["calls"], ask.calls), (0, []))
        self.assertEqual(res["events"], 1)

    def test_progress_items_become_a_tab_localized_per_language(self):
        self.put_corpus(self.corpus_records())
        util.write_json(util.camp_dir(CID) / "progress.json", dict(items=[
            dict(topic="发送报告", topic_en="Send the report", scope="this_campaign", status="in_progress", state_note="等确认", state_note_en="Awaiting confirmation",
                 next_step="周五前发出", next_step_en="Send by Friday", next_owner="Ann", owner_basis="self_commitment", stale=False, age_days=1,
                 last_activity="2026-10-21", evidence=[dict(ts="2026-10-21T09:00:00Z", speaker="Ann", quote="I will send it", check="exact")]),
            dict(topic="只有中文", topic_en="", scope="client_general", status="on_hold", next_owner="待确认", owner_basis="none", stale=True, age_days=40,
                 last_activity="2026-09-10", evidence=[dict(ts="2026-09-10T09:00:00Z", speaker="Bob", quote="先放一放", check="exact")]),
            dict(topic="别的活动的事", topic_en="Another campaign", scope="same_client", status="in_progress", next_owner="Bob", owner_basis="self_commitment",
                 stale=False, age_days=1, evidence=[dict(ts="2026-10-21T09:00:00Z", speaker="Bob", quote="I will set it up", check="exact")])], closed=[]))
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        zh = util.read_json(util.out_dir(CID) / "business.case.zh.json")["presentation"]
        en = util.read_json(util.out_dir(CID) / "business.case.en.json")["presentation"]
        self.assertEqual([i["topic"] for i in zh["progress"]["items"]], ["发送报告", "只有中文"])   # 同客户其他活动的事不放
        self.assertEqual([i["topic"] for i in en["progress"]["items"]], ["Send the report"])   # 没有干净英文的事项不进英文页
        self.assertEqual(zh["ui"]["tabs"]["progress"], "进行中事项")
        self.assertEqual(en["ui"]["tabs"]["progress"], "Work in progress")
        self.assertTrue(zh["progress"]["items"][1]["owner_unknown"])
        html = (util.out_dir(CID) / "business.en.html").read_text(encoding="utf-8")
        self.assertIn("renderProgress", html)
        self.assertIn("addEventListener('hashchange'", html)   # 旧地址跳过来的 #t=progress、同一页面里改 # 后面，都要切过去

    def test_roles_come_from_the_plan_and_same_name_accounts_merge_into_one_person(self):
        self.put_corpus(self.corpus_records())
        util.write_json(util.camp_dir(CID) / "campaign.json", dict(campaign={}, links=["777777"]))
        util.write_json(util.raw_dir("777777") / "femini.json", dict(click_url=dict(proposed_changes=[
            dict(approve_status="approved", new_margin=20, updated_at="2026-10-20T10:30:00Z", user_name="Ann")])))   # 后台账号和 Slack 同名
        p = composed([ev("C001"), ev("C002", action_zh="回复", action_en="Reply", translation="跟踪已准备好", to=["Ann"])],
                 people=[dict(name="Ann", role_zh="发起请求并调整设置", role_en="Starts requests and adjusts settings"),
                         dict(name="Bob", role_zh="准备跟踪", role_en="中文混入")])               # 英文里有汉字 → 这条角色不用
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *p), today=TODAY)
        zh = util.read_json(util.out_dir(CID) / "business.case.zh.json")
        en = util.read_json(util.out_dir(CID) / "business.case.en.json")
        self.assertEqual([x["name"] for x in zh["people"]].count("Ann"), 1)
        roles_zh = {x["name"]: x["role"] for x in zh["people"]}
        roles_en = {x["name"]: x["role"] for x in en["people"]}
        self.assertEqual(roles_zh["Ann"], "发起请求并调整设置")
        self.assertEqual(roles_en["Ann"], "Starts requests and adjusts settings")
        self.assertEqual(roles_en["Bob"], "Team member")

    def test_plan_version_bump_keeps_the_window_cache_and_only_rewrites_the_frame(self):
        self.put_corpus(self.corpus_records())
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        state = util.read_json(util.camp_dir(CID) / "business.json")
        state["plan_version"] = 0                                          # 汇总版本升级，但时间窗缓存仍然有效
        util.write_json(util.camp_dir(CID) / "business.json", state)
        ask = Scripted(composed([ev("C001")])[1])                          # 只需要总框架这一次调用
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual((res["calls"], len(ask.calls)), (1, 1))

    def test_window_version_bump_recomposes_all_windows(self):
        self.put_corpus(self.corpus_records())
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        state = util.read_json(util.camp_dir(CID) / "business.json")
        state["window_version"] = 0                                        # 时间窗提示词升级：缓存作废，窗要重新汇总
        util.write_json(util.camp_dir(CID) / "business.json", state)
        ask = Scripted(*composed([ev("C001")]))
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual((res["calls"], len(ask.calls)), (2, 2))           # 1 个窗 + 总框架，不重新挑选

    def test_changed_campaign_terms_recompose_the_frame_but_reuse_the_window_cache(self):
        self.put_corpus(self.corpus_records())
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        util.write_json(util.camp_dir(CID) / "corpus.json", dict(campaign=dict(id=5555, name="Acme_CTV_LG"), terms=dict(TERMS, context=["LG", "VN"]),
                                                                 records=self.corpus_records()))
        ask = Scripted(composed([ev("C001")])[1])                          # 只需要总框架这一次调用
        res = business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual((res["calls"], len(ask.calls)), (1, 1))

    def test_pick_version_bump_repicks_everything_but_keeps_existing_candidates(self):
        self.put_corpus(self.corpus_records())
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        state = util.read_json(util.camp_dir(CID) / "business.json")
        state["pick_version"] = 1
        util.write_json(util.camp_dir(CID) / "business.json", state)
        more = {"picks": self.picks()["picks"][:2] + [dict(record="R004", speaker="Cat", quote="Acme asked us to pause", scope="case", why="shorter span")]}
        res = business.build(CID, log=lambda *_: None, ask=Scripted(more, *composed([ev("C001")])), today=TODAY)
        self.assertEqual(res["candidates"], 4)        # 原来的 3 条保留，新挑到 1 条不同的
        self.assertEqual(res["calls"], 3)

    def test_margin_changes_from_the_admin_record_are_added_without_ai(self):
        self.put_corpus(self.corpus_records())
        util.write_json(util.camp_dir(CID) / "campaign.json", dict(campaign={}, links=["777777"]))
        util.write_json(util.raw_dir("777777") / "femini.json", dict(click_url=dict(proposed_changes=[
            dict(approve_status="approved", new_margin=50, updated_at="2026-10-20T10:30:00Z", user_name="Dan"),
            dict(approve_status="approved", new_margin=25, updated_at="2026-10-21T10:30:00Z", user_name="Dan"),
            dict(approve_status="pending", new_margin=1, updated_at="2026-10-21T11:30:00Z", user_name="Dan")])))
        business.build(CID, log=lambda *_: None, ask=Scripted(self.picks(), *composed([ev("C001")])), today=TODAY)
        case = util.read_json(util.out_dir(CID) / "business.case.en.json")
        margins = [e for e in case["events"] if e["src"] == "femini"]
        self.assertEqual([e["margin"] for e in margins], [50.0, 25.0])
        self.assertEqual(margins[1]["title"], "Margin adjusted from 50% to 25%")


if __name__ == "__main__":
    unittest.main()
