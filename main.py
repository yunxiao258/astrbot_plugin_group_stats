"""AstrBot 群统计报表插件：按群按成员统计每日发言，支持今日/本周排行与每日自动日报推送"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain, MessageEventResult
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_group_stats"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "群活跃度统计与日报/周报"
PLUGIN_VERSION = "1.0.0"

# 命令参数解析正则：兼容 /、／ 全角斜杠与无斜杠前缀
COMMAND_RE = re.compile(r"^[\/／]?\s*群统计\s*(.*)$")
# 日报时间格式校验（24 小时制 HH:MM）
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# 中文连续片段正则（热词分词用）
CN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 内置中文停用词表：常见虚词/口语词/语气词，热词提取时过滤
STOP_WORDS = frozenset("""
我们 你们 他们 咱们 自己 大家 别人 人家 这个 那个 这些 那些 这种 那种 这样 那样
什么 怎么 怎样 为啥 为什么 哪里 哪里 这里 那里 哪儿 谁呀 何时 多少
可以 没有 知道 觉得 应该 可能 因为 所以 如果 但是 而且 然后 虽然 不过 还是 就是
不是 也是 都是 还有 已经 开始 真的 非常 特别 经常 总是 一般 有点 一些 一下
现在 今天 明天 昨天 前天 后天 时候 时间 早上 晚上 中午 上午 下午 之前 之后 以后
一个 一次 一条 一块 一起 一直 一定 一样 一面 一种 一下
请问 谢谢 不好意思 没关系 哈哈哈 嘿嘿 呵呵 哈哈 嗯嗯 啊啊 呜呜 好吧 好的 好嘞
对了 其实 当然 感觉 确实 看来 好像 似乎 也许 肯定 必须 应该 能够 需要 想要
""".split())
# 热词提取的词长范围：2-6 个汉字
HOTWORD_MIN = 2
HOTWORD_MAX = 6
# 勋章规则表：(勋章名, 触发条件描述)
ACHIEVEMENT_RULES = [
    ("坚持之星", "连续 7 天发言"),
    ("话痨王", "单日发言 ≥ 50 条"),
    ("早起鸟", "6-8 点发言"),
    ("夜猫子", "0-3 点发言"),
    ("图霸", "单日图片 ≥ 10 张"),
    ("周冠军", "本周发言榜第一"),
    ("水群大师", "累计发言 ≥ 500 条"),
]

try:
    # 优先使用框架提供的 on_message（旧版 AstrBot API）
    on_message = filter.on_message
except AttributeError:
    # AstrBot v4 起改为 event_message_type 过滤器，此处回退为监听全部适配器消息
    def on_message(*args, **kwargs):
        """兼容装饰器：注册一个对所有适配器消息事件生效的 handler"""
        return filter.event_message_type(filter.EventMessageType.ALL, *args, **kwargs)


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class GroupStatsPlugin(Star):
    """群活跃度统计与日报/周报插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录：plugin_data/astrbot_plugin_group_stats（首次落盘时才创建）
        self.data_dir = self._default_data_dir()
        # 统计内存结构：group_id -> 日期(YYYY-MM-DD) -> user_id -> {name,count,chars,images}
        self._stats: dict[str, dict[str, dict[str, dict]]] = {}
        # 热词计数：group_id -> 日期 -> 词组 -> 次数（独立文件 extras.json）
        self._hotwords: dict[str, dict[str, dict[str, int]]] = {}
        # 活跃时段：group_id -> 日期 -> 小时(0-23) -> 消息数（独立文件 extras.json）
        self._hours: dict[str, dict[str, dict[str, int]]] = {}
        # 成就勋章：group_id -> user_id -> {name, badges:{勋章名: 获得日期}}（独立文件 achievements.json）
        self._achievements: dict[str, dict[str, dict]] = {}
        # 积分：group_id -> user_id -> {name, points:{日期: 当日积分}}（独立文件 points.json）
        self._points: dict[str, dict[str, dict]] = {}
        # 日报去重记录：group_id -> 已推送日期
        self._report_dates: dict[str, str] = {}
        # 群号 -> 平台实例 ID 映射（从消息事件自动学习，用于日报推送定位平台）
        self._group_platforms: dict[str, str] = {}
        # 后台日报任务
        self._report_task: asyncio.Task | None = None
        self._report_running = False
        self._last_save = 0.0
        self._load_all()
        logger.info(f"【{PLUGIN_NAME}】插件初始化完成")

    # ========== 基础工具 ==========

    def _default_data_dir(self) -> str:
        """默认数据目录（插件 data 上级的 plugin_data 目录）"""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
        )

    def _now(self) -> datetime:
        """当前时间（测试可替换以 mock 时钟）"""
        return datetime.now()

    def _today_str(self, now: datetime | None = None) -> str:
        """今天的日期字符串 YYYY-MM-DD"""
        return (now or self._now()).strftime("%Y-%m-%d")

    def _week_start_str(self, now: datetime | None = None) -> str:
        """本周周一日期字符串 YYYY-MM-DD"""
        now = now or self._now()
        return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    # ========== 配置防御（脏值容错） ==========

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        """防御性读取布尔配置"""
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            # 未识别字符串回退默认，避免误关功能
            return default
        return default

    def _cfg_int(self, key: str, default: int = 0) -> int:
        """防御性读取整数配置"""
        try:
            v = self.config.get(key, default)
            if v is None:
                return default
            return int(v)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        """防御性读取字符串配置"""
        v = self.config.get(key, default)
        if v is None:
            return default
        if isinstance(v, str):
            return v
        return str(v)

    def _cfg_list(self, key: str) -> list[str]:
        """防御性读取逗号分隔列表配置"""
        v = self.config.get(key, "")
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, (int, float)):
            return [str(v)]
        if isinstance(v, str):
            return [
                x.strip() for x in v.replace("，", ",").split(",") if x.strip()
            ]
        return []

    # ========== 数据持久化 ==========

    def _load_all(self):
        """加载全部持久化数据并清理过期统计"""
        try:
            self._load_stats()
            self._load_report_dates()
            self._load_extras()
            self._load_achievements()
            self._load_points()
            self._cleanup_old()
        except Exception as e:
            logger.warning(f"初始化统计数据失败，已重置: {e}")
            self._stats = {}
            self._report_dates = {}
            self._hotwords = {}
            self._hours = {}
            self._achievements = {}
            self._points = {}

    def _load_stats(self):
        """从磁盘加载统计数据（校验结构，损坏时重置）"""
        path = os.path.join(self.data_dir, "stats.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 嵌套结构校验：非法值（非 dict 的群/日期/成员）全部剔除，避免后续崩溃
                    cleaned = {}
                    for gid, days in data.items():
                        if not isinstance(days, dict):
                            continue
                        valid_days = {}
                        for date, members in days.items():
                            if not isinstance(date, str) or not isinstance(members, dict):
                                continue
                            valid_members = {
                                str(uid): rec
                                for uid, rec in members.items()
                                if isinstance(rec, dict)
                            }
                            if valid_members:
                                valid_days[date] = valid_members
                        if valid_days:
                            cleaned[str(gid)] = valid_days
                    self._stats = cleaned
                else:
                    logger.warning("统计数据结构异常，已重置")
        except Exception as e:
            logger.warning(f"加载统计数据失败: {e}")

    def _save_stats(self):
        """统计数据落盘（落盘前先清理过期数据；临时文件 + 原子替换防损坏）"""
        try:
            self._cleanup_old()
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "stats.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"保存统计数据失败: {e}")

    def _load_report_dates(self):
        """加载日报去重记录"""
        path = os.path.join(self.data_dir, "report_dates.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._report_dates = data
        except Exception as e:
            logger.warning(f"加载日报记录失败: {e}")

    def _save_report_dates(self):
        """持久化日报去重记录（同日同群不重复推送；临时文件 + 原子替换防损坏）"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "report_dates.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._report_dates, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"保存日报记录失败: {e}")

    def _load_extras(self):
        """加载热词与活跃时段数据（extras.json，结构校验，损坏时重置）"""
        path = os.path.join(self.data_dir, "extras.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._hotwords = self._sanitize_nested(data.get("hotwords"))
                    self._hours = self._sanitize_nested(data.get("hours"))
                else:
                    logger.warning("热词/时段数据结构异常，已重置")
        except Exception as e:
            logger.warning(f"加载热词/时段数据失败: {e}")

    @staticmethod
    def _sanitize_nested(data) -> dict:
        """清洗 group -> date -> {key: 数值} 两层嵌套结构，非法层级直接剔除"""
        if not isinstance(data, dict):
            return {}
        cleaned = {}
        for gid, dates in data.items():
            if not isinstance(dates, dict):
                continue
            valid = {
                str(d): dict(wc)
                for d, wc in dates.items()
                if isinstance(d, str) and isinstance(wc, dict)
            }
            if valid:
                cleaned[str(gid)] = valid
        return cleaned

    def _save_extras(self):
        """热词与活跃时段落盘（临时文件 + 原子替换防损坏）"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "extras.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"hotwords": self._hotwords, "hours": self._hours},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"保存热词/时段数据失败: {e}")

    def _load_achievements(self):
        """加载成就勋章数据（achievements.json，结构校验，损坏时重置）"""
        path = os.path.join(self.data_dir, "achievements.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cleaned = {}
                    for gid, users in data.items():
                        if not isinstance(users, dict):
                            continue
                        valid_users = {}
                        for uid, entry in users.items():
                            if not isinstance(entry, dict):
                                continue
                            badges = entry.get("badges")
                            if not isinstance(badges, dict):
                                badges = {}
                            valid_users[str(uid)] = {
                                "name": str(entry.get("name") or uid),
                                "badges": {str(b): str(w) for b, w in badges.items()},
                            }
                        if valid_users:
                            cleaned[str(gid)] = valid_users
                    self._achievements = cleaned
                else:
                    logger.warning("成就数据结构异常，已重置")
        except Exception as e:
            logger.warning(f"加载成就数据失败: {e}")

    def _save_achievements(self):
        """成就勋章落盘（临时文件 + 原子替换防损坏）"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "achievements.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._achievements, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"保存成就数据失败: {e}")

    def _load_points(self):
        """加载积分数据（points.json，结构校验，损坏时重置）"""
        path = os.path.join(self.data_dir, "points.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cleaned = {}
                    for gid, users in data.items():
                        if not isinstance(users, dict):
                            continue
                        valid_users = {}
                        for uid, entry in users.items():
                            if not isinstance(entry, dict):
                                continue
                            pts = entry.get("points")
                            if not isinstance(pts, dict):
                                pts = {}
                            valid_users[str(uid)] = {
                                "name": str(entry.get("name") or uid),
                                "points": {str(d): self._safe_int(v, 0) for d, v in pts.items()},
                            }
                        if valid_users:
                            cleaned[str(gid)] = valid_users
                    self._points = cleaned
                else:
                    logger.warning("积分数据结构异常，已重置")
        except Exception as e:
            logger.warning(f"加载积分数据失败: {e}")

    def _save_points(self):
        """积分落盘（临时文件 + 原子替换防损坏）"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "points.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._points, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"保存积分数据失败: {e}")

    def _cleanup_old(self):
        """清理超过 stats_keep_days 天的统计（仅内存，由调用方决定是否落盘）"""
        keep = max(1, self._cfg_int("stats_keep_days", 30))
        cutoff = (self._now() - timedelta(days=keep)).strftime("%Y-%m-%d")
        for gid in list(self._stats.keys()):
            days = self._stats[gid]
            if not isinstance(days, dict):
                # 脏数据防御：非 dict 直接丢弃该群数据
                del self._stats[gid]
                continue
            for date in list(days.keys()):
                if not isinstance(date, str) or date < cutoff:
                    del days[date]
            if not days:
                del self._stats[gid]
        # 热词/时段/积分同步清理过期日期（成就勋章永久保留）
        for store in (self._hotwords, self._hours):
            for gid in list(store.keys()):
                dates = store[gid]
                if not isinstance(dates, dict):
                    del store[gid]
                    continue
                for date in list(dates.keys()):
                    if not isinstance(date, str) or date < cutoff:
                        del dates[date]
                if not dates:
                    del store[gid]
        for gid in list(self._points.keys()):
            users = self._points[gid]
            if not isinstance(users, dict):
                del self._points[gid]
                continue
            for uid in list(users.keys()):
                entry = users[uid]
                if not isinstance(entry, dict):
                    del users[uid]
                    continue
                pts = entry.get("points")
                if isinstance(pts, dict):
                    for date in list(pts.keys()):
                        if not isinstance(date, str) or date < cutoff:
                            del pts[date]
                else:
                    entry["points"] = {}
            if not users:
                del self._points[gid]

    # ========== 消息统计 ==========

    @on_message(priority=100)
    async def on_msg(self, event: AstrMessageEvent):
        """监听群消息并计入统计（只统计普通文本消息，忽略机器人自身）"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            # 记录群所属平台实例 ID（供日报推送定位平台）
            self._group_platforms[str(group_id)] = str(event.get_platform_id() or "")
            sender_id = event.get_sender_id()
            if not sender_id:
                return
            # 忽略机器人自身
            self_id = event.get_self_id()
            if self_id and str(sender_id) == str(self_id):
                return
            # 配置开启时忽略其他机器人
            if self._cfg_bool("stats_ignore_bots", True) and self._is_bot_sender(event):
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            # 跳过命令消息：message_str 可能已被 waking_check 剥离唤醒前缀（如 "/"），
            # 需用 message_obj 保留的原始文本判断，避免把 /指令 计入普通发言
            raw = getattr(event.message_obj, "message_str", None) or text
            raw = str(raw).strip()
            if raw.startswith("/") or raw.startswith("／"):
                return
            chars = len(text)
            segments = event.get_messages() or []
            images = sum(1 for seg in segments if self._is_image_segment(seg))
            # 富媒体（图片/语音/视频等）段数量，供积分计算
            rich = sum(1 for seg in segments if self._is_rich_segment(seg))
            # 是否被 @（消息中含 @ 段），每条消息最多计一次
            at = 1 if any(self._is_at_segment(seg) for seg in segments) else 0
            name = event.get_sender_name() or sender_id
            today = self._today_str()
            gid = str(group_id)
            uid = str(sender_id)
            rec = self._stats.setdefault(gid, {}).setdefault(
                today, {}
            ).setdefault(uid, {"name": name, "count": 0, "chars": 0, "images": 0})
            rec["name"] = name
            rec["count"] += 1
            rec["chars"] += chars
            rec["images"] += images
            # —— 新能力数据收集 ——
            # 热词话题：正则分词提取词组并计数
            self._record_hotwords(gid, today, text)
            # 活跃时段：按小时（0-23）计数
            hour = int(self._now().strftime("%H"))
            self._record_hour(gid, today, hour)
            # 积分：发言 +1，富媒体每条 +2，被 @ 一次 +3（每日上限 200）
            self._add_points(gid, uid, name, today, 1 + 2 * rich + 3 * at)
            # 成就：即时规则检测（连续发言/话痨王/早起鸟/夜猫子/图霸/水群大师）
            self._check_achievements_on_message(
                gid, uid, name, today, hour, rec["count"], rec["images"]
            )
            # 定期落盘，避免每条消息都写盘（默认 5 分钟一次）
            if time.time() - self._last_save >= self._cfg_int("stats_save_interval", 300):
                self._last_save = time.time()
                self._save_stats()
                self._save_extras()
                self._save_achievements()
                self._save_points()
        except Exception as e:
            logger.warning(f"统计群消息失败: {e}")

    @staticmethod
    def _is_bot_sender(event: AstrMessageEvent) -> bool:
        """判断发送者是否为机器人（sender 带 is_bot / user_type 标记）"""
        sender = getattr(event.message_obj, "sender", None)
        if sender is None:
            return False
        if getattr(sender, "is_bot", False):
            return True
        if str(getattr(sender, "user_type", "") or "").lower() == "bot":
            return True
        return False

    @staticmethod
    def _is_image_segment(seg) -> bool:
        """判断消息段是否为图片（兼容组件对象与 dict 段）"""
        if isinstance(seg, Image):
            return True
        t = getattr(seg, "type", None)
        if t is not None:
            return str(t).lower() in ("image",)
        if isinstance(seg, dict):
            return str(seg.get("type", "")).lower() in ("image",)
        return False

    @staticmethod
    def _is_rich_segment(seg) -> bool:
        """判断消息段是否为富媒体（图片/语音/视频等，积分 +2 用）"""
        t = getattr(seg, "type", None)
        if t is not None:
            return str(t).lower() in ("image", "voice", "record", "video")
        if isinstance(seg, dict):
            return str(seg.get("type", "")).lower() in ("image", "voice", "record", "video")
        return False

    @staticmethod
    def _is_at_segment(seg) -> bool:
        """判断消息段是否为 @ 提及（被 @ 积分 +3 用）"""
        t = getattr(seg, "type", None)
        if t is not None:
            return str(t).lower() in ("at", "at_all", "mention", "mentionall")
        if isinstance(seg, dict):
            return str(seg.get("type", "")).lower() in ("at", "at_all", "mention", "mentionall")
        return False

    # ========== 统计聚合与报表 ==========

    def _aggregate(self, group_id: str, date_from: str, date_to: str) -> dict:
        """聚合 [date_from, date_to] 日期区间的成员统计数据（含脏数据防御）"""
        agg: dict[str, dict] = {}
        days = self._stats.get(group_id)
        if not isinstance(days, dict):
            return agg
        for date, members in days.items():
            if not isinstance(date, str) or not (date_from <= date <= date_to):
                continue
            if not isinstance(members, dict):
                continue
            for uid, rec in members.items():
                if not isinstance(rec, dict):
                    continue
                item = agg.setdefault(str(uid), {
                    "name": str(rec.get("name") or uid),
                    "count": 0,
                    "chars": 0,
                    "images": 0,
                })
                # 脏数据防御：字段非法（如 "abc"）按 0 处理，不拖垮整个统计
                item["count"] += self._safe_int(rec.get("count"), 0)
                item["chars"] += self._safe_int(rec.get("chars"), 0)
                item["images"] += self._safe_int(rec.get("images"), 0)
        return agg

    def _safe_int(self, value, default: int = 0) -> int:
        """安全整数转换：脏值回退默认"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ========== 热词话题统计 ==========

    def _extract_words(self, text: str) -> list[str]:
        """内置中文分词：正则提取连续中文片段，滑动窗口取 2-6 字词组，过滤停用词"""
        if not text or not CN_RUN_RE.search(text):
            return []
        words: list[str] = []
        for run in CN_RUN_RE.findall(text):
            n = len(run)
            if n < HOTWORD_MIN:
                continue
            # 长片段用滑动窗口截取 2-6 字子串，短片段直接整体
            if n <= HOTWORD_MAX:
                cands = [run]
            else:
                cands = []
                for size in range(HOTWORD_MIN, min(n, HOTWORD_MAX) + 1):
                    for i in range(0, n - size + 1):
                        cands.append(run[i:i + size])
            for w in cands:
                if w not in STOP_WORDS:
                    words.append(w)
        return words

    def _record_hotwords(self, group_id: str, date: str, text: str):
        """把一条消息的分词结果计入热词计数"""
        words = self._extract_words(text)
        if not words:
            return
        day = self._hotwords.setdefault(group_id, {}).setdefault(date, {})
        for w in words:
            day[w] = self._safe_int(day.get(w), 0) + 1

    def _aggregate_hotwords(self, group_id: str, date_from: str, date_to: str) -> dict:
        """聚合 [date_from, date_to] 区间内的热词词频（含脏数据防御）"""
        counter: dict[str, int] = {}
        data = self._hotwords.get(group_id)
        if not isinstance(data, dict):
            return counter
        for date, wc in data.items():
            if not isinstance(date, str) or not (date_from <= date <= date_to):
                continue
            if not isinstance(wc, dict):
                continue
            for w, c in wc.items():
                counter[str(w)] = counter.get(str(w), 0) + self._safe_int(c, 0)
        return counter

    def _build_hotwords_text(self, group_id: str, scope: str, top_n: int = 10) -> str:
        """构建今日/本周热词 Top 文本（scope: today / week）"""
        now = self._now()
        if scope == "week":
            start = self._week_start_str(now)
            end = self._today_str(now)
            label = f"本周 ({start} ~ {end})"
        else:
            start = end = self._today_str(now)
            label = f"今日 ({start})"
        counter = self._aggregate_hotwords(group_id, start, end)
        if not counter:
            return f"🔥 群热词 · {label}\n━━━━━━━━━━━━━━━━━━━━━━\n暂无热词数据。"
        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        lines = [f"🔥 群热词 · {label}", "━━━━━━━━━━━━━━━━━━━━━━"]
        for i, (w, c) in enumerate(ranked[:top_n], 1):
            lines.append(f"{i}. {w} ×{c}")
        if len(ranked) > top_n:
            lines.append(f"... 共 {len(ranked)} 个词组")
        return "\n".join(lines)

    # ========== 活跃时段分析 ==========

    def _record_hour(self, group_id: str, date: str, hour: int):
        """把一条消息计入对应小时（0-23）"""
        if hour < 0 or hour > 23:
            return
        day = self._hours.setdefault(group_id, {}).setdefault(date, {})
        key = str(hour)
        day[key] = self._safe_int(day.get(key), 0) + 1

    def _aggregate_hours(self, group_id: str, date_from: str, date_to: str) -> dict:
        """聚合 [date_from, date_to] 区间内的各小时发言分布（含脏数据防御）"""
        hours: dict[str, int] = {}
        data = self._hours.get(group_id)
        if not isinstance(data, dict):
            return hours
        for date, hc in data.items():
            if not isinstance(date, str) or not (date_from <= date <= date_to):
                continue
            if not isinstance(hc, dict):
                continue
            for h, c in hc.items():
                hours[str(h)] = hours.get(str(h), 0) + self._safe_int(c, 0)
        return hours

    def _build_hourly_text(self, group_id: str, scope: str, bar_max: int = 20) -> str:
        """构建今日/本周活跃时段分布文本条形图（scope: today / week）"""
        now = self._now()
        if scope == "week":
            start = self._week_start_str(now)
            end = self._today_str(now)
            label = f"本周 ({start} ~ {end})"
        else:
            start = end = self._today_str(now)
            label = f"今日 ({start})"
        agg = self._aggregate_hours(group_id, start, end)
        total = sum(agg.values())
        if not agg or total <= 0:
            return f"🕐 群活跃时段 · {label}\n━━━━━━━━━━━━━━━━━━━━━━\n暂无发言记录。"
        peak = max(agg.items(), key=lambda kv: (kv[1], kv[0]))
        lines = [f"🕐 群活跃时段 · {label}", "━━━━━━━━━━━━━━━━━━━━━━"]
        for h in range(24):
            c = agg.get(str(h), 0)
            if c <= 0:
                continue  # 只展示有发言的时段，避免刷屏
            pct = c / total * 100
            bar = "█" * max(1, round(c / peak[1] * bar_max))
            lines.append(f"{h:02d}时 {bar} {pct:.0f}%")
        lines.append(f"🔥 最活跃时段: {int(peak[0])} 时 ({peak[1]} 条)")
        return "\n".join(lines)

    # ========== 积分制活跃榜 ==========

    POINTS_DAILY_CAP = 200  # 每人每日积分上限

    def _add_points(self, group_id: str, user_id: str, name: str, date: str, gained: int):
        """累计积分（每日上限 200，超出部分丢弃）；gained <= 0 时忽略"""
        if gained <= 0:
            return
        entry = self._points.setdefault(group_id, {}).setdefault(
            user_id, {"name": name, "points": {}}
        )
        entry["name"] = name
        pts = entry.setdefault("points", {})
        cur = self._safe_int(pts.get(date), 0)
        pts[date] = min(cur + gained, self.POINTS_DAILY_CAP)

    def _aggregate_points(self, group_id: str, date_from: str, date_to: str) -> dict:
        """聚合 [date_from, date_to] 区间内各成员积分（含脏数据防御）"""
        agg: dict[str, dict] = {}
        users = self._points.get(group_id)
        if not isinstance(users, dict):
            return agg
        for uid, entry in users.items():
            if not isinstance(entry, dict):
                continue
            pts = entry.get("points")
            total = 0
            if isinstance(pts, dict):
                for date, v in pts.items():
                    if isinstance(date, str) and date_from <= date <= date_to:
                        total += self._safe_int(v, 0)
            if total > 0:
                agg[str(uid)] = {"name": str(entry.get("name") or uid), "points": total}
        return agg

    def _build_points_text(self, group_id: str, scope: str, top_n: int = 10) -> str:
        """构建今日/本周/本月积分榜文本（scope: today / week / month）"""
        now = self._now()
        today = self._today_str(now)
        if scope == "week":
            start, end, label = self._week_start_str(now), today, f"本周 ({self._week_start_str(now)} ~ {today})"
        elif scope == "month":
            start = now.strftime("%Y-%m-01")
            end = today
            label = f"本月 ({start} ~ {today})"
        else:
            start = end = today
            label = f"今日 ({start})"
        agg = self._aggregate_points(group_id, start, end)
        if not agg:
            return f"💎 群积分榜 · {label}\n━━━━━━━━━━━━━━━━━━━━━━\n暂无积分记录。"
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["points"], kv[0]))
        lines = [f"💎 群积分榜 · {label}", "━━━━━━━━━━━━━━━━━━━━━━"]
        for i, (uid, rec) in enumerate(ranked[:top_n], 1):
            lines.append(f"{i}. {rec['name']} {rec['points']} 分")
        if len(ranked) > top_n:
            lines.append(f"... 共 {len(ranked)} 人参与")
        return "\n".join(lines)

    def _build_my_points_text(self, group_id: str, event) -> str:
        """构建个人今日积分文本"""
        uid = str(event.get_sender_id())
        name = event.get_sender_name() or uid
        today = self._today_str()
        agg = self._aggregate_points(group_id, today, today)
        rec = agg.get(uid)
        if not rec:
            return f"💎 我的积分 · 今日 ({today})\n━━━━━━━━━━━━━━━━━━━━━━\n{name} 今日暂无积分。"
        return "\n".join([
            f"💎 我的积分 · 今日 ({today})",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 昵称: {rec['name']}",
            f"💰 今日积分: {rec['points']} 分（每日上限 {self.POINTS_DAILY_CAP}）",
        ])

    # ========== 成就勋章系统 ==========

    def _check_achievements_on_message(
        self, group_id: str, user_id: str, name: str, date: str,
        hour: int, count_now: int, images_now: int,
    ) -> list[str]:
        """消息到达时即时检测可触发勋章；返回本次新颁发的勋章列表，有新增才落盘"""
        # 先收集本次可授予的候选勋章，无候选则不创建空记录
        grants: list[str] = []
        # 早起鸟：6-8 点发言
        if 6 <= hour <= 8:
            grants.append("早起鸟")
        # 夜猫子：0-3 点发言
        if hour <= 3:
            grants.append("夜猫子")
        # 话痨王：单日发言 >= 50 条
        if count_now >= 50:
            grants.append("话痨王")
        # 图霸：单日图片 >= 10 张
        if images_now >= 10:
            grants.append("图霸")
        # 坚持之星：连续 7 天发言
        if self._is_consecutive_days(group_id, user_id, date, 7):
            grants.append("坚持之星")
        # 水群大师：累计发言 >= 500 条
        if self._total_count(group_id, user_id) >= 500:
            grants.append("水群大师")
        if not grants:
            return []
        badges = self._achievements.setdefault(group_id, {}).setdefault(
            user_id, {"name": name, "badges": {}}
        )["badges"]
        new_badges: list[str] = []
        for badge in grants:
            # 首次颁发：勋章不存在时才授予并记录日期
            if badge not in badges:
                badges[badge] = date
                new_badges.append(badge)
        if new_badges:
            self._save_achievements()
        return new_badges

    def _is_consecutive_days(self, group_id: str, user_id: str, date: str, days: int) -> bool:
        """判断某用户在 date 当天及往前 days-1 天是否每天都有发言"""
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return False
        for i in range(days):
            d = (day - timedelta(days=i)).strftime("%Y-%m-%d")
            members = self._stats.get(group_id, {}).get(d)
            if not isinstance(members, dict):
                return False
            rec = members.get(user_id)
            if not isinstance(rec, dict) or self._safe_int(rec.get("count"), 0) <= 0:
                return False
        return True

    def _total_count(self, group_id: str, user_id: str) -> int:
        """统计某用户全部日期累计发言条数"""
        total = 0
        days = self._stats.get(group_id)
        if not isinstance(days, dict):
            return 0
        for members in days.values():
            if not isinstance(members, dict):
                continue
            rec = members.get(user_id)
            if isinstance(rec, dict):
                total += self._safe_int(rec.get("count"), 0)
        return total

    def _settle_week_champions(self, group_id: str) -> list[tuple]:
        """结算本周周冠军（本周发言榜第一，同一周内不重复颁发）"""
        now = self._now()
        week_start = self._week_start_str(now)
        agg = self._aggregate(group_id, week_start, self._today_str(now))
        if not agg:
            return []
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        uid, rec = ranked[0]
        if self._safe_int(rec["count"], 0) <= 0:
            return []
        badges = self._achievements.setdefault(group_id, {}).setdefault(
            uid, {"name": rec["name"], "badges": {}}
        )["badges"]
        # 若本周已颁发过（日期记录为本周周一）则跳过
        if badges.get("周冠军", "") >= week_start:
            return []
        badges["周冠军"] = week_start
        self._save_achievements()
        return [("周冠军", uid, rec["name"], week_start)]

    def _build_badges_text(self, group_id: str, event, target_uid: str | None = None) -> str:
        """构建勋章墙文本：查看自己或他人（target_uid 为空时查自己）"""
        uid = str(target_uid) if target_uid else str(event.get_sender_id())
        name = event.get_sender_name() or uid
        if target_uid:
            # 查他人时优先使用勋章记录里的昵称
            entry = self._achievements.get(group_id, {}).get(uid)
            if isinstance(entry, dict) and entry.get("name"):
                name = entry["name"]
        badges = self._achievements.get(group_id, {}).get(uid, {}).get("badges", {})
        lines = [f"🏅 勋章墙 · {name}", "━━━━━━━━━━━━━━━━━━━━━━"]
        if not isinstance(badges, dict) or not badges:
            lines.append(f"{name} 暂无勋章，多发言就有机会获得哦。")
            return "\n".join(lines)
        for badge, when in badges.items():
            lines.append(f"🏅 {badge}（{when} 获得）")
        lines.append("━━━ 可获得勋章 ━━━")
        for badge, desc in ACHIEVEMENT_RULES:
            mark = "✅" if badge in badges else "　"
            lines.append(f"{mark} {badge}：{desc}")
        return "\n".join(lines)

    def _build_ranking_text(self, group_id: str, scope: str, top_n: int = 10) -> str:
        """构建今日/本周发言排行文本（scope: today / week）"""
        now = self._now()
        if scope == "week":
            start = self._week_start_str(now)
            end = self._today_str(now)
            label = f"本周 ({start} ~ {end})"
        else:
            start = end = self._today_str(now)
            label = f"今日 ({start})"
        agg = self._aggregate(group_id, start, end)
        if not agg:
            return f"📊 群活跃排行 · {label}\n━━━━━━━━━━━━━━━━━━━━━━\n暂无发言记录。"
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        lines = [f"📊 群活跃排行 · {label}", "━━━━━━━━━━━━━━━━━━━━━━"]
        for i, (uid, rec) in enumerate(ranked[:top_n], 1):
            lines.append(f"{i}. {rec['name']} {rec['count']} 条")
        if len(ranked) > top_n:
            lines.append(f"... 共 {len(ranked)} 人参与")
        return "\n".join(lines)

    def _build_overview_text(self, group_id: str) -> str:
        """构建今日群统计简版总览"""
        now = self._now()
        today = self._today_str(now)
        agg = self._aggregate(group_id, today, today)
        total = sum(r["count"] for r in agg.values())
        lines = [
            "📊 群统计总览",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📅 今日 ({today})",
            f"💬 总发言: {total} 条",
            f"👥 参与人数: {len(agg)} 人",
        ]
        if agg:
            # 非纯数字 ID（如微信 wxid_xxx）也能排序：数字优先，其余按字符串
            def _sort_key(kv):
                uid = kv[0]
                try:
                    return (kv[1]["count"], -int(uid))
                except (TypeError, ValueError):
                    return (kv[1]["count"], 0)

            top = max(agg.items(), key=_sort_key)
            lines.append(f"🔥 最活跃成员: {top[1]['name']} ({top[1]['count']} 条)")
        else:
            lines.append("🔥 今日暂无发言记录")
        return "\n".join(lines)

    def _build_mine_text(self, group_id: str, event: AstrMessageEvent) -> str:
        """构建个人今日统计文本"""
        sender_id = event.get_sender_id()
        name = event.get_sender_name() or sender_id
        today = self._today_str()
        agg = self._aggregate(group_id, today, today)
        rec = agg.get(sender_id)
        if not rec:
            return f"📋 个人统计 · 今日 ({today})\n━━━━━━━━━━━━━━━━━━━━━━\n{name} 今日暂无发言记录。"
        return "\n".join([
            f"📋 个人统计 · 今日 ({today})",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 昵称: {rec['name']}",
            f"💬 发言: {rec['count']} 条",
            f"🔤 字符: {rec['chars']} 字",
            f"🖼️ 图片: {rec['images']} 张",
        ])

    def _build_daily_report_text(self, group_id: str, date: str) -> str | None:
        """构建某日日报摘要（含活跃 Top5、热词 Top5、活跃时段、积分 Top5），当日无数据返回 None"""
        agg = self._aggregate(group_id, date, date)
        if not agg:
            return None
        total = sum(r["count"] for r in agg.values())
        chars = sum(r["chars"] for r in agg.values())
        images = sum(r["images"] for r in agg.values())
        lines = [
            f"📅 群日报 · {date}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"群号: {group_id}",
            f"总发言: {total} 条",
            f"参与人数: {len(agg)} 人",
            f"总字符: {chars} 字",
        ]
        if images:
            lines.append(f"图片: {images} 张")
        lines.append("━━━ 活跃 Top5 ━━━")
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        for i, (uid, rec) in enumerate(ranked[:5], 1):
            lines.append(f"{i}. {rec['name']} {rec['count']} 条")
        # 热词 Top5（当日分词词频）
        hot = self._aggregate_hotwords(group_id, date, date)
        if hot:
            ranked_hot = sorted(hot.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            lines.append("━━━ 热词 Top5 ━━━")
            for i, (w, c) in enumerate(ranked_hot, 1):
                lines.append(f"{i}. {w} ×{c}")
        # 活跃时段条形图（当日按小时分布）
        hours = self._aggregate_hours(group_id, date, date)
        total_h = sum(hours.values())
        if hours and total_h > 0:
            peak = max(hours.items(), key=lambda kv: (kv[1], kv[0]))
            lines.append("━━━ 活跃时段 ━━━")
            for h in range(24):
                c = hours.get(str(h), 0)
                if c <= 0:
                    continue
                pct = c / total_h * 100
                bar = "█" * max(1, round(c / peak[1] * 12))
                lines.append(f"{h:02d}时 {bar} {pct:.0f}%")
            lines.append(f"🔥 最活跃时段: {int(peak[0])} 时")
        # 积分 Top5（当日积分榜）
        pts = self._aggregate_points(group_id, date, date)
        if pts:
            ranked_pts = sorted(pts.items(), key=lambda kv: (-kv[1]["points"], kv[0]))[:5]
            lines.append("━━━ 积分 Top5 ━━━")
            for i, (uid, rec) in enumerate(ranked_pts, 1):
                lines.append(f"{i}. {rec['name']} {rec['points']} 分")
        return "\n".join(lines)

    def _help_text(self) -> str:
        """指令帮助文本"""
        return "\n".join([
            "📊 群统计报表 · 用法",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "/群统计          今日群聊总览",
            "/群统计 今日     今日发言排行 Top10",
            "/群统计 本周     本周发言排行 Top10",
            "/群统计 我的     我的今日发言统计",
            "/群统计 热词     今日热词话题 Top10",
            "/群统计 热词 本周 本周热词话题 Top10",
            "/群统计 时段     今日活跃时段分布",
            "/群统计 时段 本周 本周活跃时段分布",
            "/群统计 勋章     我的成就勋章",
            "/群统计 勋章 <用户ID>  查看他人勋章",
            "/群统计 积分     今日积分榜 Top10",
            "/群统计 积分 本周 本周积分榜 Top10",
            "/群统计 积分 本月 本月积分榜 Top10",
            "/群统计 积分 我的 我的今日积分",
        ])

    def _send_text(self, event: AstrMessageEvent, text: str) -> MessageEventResult:
        """构造纯文本回复"""
        return event.chain_result([Plain(text)])

    # ========== 指令处理 ==========

    @filter.command("群统计", priority=200)
    async def cmd_group_stats(self, event: AstrMessageEvent) -> MessageEventResult | None:
        """群统计入口：/群统计 [今日|本周|我的|热词|时段|勋章|积分|帮助]"""
        text = event.message_str or ""
        m = COMMAND_RE.match(text)
        arg = m.group(1).strip() if m else ""
        group_id = event.get_group_id()
        if not group_id:
            return self._send_text(event, "本命令仅支持在群聊中使用。")
        if arg in ("今日", "今天"):
            return self._send_text(event, self._build_ranking_text(group_id, "today", 10))
        if arg in ("本周", "周榜", "周报"):
            return self._send_text(event, self._build_ranking_text(group_id, "week", 10))
        if arg in ("我的", "个人", "me"):
            return self._send_text(event, self._build_mine_text(group_id, event))
        # —— 热词话题 ——
        if arg in ("热词", "热词榜", "话题"):
            return self._send_text(event, self._build_hotwords_text(group_id, "today", 10))
        if arg.startswith("热词"):
            sub = arg[len("热词"):].strip()
            if sub in ("本周", "周", "本周热词"):
                return self._send_text(event, self._build_hotwords_text(group_id, "week", 10))
            return self._send_text(event, f"未知参数「{arg}」，发送「/群统计 帮助」查看用法。")
        # —— 活跃时段 ——
        if arg in ("时段", "活跃", "活跃时段"):
            return self._send_text(event, self._build_hourly_text(group_id, "today"))
        if arg.startswith("时段"):
            sub = arg[len("时段"):].strip()
            if sub in ("本周", "周"):
                return self._send_text(event, self._build_hourly_text(group_id, "week"))
            return self._send_text(event, f"未知参数「{arg}」，发送「/群统计 帮助」查看用法。")
        # —— 成就勋章 ——
        if arg in ("勋章", "成就", "badge"):
            return self._send_text(event, self._build_badges_text(group_id, event))
        if arg.startswith("勋章"):
            target = arg[len("勋章"):].strip()
            if target:
                return self._send_text(event, self._build_badges_text(group_id, event, target))
            return self._send_text(event, self._build_badges_text(group_id, event))
        # —— 积分榜 ——
        if arg in ("积分", "积分榜", "活跃榜"):
            return self._send_text(event, self._build_points_text(group_id, "today", 10))
        if arg.startswith("积分"):
            sub = arg[len("积分"):].strip()
            if sub in ("本周", "周"):
                return self._send_text(event, self._build_points_text(group_id, "week", 10))
            if sub in ("本月", "月"):
                return self._send_text(event, self._build_points_text(group_id, "month", 10))
            if sub in ("我的", "me"):
                return self._send_text(event, self._build_my_points_text(group_id, event))
            return self._send_text(event, f"未知参数「{arg}」，发送「/群统计 帮助」查看用法。")
        if arg in ("帮助", "help", "?"):
            return self._send_text(event, self._help_text())
        if arg:
            return self._send_text(event, f"未知参数「{arg}」，发送「/群统计 帮助」查看用法。")
        return self._send_text(event, self._build_overview_text(group_id))

    # ========== 自动日报 ==========

    async def initialize(self) -> None:
        """插件加载/重载时启动日报定时任务"""
        await self._start_report_loop()

    @filter.on_astrbot_loaded()
    async def _start_report_loop(self):
        """启动日报定时任务（幂等：重复调用不会重复启动）"""
        if not self._cfg_bool("stats_report_enable", False):
            return
        if self._report_running:
            return
        self._report_running = True
        self._report_task = asyncio.create_task(self._report_loop())
        self._report_task.add_done_callback(
            lambda t: (
                logger.error(f"日报任务异常退出: {t.exception()}")
                if not t.cancelled() and t.exception()
                else None
            )
        )

    async def _report_loop(self):
        """后台定时循环：每 30 秒检查一次日报触发条件"""
        while self._report_running:
            try:
                await self._check_and_send_reports()
            except Exception as e:
                logger.warning(f"日报任务异常: {e}")
            await asyncio.sleep(30)

    def _resolve_platform(self, group_id: str) -> str:
        """确定日报目标群所属平台 ID：配置优先，其次自动学习映射，再否则为空"""
        v = self._cfg_str("stats_report_platform").strip()
        if v:
            return v
        return self._group_platforms.get(str(group_id), "")

    async def _check_and_send_reports(self):
        """检查是否到达日报推送时间，到点向目标群推送当日统计摘要（同日同群去重）"""
        if not self._cfg_bool("stats_report_enable", False):
            return
        now = self._now()
        target = self._cfg_str("stats_report_time", "22:00").strip()
        # 校验时间格式，非法值回退默认，避免任务永远不触发
        if not TIME_RE.match(target):
            target = "22:00"
        if now.strftime("%H:%M") < target:
            return
        today = now.strftime("%Y-%m-%d")
        for gid in self._cfg_list("stats_report_groups"):
            if self._report_dates.get(gid) == today:
                continue  # 同日同群已推送，去重
            # 结算本周周冠军勋章（有数据才可能颁发）
            self._settle_week_champions(gid)
            platform = self._resolve_platform(gid)
            if not platform:
                logger.warning(
                    f"【{PLUGIN_NAME}】群 {gid} 的平台 ID 未知"
                    f"（可在 stats_report_platform 中指定，或等该群有消息后自动学习），本次跳过日报"
                )
                continue
            text = self._build_daily_report_text(gid, today)
            if not text:
                continue  # 当日无数据，不推送
            if await self._send_report(platform, gid, text):
                self._report_dates[gid] = today
                self._save_report_dates()

    async def _send_report(self, platform: str, group_id: str, text: str) -> bool:
        """向目标群推送日报（UMO 格式 平台ID:GroupMessage:群号）；成功返回 True，失败不记账"""
        umo = f"{platform}:GroupMessage:{group_id}"
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as e:
            logger.warning(f"推送日报到群 {group_id} 失败: {e}")
            return False

    async def terminate(self):
        """插件卸载：取消后台任务并落盘数据"""
        self._report_running = False
        if self._report_task:
            self._report_task.cancel()
            try:
                await self._report_task
            except asyncio.CancelledError:
                pass
        self._save_stats()
        self._save_report_dates()
        self._save_extras()
        self._save_achievements()
        self._save_points()
