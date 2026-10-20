# 工行金边分行每日新闻监测

每天金边时间 **08:00** 汇总前一天新闻，推送已有 Telegram 机器人所配置的接收聊天。08:37 自动补跑；已确认送达的日报不会再次发送。每次检查最近三天中尚未送达的日报，自 `MONITOR_START_DATE` 起算。GitHub 计划任务可能延迟或被平台丢弃，时间不是实时服务承诺。

## 监测与日报内容

- Google News 5 路、Bing News 3 路公开 RSS 检索，包含 ICBC Phnom Penh、ICBC Cambodia、Industrial and Commercial Bank of China、工商银行、工行，以及金边/柬埔寨中英柬文关键词。
- 标题或可用来源摘要必须同时出现银行和金边/柬埔寨关联，排除加拿大同名 ICBC 保险机构。原文正文未被索引、仅正文提及分行、社交媒体和付费墙内容可能遗漏。
- 按 RSS **标注的发布时间**换算金边日期；不会将首次抓到的旧文章算作昨天新闻。索引的时间不一定等于网站原始发布时间，本程序不声称已经逐篇核验原站时间。
- 合并相同链接、相同或高度近似标题。日报为中文说明，保留原文标题、来源、时间、链接；Bing 有可用的来源摘要时附上原文摘要。没有付费 AI 翻译或生成摘要，也不会根据标题编造文章内容。
- 检索成功但零结果：发送“本次检索未发现符合日期和关联规则的新闻”。部分来源故障：明确标注不完整，后续重试完整日报。全部失败：发送故障通知，不能当作没有新闻。

## 云端部署与费用边界

当前部署：<https://github.com/Ian1232/icbc-phnom-penh-news-monitor>

使用**公开仓库的标准 `ubuntu-latest` GitHub 托管运行器**，无需购买服务器。Python 标准库实现，无需安装收费 API、AI 模型或第三方新闻订阅。Telegram 使用普通机器人消息，未启用 `allow_paid_broadcast`。

GitHub 官方说明：[标准运行器计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[定时任务限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。Telegram：[机器人发送限制与免费发送](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)。**若改为私有仓库或 larger runner，免费条件会改变。** 平台未来可能调整政策，免费服务没有持续可用性保证。

公开仓库连续 60 天没有活动时，GitHub 会停用定时任务。程序在独立 `monitor-state` 分支提交实际送达记录，保留运行活动；若任务长期没有成功运行，仍需在 Actions 页恢复。主分支不写入运行数据。

## 配置与保密

在仓库 Settings → Secrets and variables → Actions 设置：

| 类型 | 名称 | 用途 |
|---|---|---|
| Secret | `TELEGRAM_BOT_TOKEN` | 机器人密钥 |
| Secret | `TELEGRAM_CHAT_ID` | 单个接收聊天 |
| Secret | `TELEGRAM_CHAT_IDS` | 多个接收聊天，以逗号/空格/分号分隔；填写后优先于单个 ID |
| Variable | `MONITOR_START_DATE` | 最早日报日期，当前 `2026-10-19` |

已有 Telegram 配置已加密迁入新仓库。机器人密钥和聊天 ID 不进入公开源码、日志、日报附件或状态分支。状态分支仅记录公开新闻文本、送达分块序号和以密钥计算的接收方 HMAC 标识。

## 查看和手动运行

打开仓库 **Actions → ICBC Phnom Penh daily news → Run workflow**：

- 默认：发送昨天日报；最近三天有遗漏时补发。
- `report_date`：指定已经结束的金边日期，格式 `YYYY-MM-DD`。
- `dry_run`：只采集并生成报告，不推送、不修改送达记录。
- `test_message`：发送带“测试”字样的消息，使用独立送达记录。
- `force`：明确要求重复发送时才勾选。

每次运行保留 7 天诊断报告，可查看查询数量、原始条数、过滤后的条数及来源成功/失败状态。红色失败任务可能仍已发送故障通知或部分结果，应结合报告检查。

## 本地运行

要求 Python 3.12+；不需要 `pip install`。

```console
python -m unittest discover -s tests -v
python -m news_monitor --date 2026-10-19 --dry-run
python -m news_monitor
```

本地发送时从环境变量提供 Telegram 配置，不把密钥写在命令参数或代码中。`state/deliveries.json` 和 `reports/` 已在忽略列表中。

## 投递与恢复

生产任务串行执行；成功发送每一分块后保存本地记录，任务结束时即使发生部分失败也提交云端状态。重复运行会使用第一次冻结的日报文本，只补发尚未确认的分块。Telegram `sendMessage` 不提供幂等键；如果 Telegram 已接受消息但返回前网络中断，或进程在远端接受后、记录落盘前崩溃，后续重试仍存在重复消息的极小窗口。本程序不声称“严格一次”投递。

补发窗口为最近三天；停运超过三天时，可通过手动指定 `report_date` 补发更早日期，但免费新闻索引不保证保留历史收录。
