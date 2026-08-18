# -*- coding: utf-8 -*-
"""群统计报表插件新能力单元测试：热词话题、活跃时段、成就勋章、积分制活跃榜、独立持久化"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_group_stats.main import GroupStatsPlugin  # noqa: E402

# 固定测试时钟：2026-08-15（周六）中午 12:00，本周一为 2026-08-10
FIXED_NOW = datetime(2026, 8, 15, 12, 0, 0)

_tmp_dirs: list[str] = []


def tearDownModule():
    """清理所有测试产生的临时数据目录"""
    for d in _tmp_dirs:
        shutil.rmtree(d, ignore_errors=True)


def make_plugin(config=None, now=None, context=None):
    """构造插件实例：数据目录重定向到临时目录，可注入 mock 时钟"""
    p = GroupStatsPlugin(context or FakeContext(), config or {})
    p._stats = {}
    p._report_dates = {}
    p._hotwords = {}
    p._hours = {}
    p._achievements = {}
    p._points = {}
    tmp = tempfile.mkdtemp(prefix="group_stats_extra_test_")
    _tmp_dirs.append(tmp)
    p.data_dir = tmp
    if now is not None:
        p._now = lambda: now
    return p


class FakeSender:
    """消息发送者替身"""

    def __init__(self, user_id, nickname="", is_bot=False):
        self.user_id = str(user_id)
        self.nickname = nickname or str(user_id)
        self.is_bot = is_bot


class FakeMessageObj:
    """消息对象替身"""

    def __init__(self, group_id="123", self_id="999", sender=None, segments=None):
        self.group_id = str(group_id)
        self.self_id = str(self_id)
        self.sender = sender or FakeSender("1", "用户1")
        self.message = segments if segments is not None else [{"type": "plain", "text": "hello"}]


class FakeEvent:
    """AstrMessageEvent 最小替身"""

    def __init__(self, message_str="hello", group_id="123", sender=None, self_id="999",
                 segments=None):
        self.message_str = message_str
        self.message_obj = FakeMessageObj(group_id, self_id, sender, segments)

    def get_group_id(self):
        return self.message_obj.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_self_id(self):
        return self.message_obj.self_id

    def get_platform_id(self):
        return "default"

    def get_messages(self):
        return self.message_obj.message

    def chain_result(self, chain):
        return _Result(chain)


class _Result:
    """chain_result 替身结果，text 为全部文本拼接"""

    def __init__(self, chain):
        self.chain = chain

    @property
    def text(self):
        return "".join(getattr(c, "text", "") or "" for c in self.chain)


class FakeContext:
    """Context 替身：记录 send_message 调用"""

    def __init__(self):
        self.sent = []

    async def send_message(self, session, message_chain):
        self.sent.append((str(session), message_chain))
        return True


class TestHotwords(unittest.TestCase):
    """热词话题统计：分词、计数、聚合、命令"""

    def test_extract_words_length_and_stopwords(self):
        p = make_plugin(now=FIXED_NOW)
        words = p._extract_words("今天天气真不错，我们一起去公园玩")
        # 全部词长在 2-6 字之间
        for w in words:
            self.assertTrue(2 <= len(w) <= 6, f"词长越界: {w}")
        # 停用词被过滤
        self.assertNotIn("今天", words)
        self.assertNotIn("我们", words)
        # 话题词被提取
        self.assertIn("天气", words)
        self.assertIn("公园", words)

    def test_extract_words_no_cn(self):
        p = make_plugin(now=FIXED_NOW)
        self.assertEqual(p._extract_words("hello world 123"), [])

    def test_record_and_aggregate(self):
        p = make_plugin(now=FIXED_NOW)
        p._record_hotwords("123", "2026-08-15", "今天天气不错天气真好")
        p._record_hotwords("123", "2026-08-15", "天气好")
        p._record_hotwords("123", "2026-08-10", "天气好")
        agg = p._aggregate_hotwords("123", "2026-08-15", "2026-08-15")
        # 长片段滑动提取出 2 次"天气"；短片段"天气好"整体作为一个词组
        self.assertEqual(agg.get("天气", 0), 2)
        self.assertEqual(agg.get("天气好", 0), 1)
        # 区间过滤：本周（含 08-10）"天气好"合计 2 次（今日 1 次 + 08-10 1 次）
        agg_week = p._aggregate_hotwords("123", "2026-08-10", "2026-08-15")
        self.assertEqual(agg_week.get("天气好", 0), 2)

    def test_build_hotwords_text(self):
        p = make_plugin(now=FIXED_NOW)
        p._hotwords = {
            "123": {
                "2026-08-15": {"天气": 5, "公园": 3, "跑步": 1},
                "2026-08-10": {"爬山": 9},
            }
        }
        text = p._build_hotwords_text("123", "today", 10)
        self.assertIn("热词", text)
        self.assertIn("1. 天气 ×5", text)
        self.assertNotIn("爬山", text)  # 非今日
        text_week = p._build_hotwords_text("123", "week", 10)
        self.assertIn("1. 爬山 ×9", text_week)

    def test_build_hotwords_text_empty(self):
        p = make_plugin(now=FIXED_NOW)
        text = p._build_hotwords_text("999", "today", 10)
        self.assertIn("暂无热词数据", text)

    def test_command_hotwords(self):
        p = make_plugin(now=FIXED_NOW)
        p._hotwords = {"123": {"2026-08-15": {"天气": 5}}}
        ev = FakeEvent("/群统计 热词", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("1. 天气 ×5", result.text)
        ev2 = FakeEvent("/群统计 热词 本周", sender=FakeSender("1", "张三"))
        result2 = asyncio.run(p.cmd_group_stats(ev2))
        self.assertIn("本周", result2.text)

    def test_on_msg_collects_hotwords(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("今天天气不错", sender=FakeSender("1", "张三"))))
        day = p._hotwords["123"]["2026-08-15"]
        # 6 字短片段整体作为一个词组
        self.assertGreaterEqual(day.get("今天天气不错", 0), 1)
        # 同时不破坏原有统计
        self.assertEqual(p._stats["123"]["2026-08-15"]["1"]["count"], 1)


class TestHours(unittest.TestCase):
    """活跃时段分析：按小时统计、条形图、最活跃时段"""

    def test_record_hour(self):
        p = make_plugin(now=FIXED_NOW)
        p._record_hour("123", "2026-08-15", 12)
        p._record_hour("123", "2026-08-15", 12)
        p._record_hour("123", "2026-08-15", 8)
        self.assertEqual(p._hours["123"]["2026-08-15"]["12"], 2)
        self.assertEqual(p._hours["123"]["2026-08-15"]["8"], 1)

    def test_record_hour_out_of_range_ignored(self):
        p = make_plugin(now=FIXED_NOW)
        p._record_hour("123", "2026-08-15", -1)
        p._record_hour("123", "2026-08-15", 24)
        self.assertEqual(p._hours, {})

    def test_aggregate_hours_scope(self):
        p = make_plugin(now=FIXED_NOW)
        p._hours = {
            "123": {
                "2026-08-15": {"12": 3, "8": 1},
                "2026-08-10": {"20": 5},
                "2026-07-01": {"0": 99},  # 过期
            }
        }
        today = p._aggregate_hours("123", "2026-08-15", "2026-08-15")
        self.assertEqual(today, {"12": 3, "8": 1})
        week = p._aggregate_hours("123", "2026-08-10", "2026-08-15")
        self.assertEqual(week.get("20"), 5)

    def test_build_hourly_text(self):
        p = make_plugin(now=FIXED_NOW)
        p._hours = {"123": {"2026-08-15": {"12": 6, "8": 2, "20": 12}}}
        text = p._build_hourly_text("123", "today")
        self.assertIn("活跃时段", text)
        self.assertIn("12时", text)
        self.assertIn("%", text)
        self.assertIn("█", text)  # 条形图块字符
        self.assertIn("最活跃时段: 20 时", text)
        # 无发言的小时不展示
        self.assertNotIn("00时", text)

    def test_build_hourly_text_empty(self):
        p = make_plugin(now=FIXED_NOW)
        text = p._build_hourly_text("999", "today")
        self.assertIn("暂无发言记录", text)

    def test_on_msg_collects_hour(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("你好", sender=FakeSender("1", "张三"))))
        self.assertEqual(p._hours["123"]["2026-08-15"]["12"], 1)

    def test_command_hours(self):
        p = make_plugin(now=FIXED_NOW)
        p._hours = {"123": {"2026-08-15": {"12": 3}}}
        ev = FakeEvent("/群统计 时段", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("最活跃时段: 12 时", result.text)
        ev2 = FakeEvent("/群统计 时段 本周", sender=FakeSender("1", "张三"))
        result2 = asyncio.run(p.cmd_group_stats(ev2))
        self.assertIn("本周", result2.text)


class TestAchievements(unittest.TestCase):
    """成就勋章：触发判定、不重复颁发、周冠军结算、查看"""

    def test_early_bird(self):
        p = make_plugin(now=datetime(2026, 8, 15, 7, 30, 0))
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 7, 1, 0)
        self.assertIn("早起鸟", new)
        self.assertIn("早起鸟", p._achievements["123"]["1"]["badges"])

    def test_night_owl(self):
        p = make_plugin(now=datetime(2026, 8, 15, 2, 0, 0))
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 2, 1, 0)
        self.assertIn("夜猫子", new)

    def test_talkative_king_at_50(self):
        p = make_plugin(now=FIXED_NOW)
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 50, 0)
        self.assertIn("话痨王", new)
        # 再次触发不重复颁发
        new2 = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 60, 0)
        self.assertNotIn("话痨王", new2)

    def test_image_king_at_10(self):
        p = make_plugin(now=FIXED_NOW)
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 10, 10)
        self.assertIn("图霸", new)

    def test_persistent_star_consecutive_days(self):
        p = make_plugin(now=FIXED_NOW)
        # 构造 2026-08-09 ~ 08-15 连续 7 天发言记录
        days = {}
        for i in range(7):
            d = datetime(2026, 8, 15) - timedelta(days=i)
            days[d.strftime("%Y-%m-%d")] = {"1": {"name": "张三", "count": 1, "chars": 5, "images": 0}}
        p._stats = {"123": days}
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 1, 0)
        self.assertIn("坚持之星", new)

    def test_persistent_star_breaks_on_gap(self):
        p = make_plugin(now=FIXED_NOW)
        days = {
            "2026-08-15": {"1": {"name": "张三", "count": 1, "chars": 5, "images": 0}},
            "2026-08-13": {"1": {"name": "张三", "count": 1, "chars": 5, "images": 0}},  # 缺 08-14
        }
        p._stats = {"123": days}
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 1, 0)
        self.assertNotIn("坚持之星", new)

    def test_water_group_master(self):
        p = make_plugin(now=FIXED_NOW)
        p._stats = {"123": {"2026-08-15": {"1": {"name": "张三", "count": 500, "chars": 5, "images": 0}}}}
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 500, 0)
        self.assertIn("水群大师", new)

    def test_no_badge_without_trigger(self):
        p = make_plugin(now=FIXED_NOW)
        new = p._check_achievements_on_message("123", "1", "张三", "2026-08-15", 12, 1, 0)
        self.assertEqual(new, [])
        self.assertEqual(p._achievements, {})

    def test_week_champion_settle_once(self):
        p = make_plugin(now=FIXED_NOW)
        p._stats = {
            "123": {
                "2026-08-10": {"1": {"name": "张三", "count": 10, "chars": 50, "images": 0}},
                "2026-08-15": {
                    "1": {"name": "张三", "count": 5, "chars": 25, "images": 0},
                    "2": {"name": "李四", "count": 8, "chars": 40, "images": 0},
                },
            }
        }
        granted = p._settle_week_champions("123")
        self.assertEqual(len(granted), 1)
        badge, uid, name, week = granted[0]
        self.assertEqual(badge, "周冠军")
        self.assertEqual(uid, "1")  # 本周合计 15 条，第一
        self.assertEqual(week, "2026-08-10")
        # 同一周内再次结算不重复颁发
        granted2 = p._settle_week_champions("123")
        self.assertEqual(granted2, [])

    def test_week_champion_no_data(self):
        p = make_plugin(now=FIXED_NOW)
        self.assertEqual(p._settle_week_champions("123"), [])

    def test_on_msg_triggers_badge(self):
        p = make_plugin(now=datetime(2026, 8, 15, 7, 0, 0))
        asyncio.run(p.on_msg(FakeEvent("早上好", sender=FakeSender("1", "张三"))))
        self.assertIn("早起鸟", p._achievements["123"]["1"]["badges"])

    def test_build_badges_text_self_and_other(self):
        p = make_plugin(now=FIXED_NOW)
        p._achievements = {
            "123": {
                "1": {"name": "张三", "badges": {"早起鸟": "2026-08-15"}},
                "2": {"name": "李四", "badges": {"夜猫子": "2026-08-14"}},
            }
        }
        ev = FakeEvent("/群统计 勋章", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("勋章墙", result.text)
        self.assertIn("早起鸟", result.text)
        # 查看他人
        ev2 = FakeEvent("/群统计 勋章 2", sender=FakeSender("1", "张三"))
        result2 = asyncio.run(p.cmd_group_stats(ev2))
        self.assertIn("李四", result2.text)
        self.assertIn("夜猫子（2026-08-14 获得）", result2.text)
        # 他人没有的勋章不会以"已获得"形式出现
        self.assertNotIn("早起鸟（2026-08-15 获得）", result2.text)

    def test_build_badges_text_empty(self):
        p = make_plugin(now=FIXED_NOW)
        ev = FakeEvent("/群统计 勋章", sender=FakeSender("99", "路人"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("暂无勋章", result.text)


class TestPoints(unittest.TestCase):
    """积分制活跃榜：加分规则、每日上限、周/月榜、命令"""

    def test_text_message_plus_one(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("你好", sender=FakeSender("1", "张三"))))
        self.assertEqual(p._points["123"]["1"]["points"]["2026-08-15"], 1)

    def test_image_message_plus_three(self):
        p = make_plugin(now=FIXED_NOW)
        segments = [{"type": "plain", "text": "看图"}, {"type": "image", "url": "http://x/1.png"}]
        asyncio.run(p.on_msg(FakeEvent("看图", sender=FakeSender("1", "张三"), segments=segments)))
        # 发言 +1，图片富媒体 +2
        self.assertEqual(p._points["123"]["1"]["points"]["2026-08-15"], 3)

    def test_at_message_plus_four(self):
        p = make_plugin(now=FIXED_NOW)
        segments = [{"type": "plain", "text": "hi"}, {"type": "at", "target": "999"}]
        asyncio.run(p.on_msg(FakeEvent("hi", sender=FakeSender("1", "张三"), segments=segments)))
        # 发言 +1，被 @ +3
        self.assertEqual(p._points["123"]["1"]["points"]["2026-08-15"], 4)

    def test_rich_and_at_combined(self):
        p = make_plugin(now=FIXED_NOW)
        segments = [
            {"type": "plain", "text": "hi"},
            {"type": "image", "url": "http://x/1.png"},
            {"type": "at", "target": "999"},
        ]
        asyncio.run(p.on_msg(FakeEvent("hi", sender=FakeSender("1", "张三"), segments=segments)))
        # 1 + 2 + 3 = 6
        self.assertEqual(p._points["123"]["1"]["points"]["2026-08-15"], 6)

    def test_daily_cap_200(self):
        p = make_plugin(now=FIXED_NOW)
        p._add_points("123", "1", "张三", "2026-08-15", 150)
        p._add_points("123", "1", "张三", "2026-08-15", 100)
        self.assertEqual(p._points["123"]["1"]["points"]["2026-08-15"], 200)

    def test_negative_gain_ignored(self):
        p = make_plugin(now=FIXED_NOW)
        p._add_points("123", "1", "张三", "2026-08-15", -5)
        self.assertEqual(p._points, {})

    def test_command_message_not_counted(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("/群统计 今日", sender=FakeSender("1", "张三"))))
        self.assertEqual(p._points, {})

    def test_aggregate_points_week_month(self):
        p = make_plugin(now=FIXED_NOW)
        p._points = {
            "123": {
                "1": {"name": "张三", "points": {"2026-08-15": 30, "2026-08-10": 20, "2026-07-20": 99}},
                "2": {"name": "李四", "points": {"2026-08-15": 50}},
            }
        }
        today = p._aggregate_points("123", "2026-08-15", "2026-08-15")
        self.assertEqual(today["1"]["points"], 30)
        week = p._aggregate_points("123", "2026-08-10", "2026-08-15")
        self.assertEqual(week["1"]["points"], 50)
        month = p._aggregate_points("123", "2026-08-01", "2026-08-15")
        self.assertEqual(month["1"]["points"], 50)

    def test_build_points_text(self):
        p = make_plugin(now=FIXED_NOW)
        p._points = {
            "123": {
                "1": {"name": "张三", "points": {"2026-08-15": 30}},
                "2": {"name": "李四", "points": {"2026-08-15": 50}},
            }
        }
        text = p._build_points_text("123", "today", 10)
        self.assertIn("积分榜", text)
        self.assertIn("1. 李四 50 分", text)
        self.assertIn("2. 张三 30 分", text)

    def test_build_points_text_empty(self):
        p = make_plugin(now=FIXED_NOW)
        self.assertIn("暂无积分记录", p._build_points_text("999", "today", 10))

    def test_my_points(self):
        p = make_plugin(now=FIXED_NOW)
        p._points = {"123": {"1": {"name": "张三", "points": {"2026-08-15": 42}}}}
        ev = FakeEvent("/群统计 积分 我的", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("今日积分: 42 分", result.text)
        self.assertIn("每日上限 200", result.text)

    def test_command_points_week_month(self):
        p = make_plugin(now=FIXED_NOW)
        p._points = {
            "123": {
                "1": {"name": "张三", "points": {"2026-08-15": 30, "2026-08-10": 20}},
            }
        }
        ev = FakeEvent("/群统计 积分 本周", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("本周", result.text)
        self.assertIn("50 分", result.text)
        ev2 = FakeEvent("/群统计 积分 本月", sender=FakeSender("1", "张三"))
        result2 = asyncio.run(p.cmd_group_stats(ev2))
        self.assertIn("本月", result2.text)


class TestDailyReportExtra(unittest.TestCase):
    """日报新增段落：热词 Top5、活跃时段、积分 Top5"""

    def _plugin_with_data(self):
        p = make_plugin(now=FIXED_NOW)
        p._stats = {
            "123": {"2026-08-15": {"1": {"name": "张三", "count": 5, "chars": 50, "images": 2}}}
        }
        p._hotwords = {"123": {"2026-08-15": {"天气": 4, "公园": 2}}}
        p._hours = {"123": {"2026-08-15": {"12": 4, "8": 1}}}
        p._points = {"123": {"1": {"name": "张三", "points": {"2026-08-15": 8}}}}
        return p

    def test_daily_report_contains_extra_sections(self):
        p = self._plugin_with_data()
        text = p._build_daily_report_text("123", "2026-08-15")
        self.assertIn("群日报", text)
        self.assertIn("1. 张三 5 条", text)
        self.assertIn("热词 Top5", text)
        self.assertIn("1. 天气 ×4", text)
        self.assertIn("活跃时段", text)
        self.assertIn("12时", text)
        self.assertIn("最活跃时段: 12 时", text)
        self.assertIn("积分 Top5", text)
        self.assertIn("1. 张三 8 分", text)

    def test_daily_report_without_extra_data(self):
        # 无热词/时段/积分数据时，日报只含原有段落，不报错
        p = make_plugin(now=FIXED_NOW)
        p._stats = {
            "123": {"2026-08-15": {"1": {"name": "张三", "count": 5, "chars": 50, "images": 0}}}
        }
        text = p._build_daily_report_text("123", "2026-08-15")
        self.assertIn("群日报", text)
        self.assertNotIn("热词 Top5", text)
        self.assertNotIn("积分 Top5", text)

    def test_week_champion_settled_in_report_check(self):
        p = make_plugin(config={
            "stats_report_enable": True,
            "stats_report_time": "22:00",
            "stats_report_groups": "123",
            "stats_report_platform": "default",
        }, now=datetime(2026, 8, 15, 22, 1, 0))
        p._stats = {
            "123": {
                "2026-08-10": {"1": {"name": "张三", "count": 10, "chars": 50, "images": 0}},
                "2026-08-15": {"1": {"name": "张三", "count": 5, "chars": 25, "images": 0}},
            }
        }
        asyncio.run(p._check_and_send_reports())
        self.assertIn("周冠军", p._achievements["123"]["1"]["badges"])


class TestExtraPersistence(unittest.TestCase):
    """新数据持久化：原子写、恢复、脏数据防御、过期清理"""

    def test_save_and_reload_extras(self):
        p = make_plugin(now=FIXED_NOW)
        p._hotwords = {"123": {"2026-08-15": {"天气": 3}}}
        p._hours = {"123": {"2026-08-15": {"12": 3}}}
        p._save_extras()
        path = os.path.join(p.data_dir, "extras.json")
        self.assertTrue(os.path.exists(path))
        # 新实例恢复
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_extras()
        self.assertEqual(p2._hotwords, {"123": {"2026-08-15": {"天气": 3}}})
        self.assertEqual(p2._hours, {"123": {"2026-08-15": {"12": 3}}})

    def test_save_and_reload_achievements(self):
        p = make_plugin(now=FIXED_NOW)
        p._achievements = {"123": {"1": {"name": "张三", "badges": {"早起鸟": "2026-08-15"}}}}
        p._save_achievements()
        path = os.path.join(p.data_dir, "achievements.json")
        self.assertTrue(os.path.exists(path))
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_achievements()
        self.assertEqual(p2._achievements["123"]["1"]["badges"]["早起鸟"], "2026-08-15")

    def test_save_and_reload_points(self):
        p = make_plugin(now=FIXED_NOW)
        p._points = {"123": {"1": {"name": "张三", "points": {"2026-08-15": 42}}}}
        p._save_points()
        path = os.path.join(p.data_dir, "points.json")
        self.assertTrue(os.path.exists(path))
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_points()
        self.assertEqual(p2._points["123"]["1"]["points"]["2026-08-15"], 42)

    def test_load_corrupted_extras_reset(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "extras.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_extras()
        self.assertEqual(p2._hotwords, {})
        self.assertEqual(p2._hours, {})

    def test_load_wrong_shape_extras_reset(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "extras.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hotwords": ["not", "a", "dict"], "hours": 123}, f)
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_extras()
        self.assertEqual(p2._hotwords, {})
        self.assertEqual(p2._hours, {})

    def test_load_corrupted_achievements_reset(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "achievements.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_achievements()
        self.assertEqual(p2._achievements, {})

    def test_load_corrupted_points_reset(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "points.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("null")
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_points()
        self.assertEqual(p2._points, {})

    def test_cleanup_old_extra_data(self):
        p = make_plugin(config={"stats_keep_days": 30}, now=FIXED_NOW)
        p._hotwords = {
            "123": {"2026-08-15": {"天气": 3}, "2026-07-01": {"旧词": 9}}
        }
        p._hours = {"123": {"2026-08-15": {"12": 3}, "2026-07-01": {"0": 9}}}
        p._points = {
            "123": {"1": {"name": "张三", "points": {"2026-08-15": 3, "2026-07-01": 9}}}
        }
        p._cleanup_old()
        self.assertIn("2026-08-15", p._hotwords["123"])
        self.assertNotIn("2026-07-01", p._hotwords["123"])
        self.assertNotIn("2026-07-01", p._hours["123"])
        self.assertNotIn("2026-07-01", p._points["123"]["1"]["points"])
        self.assertIn("2026-08-15", p._points["123"]["1"]["points"])

    def test_terminate_saves_all(self):
        async def scenario():
            p = make_plugin(now=FIXED_NOW)
            p._hotwords = {"123": {"2026-08-15": {"天气": 3}}}
            p._hours = {"123": {"2026-08-15": {"12": 3}}}
            p._achievements = {"123": {"1": {"name": "张三", "badges": {"早起鸟": "2026-08-15"}}}}
            p._points = {"123": {"1": {"name": "张三", "points": {"2026-08-15": 3}}}}
            await p.terminate()
            return p

        p = asyncio.run(scenario())
        for name in ("extras.json", "achievements.json", "points.json"):
            self.assertTrue(os.path.exists(os.path.join(p.data_dir, name)), name)


class TestCommandDispatch(unittest.TestCase):
    """新命令分发：未知参数处理与别名"""

    def test_unknown_sub_args(self):
        p = make_plugin(now=FIXED_NOW)
        for cmd in ("/群统计 热词 乱七八糟", "/群统计 时段 明天", "/群统计 积分 明年"):
            ev = FakeEvent(cmd, sender=FakeSender("1", "张三"))
            result = asyncio.run(p.cmd_group_stats(ev))
            self.assertIn("未知参数", result.text)

    def test_aliases(self):
        p = make_plugin(now=FIXED_NOW)
        p._hotwords = {"123": {"2026-08-15": {"天气": 1}}}
        p._points = {"123": {"1": {"name": "张三", "points": {"2026-08-15": 5}}}}
        for cmd in ("/群统计 热词榜", "/群统计 话题"):
            ev = FakeEvent(cmd, sender=FakeSender("1", "张三"))
            result = asyncio.run(p.cmd_group_stats(ev))
            self.assertIn("热词", result.text)
        ev = FakeEvent("/群统计 活跃榜", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("积分榜", result.text)
        ev = FakeEvent("/群统计 成就", sender=FakeSender("1", "张三"))
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("勋章墙", result.text)

    def test_help_lists_new_commands(self):
        p = make_plugin(now=FIXED_NOW)
        ev = FakeEvent("/群统计 帮助")
        result = asyncio.run(p.cmd_group_stats(ev))
        for kw in ("热词", "时段", "勋章", "积分"):
            self.assertIn(kw, result.text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
