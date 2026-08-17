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
            return v.strip().lower() in ("1", "true", "yes", "on")
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
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    # ========== 数据持久化 ==========

    def _load_all(self):
        """加载全部持久化数据并清理过期统计"""
        self._load_stats()
        self._load_report_dates()
        self._cleanup_old()

    def _load_stats(self):
        """从磁盘加载统计数据（校验结构，损坏时重置）"""
        path = os.path.join(self.data_dir, "stats.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._stats = data
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
        """持久化日报去重记录（同日同群不重复推送）"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, "report_dates.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._report_dates, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存日报记录失败: {e}")

    def _cleanup_old(self):
        """清理超过 stats_keep_days 天的统计（仅内存，由调用方决定是否落盘）"""
        keep = max(1, self._cfg_int("stats_keep_days", 30))
        cutoff = (self._now() - timedelta(days=keep)).strftime("%Y-%m-%d")
        for gid in list(self._stats.keys()):
            for date in list(self._stats[gid].keys()):
                if date < cutoff:
                    del self._stats[gid][date]
            if not self._stats[gid]:
                del self._stats[gid]

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
            images = sum(1 for seg in event.get_messages() if self._is_image_segment(seg))
            name = event.get_sender_name() or sender_id
            today = self._today_str()
            rec = self._stats.setdefault(group_id, {}).setdefault(
                today, {}
            ).setdefault(str(sender_id), {"name": name, "count": 0, "chars": 0, "images": 0})
            rec["name"] = name
            rec["count"] += 1
            rec["chars"] += chars
            rec["images"] += images
            # 定期落盘，避免每条消息都写盘（默认 5 分钟一次）
            if time.time() - self._last_save >= self._cfg_int("stats_save_interval", 300):
                self._last_save = time.time()
                self._save_stats()
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

    # ========== 统计聚合与报表 ==========

    def _aggregate(self, group_id: str, date_from: str, date_to: str) -> dict:
        """聚合 [date_from, date_to] 日期区间的成员统计数据（含脏数据防御）"""
        agg: dict[str, dict] = {}
        for date, members in (self._stats.get(group_id) or {}).items():
            if not (date_from <= date <= date_to):
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
            top = max(agg.items(), key=lambda kv: (kv[1]["count"], -int(kv[0])))
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
        """构建某日日报摘要（含活跃 Top5），当日无数据返回 None"""
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
        ])

    def _send_text(self, event: AstrMessageEvent, text: str) -> MessageEventResult:
        """构造纯文本回复"""
        return event.chain_result([Plain(text)])

    # ========== 指令处理 ==========

    @filter.command("群统计", priority=200)
    async def cmd_group_stats(self, event: AstrMessageEvent) -> MessageEventResult | None:
        """群统计入口：/群统计 [今日|本周|我的|帮助]"""
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
