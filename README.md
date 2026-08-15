# 群统计报表（astrbot_plugin_group_stats）

AstrBot 群活跃度统计与日报/周报插件：自动统计每个群每位成员的每日发言情况，支持今日/本周排行查询，并可定时向指定群推送每日统计摘要。

- **作者**：云晓
- **版本**：1.0.0
- **许可证**：MIT（详见 [LICENSE](LICENSE)）

## 功能

### 消息统计
- 监听群消息，按群、按成员、按日期记录发言数据
- 统计量：发言条数、字符总数、图片数
- 只统计普通文本消息（命令消息不计入），始终忽略机器人自身
- 可配置是否忽略其他机器人账号的发言（`stats_ignore_bots`，默认开启）
- 数据存于内存，每 5 分钟定期落盘，插件卸载时强制落盘

### 排行查询（`/群统计`）
- `/群统计`：今日群聊简版总览（总发言条数 / 参与人数 / 最活跃成员）
- `/群统计 今日`：今日发言排行 Top10（昵称 + 条数）
- `/群统计 本周`：本周（周一至今）发言排行 Top10
- `/群统计 我的`：我的今日发言统计（条数 / 字符 / 图片）
- `/群统计 帮助`：查看全部用法
- 命令兼容 `/`、`／` 全角斜杠与无斜杠前缀

### 自动日报
- 配置 `stats_report_enable` 开启后，后台每 30 秒检查一次，到达 `stats_report_time`（默认 22:00）时向 `stats_report_groups` 指定的群推送当日统计摘要（含活跃 Top5）
- 同日同群去重，已推送日期持久化，重启不重复推送
- 当日无发言数据的群不推送

### 数据清理
- `stats_keep_days`（默认 30 天）控制统计保留天数，自动清理过期数据

## 配置

见 `_conf_schema.json`，关键项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `stats_report_enable` | bool | `false` | 是否启用每日自动群日报推送 |
| `stats_report_time` | string | `22:00` | 每日日报推送时间（HH:MM） |
| `stats_report_groups` | string | 空 | 自动日报推送的目标群号列表，逗号分隔 |
| `stats_keep_days` | int | `30` | 统计数据保留天数，自动清理过期数据 |
| `stats_ignore_bots` | bool | `true` | 是否忽略机器人账号的发言统计 |

## 使用示例

```
/群统计            # 今日群聊总览
/群统计 今日       # 今日发言排行 Top10
/群统计 本周       # 本周发言排行 Top10
/群统计 我的       # 我的今日发言统计
/群统计 帮助       # 查看用法
```

## 数据存储

存储于 `plugin_data/astrbot_plugin_group_stats/`：

- `stats.json`：按群按日期按成员的发言统计数据
- `report_dates.json`：日报推送去重记录（群号 → 已推送日期）

## 依赖

- AstrBot 核心库（v4.x）
- 仅使用 Python 标准库与 AstrBot 自带组件，无第三方依赖

## 测试

在插件目录执行：

```
D:\uv-tools\astrbot\Scripts\python.exe test_group_stats.py
```

测试使用 FakeEvent / FakeContext 替身与临时数据目录，不联网、不写真实 plugin_data。
