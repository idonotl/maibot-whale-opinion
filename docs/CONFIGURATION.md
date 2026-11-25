# 配置字段参考

由本次发布的 config.py 提取；最终校验以代码与宿主配置 UI 为准。不要把实际配置提交到仓库。

| 配置类.字段 | 类型 | 默认值/工厂 | 说明 |
|---|---|---|---|
| OpinionSettings.config_version | `str` | `'0.1.0'` | 配置结构版本；缺少或清空时插件拒绝启用 |
| OpinionSettings.enabled | `bool` | `False` | 总开关；首次部署建议关闭，完成离线验收后再开启 |
| OpinionSettings.shadow_mode | `bool` | `True` | 试运行：只记录判断，不改写回复 |
| OpinionSettings.bot_account_id | `str` | `''` | 机器人自己的 QQ 号，用于区分平台、账号与场景 |
| OpinionSettings.private_user_ids | `list[str]` | `list` | 允许使用的私聊 QQ 号，留空表示不启用私聊 |
| OpinionSettings.group_ids | `list[str]` | `list` | 允许使用的群号，留空表示不在群聊启用 |
| OpinionSettings.webui_test_prefix | `str` | `''` | 仅用于本地验收的 WebUI 身份前缀；留空则不启用网页会话 |
| OpinionSettings.enable_web_evidence | `bool` | `False` | 是否采纳宿主工具结果作为证据；需先通过适配验收 |
| OpinionSettings.search_per_turn_max | `int` | `2` | 每轮最多搜索次数（仅记录与提示，实际由 Planner 执行） |
| OpinionSettings.read_per_turn_max | `int` | `2` | 每轮最多正文读取次数（同上） |
| OpinionSettings.analysis_output_tokens_max | `int` | `800` | 单次结构化分析的最大输出 token |
| OpinionSettings.analysis_task | `str` | `'utils'` | 结构化分析使用的 MaiBot 模型任务 |
| OpinionSettings.analysis_timeout_seconds | `float` | `120.0` | 后台分析总等待时间，含候选模型切换 |
| OpinionSettings.analysis_min_interval_seconds | `int` | `20` | 同一会话两次分析的间隔下限 |
| OpinionSettings.default_criteria | `list[str]` | `lambda: ['稳定性', '关键决策', '团队贡献', '精彩操作']` | 评价赛事/表现的默认关注点，可被用户明确指定的标准覆盖 |
| OpinionSettings.discussion_review_after_turns | `int` | `3` | 第几轮开始检查是否还有信息增量 |
| OpinionSettings.no_new_information_limit | `int` | `2` | 连续几轮没有新信息就允许保留分歧并收尾 |
| OpinionSettings.repeat_window_turns | `int` | `2` | 最近几轮说过的理由不再换个说法重复 |
| OpinionSettings.topic_active_ttl_hours | `int` | `24` | 活跃讨论状态保留时长 |
| OpinionSettings.closed_topic_retention_days | `int` | `7` | 已结束话题保留天数 |
| OpinionSettings.audit_retention_days | `int` | `14` | 审计元数据保留天数 |
| OpinionSettings.evidence_retention_days | `int` | `3` | 证据缓存保留天数 |
| OpinionSettings.max_prompt_chars | `int` | `900` | 注入提示块的最大字符数 |
| OpinionConfig.plugin | `OpinionSettings` | `OpinionSettings` |  |
