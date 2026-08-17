# -*- coding: utf-8 -*-
"""群统计报表插件单元测试：消息统计、排行、个人统计、总览、自动日报、持久化、清理、配置防御、terminate"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

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
    # 清空构造时可能从真实 plugin_data 读到的数据
    p._stats = {}
    p._report_dates = {}
    tmp = tempfile.mkdtemp(prefix="group_stats_test_")
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
    """消息对象替身（对应 AstrBot 的 AstrBotMessage）"""

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
    """chain_result 的替身结果，text 为全部文本拼接"""

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


def chain_text(chain) -> str:
    """提取消息链中的全部文本（兼容 MessageChain 与普通列表）"""
    comps = getattr(chain, "chain", None)
    if comps is None:
        comps = [chain]
    return "".join(getattr(c, "text", "") or "" for c in comps)


class TestMessageStats(unittest.TestCase):
    """消息计入统计与机器人过滤"""

    def test_text_message_counted(self):
        p = make_plugin(now=FIXED_NOW)
        ev = FakeEvent("hello world", sender=FakeSender("1", "张三"))
        asyncio.run(p.on_msg(ev))
        rec = p._stats["123"]["2026-08-15"]["1"]
        self.assertEqual(rec["count"], 1)
        self.assertEqual(rec["chars"], 11)
        self.assertEqual(rec["images"], 0)
        self.assertEqual(rec["name"], "张三")

    def test_multi_messages_accumulate(self):
        p = make_plugin(now=FIXED_NOW)
        for text in ("早上好", "今天天气不错", "哈哈哈哈"):
            asyncio.run(p.on_msg(FakeEvent(text, sender=FakeSender("1", "张三"))))
        rec = p._stats["123"]["2026-08-15"]["1"]
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["chars"], 13)

    def test_bot_self_ignored(self):
        p = make_plugin(now=FIXED_NOW)
        ev = FakeEvent("机器人的消息", sender=FakeSender("999", "bot"), self_id="999")
        asyncio.run(p.on_msg(ev))
        self.assertEqual(p._stats, {})

    def test_other_bot_ignored_when_enabled(self):
        p = make_plugin(config={"stats_ignore_bots": True}, now=FIXED_NOW)
        ev = FakeEvent("机器人的消息", sender=FakeSender("777", "其他bot", is_bot=True))
        asyncio.run(p.on_msg(ev))
        self.assertEqual(p._stats, {})

    def test_other_bot_counted_when_disabled(self):
        p = make_plugin(config={"stats_ignore_bots": False}, now=FIXED_NOW)
        ev = FakeEvent("机器人的消息", sender=FakeSender("777", "其他bot", is_bot=True))
        asyncio.run(p.on_msg(ev))
        self.assertEqual(p._stats["123"]["2026-08-15"]["777"]["count"], 1)

    def test_image_segment_counted(self):
        p = make_plugin(now=FIXED_NOW)
        segments = [{"type": "plain", "text": "看图"}, {"type": "image", "url": "http://x/1.png"}]
        ev = FakeEvent("看图", sender=FakeSender("1", "张三"), segments=segments)
        asyncio.run(p.on_msg(ev))
        rec = p._stats["123"]["2026-08-15"]["1"]
        self.assertEqual(rec["count"], 1)
        self.assertEqual(rec["images"], 1)
        self.assertEqual(rec["chars"], 2)

    def test_command_message_not_counted(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("/群统计 今日", sender=FakeSender("1", "张三"))))
        asyncio.run(p.on_msg(FakeEvent("／群统计", sender=FakeSender("2", "李四"))))
        self.assertEqual(p._stats, {})

    def test_non_group_message_ignored(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("私聊消息", group_id="")))
        self.assertEqual(p._stats, {})

    def test_empty_text_ignored(self):
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("   ", sender=FakeSender("1", "张三"))))
        self.assertEqual(p._stats, {})


class TestRanking(unittest.TestCase):
    """今日/本周排行排序与格式化"""

    def setUp(self):
        self.p = make_plugin(now=FIXED_NOW)
        self.p._stats = {
            "123": {
                "2026-08-15": {
                    "1": {"name": "张三", "count": 5, "chars": 50, "images": 0},
                    "2": {"name": "李四", "count": 3, "chars": 30, "images": 0},
                    "3": {"name": "王五", "count": 8, "chars": 80, "images": 0},
                },
                "2026-08-10": {
                    "4": {"name": "赵六", "count": 10, "chars": 100, "images": 0},
                },
                # 过期数据不应出现在排行中
                "2026-07-01": {
                    "5": {"name": "老周", "count": 99, "chars": 999, "images": 0},
                },
            }
        }

    def test_today_ranking_sorted(self):
        text = self.p._build_ranking_text("123", "today", 10)
        self.assertIn("今日", text)
        self.assertIn("1. 王五 8 条", text)
        self.assertIn("2. 张三 5 条", text)
        self.assertIn("3. 李四 3 条", text)
        self.assertNotIn("赵六", text)  # 非今日
        self.assertNotIn("老周", text)  # 过期数据

    def test_week_ranking_sorted(self):
        text = self.p._build_ranking_text("123", "week", 10)
        self.assertIn("本周", text)
        self.assertIn("2026-08-10", text)
        self.assertIn("1. 赵六 10 条", text)  # 周一起累计
        self.assertIn("2. 王五 8 条", text)
        self.assertNotIn("老周", text)

    def test_ranking_top_n_limit(self):
        text = self.p._build_ranking_text("123", "today", 2)
        self.assertIn("1. 王五 8 条", text)
        self.assertIn("2. 张三 5 条", text)
        self.assertNotIn("李四", text)
        self.assertIn("共 3 人参与", text)

    def test_ranking_empty(self):
        text = self.p._build_ranking_text("999", "today", 10)
        self.assertIn("暂无发言记录", text)

    def test_command_parse_with_fullwidth_slash(self):
        ev = FakeEvent("／群统计 本周", sender=FakeSender("1", "张三"))
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("1. 赵六 10 条", result.text)

    def test_command_parse_without_slash(self):
        ev = FakeEvent("群统计 今日", sender=FakeSender("1", "张三"))
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("1. 王五 8 条", result.text)


class TestOverviewAndMine(unittest.TestCase):
    """总览与个人统计"""

    def setUp(self):
        self.p = make_plugin(now=FIXED_NOW)
        self.p._stats = {
            "123": {
                "2026-08-15": {
                    "1": {"name": "张三", "count": 5, "chars": 50, "images": 0},
                    "2": {"name": "李四", "count": 3, "chars": 30, "images": 1},
                    "3": {"name": "王五", "count": 8, "chars": 80, "images": 2},
                }
            }
        }

    def test_overview(self):
        ev = FakeEvent("/群统计", sender=FakeSender("1", "张三"))
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("总发言: 16 条", result.text)
        self.assertIn("参与人数: 3 人", result.text)
        self.assertIn("最活跃成员: 王五 (8 条)", result.text)

    def test_overview_no_data(self):
        ev = FakeEvent("/群统计", sender=FakeSender("1", "张三"))
        result = asyncio.run(make_plugin(now=FIXED_NOW).cmd_group_stats(ev))
        self.assertIn("总发言: 0 条", result.text)
        self.assertIn("今日暂无发言记录", result.text)

    def test_mine(self):
        ev = FakeEvent("/群统计 我的", sender=FakeSender("2", "李四"))
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("昵称: 李四", result.text)
        self.assertIn("发言: 3 条", result.text)
        self.assertIn("字符: 30 字", result.text)
        self.assertIn("图片: 1 张", result.text)

    def test_mine_no_data(self):
        ev = FakeEvent("/群统计 我的", sender=FakeSender("99", "路人"))
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("路人 今日暂无发言记录", result.text)

    def test_help_and_unknown(self):
        ev = FakeEvent("/群统计 帮助")
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("/群统计 今日", result.text)
        ev2 = FakeEvent("/群统计 乱七八糟")
        result2 = asyncio.run(self.p.cmd_group_stats(ev2))
        self.assertIn("未知参数", result2.text)

    def test_private_chat_rejected(self):
        ev = FakeEvent("/群统计", group_id="")
        result = asyncio.run(self.p.cmd_group_stats(ev))
        self.assertIn("仅支持在群聊", result.text)


class TestDailyReport(unittest.TestCase):
    """自动日报：到点触发、同日去重、次日再推、无数据不推"""

    DATA = {
        "123": {
            "2026-08-15": {
                "1": {"name": "张三", "count": 5, "chars": 50, "images": 0},
                "2": {"name": "李四", "count": 3, "chars": 30, "images": 1},
                "3": {"name": "王五", "count": 8, "chars": 80, "images": 2},
            }
        }
    }

    def _plugin(self, enable=True, time_="22:00", groups="123", now=None):
        return make_plugin(config={
            "stats_report_enable": enable,
            "stats_report_time": time_,
            "stats_report_groups": groups,
            "stats_report_platform": "default",
        }, now=now or datetime(2026, 8, 15, 22, 1, 0))

    def test_reach_time_sends_once(self):
        p = self._plugin()
        p._stats = dict(self.DATA)
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 1)
        umo, chain = p.context.sent[0]
        self.assertEqual(umo, "default:GroupMessage:123")
        text = chain_text(chain)
        self.assertIn("群日报", text)
        self.assertIn("2026-08-15", text)
        self.assertIn("总发言: 16 条", text)
        self.assertIn("参与人数: 3 人", text)
        self.assertIn("1. 王五 8 条", text)
        # 同日同群去重：再次检查不再推送
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 1)
        # 去重记录已持久化
        self.assertEqual(p._report_dates["123"], "2026-08-15")

    def test_before_time_no_send(self):
        p = self._plugin(now=datetime(2026, 8, 15, 21, 59, 0))
        p._stats = dict(self.DATA)
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 0)

    def test_next_day_sends_again(self):
        p = self._plugin(now=datetime(2026, 8, 15, 22, 1, 0))
        p._stats = dict(self.DATA)
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 1)
        # 模拟第二天
        p._now = lambda: datetime(2026, 8, 16, 22, 1, 0)
        p._stats = {"123": {"2026-08-16": {"1": {"name": "张三", "count": 1, "chars": 5, "images": 0}}}}
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 2)
        self.assertEqual(p._report_dates["123"], "2026-08-16")

    def test_disabled_no_send(self):
        p = self._plugin(enable=False)
        p._stats = dict(self.DATA)
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 0)

    def test_no_data_no_send(self):
        p = self._plugin()
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 0)

    def test_multi_groups(self):
        p = self._plugin(groups="123,456")
        p._stats = {
            "123": {"2026-08-15": {"1": {"name": "张三", "count": 1, "chars": 1, "images": 0}}},
            "456": {"2026-08-15": {"1": {"name": "李四", "count": 2, "chars": 2, "images": 0}}},
        }
        asyncio.run(p._check_and_send_reports())
        umos = [u for u, _ in p.context.sent]
        self.assertEqual(umos, ["default:GroupMessage:123", "default:GroupMessage:456"])
        self.assertEqual(p._report_dates, {"123": "2026-08-15", "456": "2026-08-15"})

    def test_bad_report_time_fallback(self):
        # 时间配置脏值：回退 22:00，22:01 仍可触发
        p = self._plugin(time_="not-a-time")
        p._stats = dict(self.DATA)
        asyncio.run(p._check_and_send_reports())
        self.assertEqual(len(p.context.sent), 1)


class TestPersistence(unittest.TestCase):
    """数据落盘与恢复"""

    DATA = {
        "123": {
            "2026-08-15": {
                "1": {"name": "张三", "count": 5, "chars": 50, "images": 0},
                "2": {"name": "李四", "count": 3, "chars": 30, "images": 1},
            }
        }
    }

    def test_save_and_reload_stats(self):
        p = make_plugin(now=FIXED_NOW)
        p._stats = json.loads(json.dumps(self.DATA))
        p._save_stats()
        path = os.path.join(p.data_dir, "stats.json")
        self.assertTrue(os.path.exists(path))
        # 新实例从同一目录恢复
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._stats = {}
        p2._report_dates = {}
        p2._load_all()
        self.assertEqual(p2._stats, self.DATA)

    def test_save_and_reload_report_dates(self):
        p = make_plugin(now=FIXED_NOW)
        p._report_dates = {"123": "2026-08-15"}
        p._save_report_dates()
        path = os.path.join(p.data_dir, "report_dates.json")
        self.assertTrue(os.path.exists(path))
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_report_dates()
        self.assertEqual(p2._report_dates, {"123": "2026-08-15"})

    def test_load_corrupted_file_reset(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "stats.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not-valid-json")
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._stats = {}
        p2._load_stats()
        self.assertEqual(p2._stats, {})

    def test_on_msg_autosave_interval(self):
        # 首次消息即触发落盘（_last_save 初始为 0），验证 on_msg 会写盘
        p = make_plugin(now=FIXED_NOW)
        asyncio.run(p.on_msg(FakeEvent("第一条消息", sender=FakeSender("1", "张三"))))
        path = os.path.join(p.data_dir, "stats.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["123"]["2026-08-15"]["1"]["count"], 1)


class TestCleanup(unittest.TestCase):
    """过期数据清理"""

    def test_cleanup_old_dates(self):
        p = make_plugin(config={"stats_keep_days": 30}, now=FIXED_NOW)
        p._stats = {
            "123": {
                "2026-08-15": {"1": {"name": "张三", "count": 5, "chars": 50, "images": 0}},
                "2026-08-01": {"2": {"name": "李四", "count": 3, "chars": 30, "images": 0}},
                "2026-07-01": {"3": {"name": "老周", "count": 99, "chars": 999, "images": 0}},
            }
        }
        p._cleanup_old()
        self.assertIn("2026-08-15", p._stats["123"])
        self.assertIn("2026-08-01", p._stats["123"])
        self.assertNotIn("2026-07-01", p._stats["123"])

    def test_cleanup_removes_empty_group(self):
        p = make_plugin(config={"stats_keep_days": 30}, now=FIXED_NOW)
        p._stats = {"123": {"2026-07-01": {"1": {"name": "老周", "count": 1, "chars": 1, "images": 0}}}}
        p._cleanup_old()
        self.assertNotIn("123", p._stats)

    def test_cleanup_keep_days_dirty_value(self):
        # keep_days 为脏值/负值时不崩溃，且至少保留 1 天
        for cfg in ({"stats_keep_days": "abc"}, {"stats_keep_days": -5}, {}):
            p = make_plugin(config=cfg, now=FIXED_NOW)
            p._stats = {
                "123": {"2026-08-15": {"1": {"name": "张三", "count": 1, "chars": 1, "images": 0}}}
            }
            p._cleanup_old()
            self.assertIn("123", p._stats)


class TestConfigDefense(unittest.TestCase):
    """配置脏值防御"""

    def test_dirty_config_guards(self):
        p = make_plugin(config={
            "stats_report_enable": "yes-ish",   # 非布尔
            "stats_report_time": None,
            "stats_report_groups": None,
            "stats_keep_days": "abc",
            "stats_ignore_bots": 0,
        }, now=FIXED_NOW)
        self.assertFalse(p._cfg_bool("stats_report_enable", False))
        self.assertEqual(p._cfg_str("stats_report_time", "22:00"), "22:00")
        self.assertEqual(p._cfg_list("stats_report_groups"), [])
        self.assertEqual(p._cfg_int("stats_keep_days", 30), 30)
        self.assertFalse(p._cfg_bool("stats_ignore_bots", True))
        # 脏配置下各流程不崩溃
        ev = FakeEvent("/群统计")
        result = asyncio.run(p.cmd_group_stats(ev))
        self.assertIn("总览", result.text)
        asyncio.run(p._check_and_send_reports())
        p._cleanup_old()

    def test_cfg_bool_string_variants(self):
        p = make_plugin(config={"a": "true", "b": "1", "c": "yes", "d": "on", "e": "off"})
        self.assertTrue(p._cfg_bool("a", False))
        self.assertTrue(p._cfg_bool("b", False))
        self.assertTrue(p._cfg_bool("c", False))
        self.assertTrue(p._cfg_bool("d", False))
        self.assertFalse(p._cfg_bool("e", True))

    def test_cfg_list_variants(self):
        p = make_plugin(config={
            "a": "123, 456 ,789",
            "b": ["1", "2"],
            "c": 123,
        })
        self.assertEqual(p._cfg_list("a"), ["123", "456", "789"])
        self.assertEqual(p._cfg_list("b"), ["1", "2"])
        self.assertEqual(p._cfg_list("c"), ["123"])

    def test_report_dates_corrupted(self):
        p = make_plugin(now=FIXED_NOW)
        path = os.path.join(p.data_dir, "report_dates.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        p2 = GroupStatsPlugin(FakeContext(), {})
        p2.data_dir = p.data_dir
        p2._load_report_dates()
        self.assertEqual(p2._report_dates, {})


class TestTerminate(unittest.TestCase):
    """terminate：取消后台任务并落盘"""

    def test_terminate_cancels_task_and_saves(self):
        async def scenario():
            p = make_plugin(now=FIXED_NOW)
            p._stats = {
                "123": {"2026-08-15": {"1": {"name": "张三", "count": 1, "chars": 5, "images": 0}}}
            }
            p._report_dates = {"123": "2026-08-15"}
            p._report_running = True
            p._report_task = asyncio.create_task(p._report_loop())
            await asyncio.sleep(0.05)
            await p.terminate()
            return p

        p = asyncio.run(scenario())
        self.assertTrue(p._report_task.cancelled())
        self.assertFalse(p._report_running)
        # 数据已落盘
        with open(os.path.join(p.data_dir, "stats.json"), "r", encoding="utf-8") as f:
            stats = json.load(f)
        self.assertEqual(stats["123"]["2026-08-15"]["1"]["count"], 1)
        with open(os.path.join(p.data_dir, "report_dates.json"), "r", encoding="utf-8") as f:
            dates = json.load(f)
        self.assertEqual(dates["123"], "2026-08-15")

    def test_terminate_without_task(self):
        async def scenario():
            p = make_plugin(now=FIXED_NOW)
            await p.terminate()
            return p

        asyncio.run(scenario())  # 不抛异常即可


if __name__ == "__main__":
    unittest.main(verbosity=1)
