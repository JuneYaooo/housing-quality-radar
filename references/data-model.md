# 配置、记录与持久化

运行目录包含 `config.json`、`posts.jsonl`、`accounts.jsonl`、`properties.jsonl`、`observations.jsonl`、`runs.jsonl`、`events.jsonl` 和按日期保存的报告。小样本可直接维护 JSONL；量大再用 SQLite，保持字段语义一致。每条记录有 `schema_version: 1`；时间采用带时区 ISO 8601，默认 Asia/Shanghai，金额统一人民币但保留原始文本和单位。

## 记录字段

| 文件 | 必要字段与含义 |
|---|---|
| posts | `post_id`、`source_url`、`account_id`（来源脱敏时可空）、`published_at`（未知可空）、`published_at_precision`（minute/day/month/relative/unknown）、`published_at_raw`、`first_observed_at`、`last_observed_at`、`query`、`content_access`（full/partial/snippet_only）、`excerpt`、`media_checked`、`listing_claims` |
| accounts | `account_id`、`profile_url`、`role`、`role_confidence`、`role_evidence`、`source_group_id`（未知可空）、`independence_evidence`、`reviewed_at`、`history_sample_n` |
| properties | `property_id`、`city`、`district`、`community`、`transaction_type`、`rental_type`、`layout`、`area_sqm`、`area_basis`（建筑/套内/未知）、`floor_band`、`elevator`、`orientation`、`condition`、`confirmed_post_ids`、`possible_matches`、`match_evidence`、`match_confidence`、`first_observed_at`、`last_observed_at` |
| observations | `observation_id`、`run_id`、`post_id`、`property_id`（未匹配可空）、`observed_at`、`observation_type`、`value`、`source_url`、`evidence_excerpt`、`evidence_kind`（observed/publisher_claim/user_verified/inferred）、`effective_at`（明确事件时间，否则为空） |
| runs | `run_id`、`started_at`、`finished_at`、`config_version`、`queries_attempted`、`queries_succeeded`、`sort_modes`、`posts_seen`、`posts_new`、`accounts_reviewed`、`coverage_notes`、`errors`、`result`（complete/partial/blocked） |
| events | `event_id`、`property_id`、`event_type`、`old_value`、`new_value`、`evidence_ids`、`created_at`、`notification_status` |

`listing_claims` 每个字段保存 `{value, raw, evidence_excerpt}`，涵盖位置、价格、费用、户型、面积、装修、租售状态。全文及媒体不必批量存档，保留复核需要的短摘录和必要截图即可。

价格至少区分：租金元/月、整租/单间、付款周期、押金、物业费、中介费及其他必要费用；买房总价元、面积口径、报价单价元/平方米。万元先换算，保留原值；短租日价不直接混入月租比较。购房额外费用未知则标未知，不编造税费或贷款条件。

`observation_type` 建议：`post_seen`、`price_claim`、`available_claim`、`rented_claim`、`sold_claim`、`user_verified_status`、`post_deleted`、`post_inaccessible`、`search_not_found`、`conflict`。帖子状态与房屋状态分别保存。新观察不能覆盖旧价格、旧文案和已发生的状态。

## 幂等与失败恢复

- 优先用平台稳定 ID；没有 ID 时以规范链接为键，只剔除已确认的追踪参数，保留访问必要参数。无法得到链接的截图使用本地稳定 ID，不伪造平台 ID。
- 同一运行重复看到同帖只记一次同类同值观察；不同日期再次看到是有效新观察，不应被删掉。
- 房源 ID 稳定，房源合并/拆分保留映射、原因和旧簇；未知字段不能覆盖已证实字段，冲突并存。
- 写文件采用临时文件＋原子替换；并发运行加锁或串行处理。失败后重跑使用原 run_id 或幂等键，不能重复通知。
- `event_id` 由房源、事件类型及新证据/新值稳定生成；已发送事件不重复发送。通知失败可重试同一事件，保留失败记录。
- 账号、房源和观察中的推断标注规则版本与证据 ID，规则变更后允许重新计算，不能篡改原始观察。

## 一个虚构观察示例

```json
{"schema_version":1,"observation_id":"demo-obs-01","run_id":"demo-run-01","post_id":"demo-post-01","property_id":"demo-home-01","observed_at":"2026-09-21T10:00:00+08:00","observation_type":"available_claim","value":"可租","source_url":null,"evidence_excerpt":"虚构示例：月底可入住","evidence_kind":"publisher_claim","effective_at":null}
```

示例仅解释结构，禁止混入真实监控数据。报告引用 `null` 来源时要说是未提供链接的材料，不能生成看似真实的原帖地址。

## 来源追踪补充

所有入口统一遵循 [source-adapters.md](source-adapters.md) 的跨来源字段与缓存语义。缓存复用不是新的房源观察；匿名作者字段不得伪装为真实账号标识。runs 额外记录 `provider_attempts`、`capabilities_succeeded`、`cache_items` 与 `missing_layers`。

## 结构化房源输入（listing_metrics）

每次排序前，Agent 把当前去重房源整理为 `listings-<run_id>.json`：`{"as_of": "YYYY-MM-DD", "listings": [...], "exit_durations": [...]}`。每条房源：

| 字段 | 说明 |
| --- | --- |
| `post_id`、`property_id` | 稳定 ID；同一房源多条报价时 `property_id` 相同，脚本只取最近观察 |
| `city`、`community`、`transaction_type`（rent/sale）、`layout` | `layout` 用同一写法（如“2室”）才会被视为同户型 |
| `observed_on`、`published_on` | 日期；`published_on` 未知为 null |
| `price_claims` | `[{source: title/body/comment, raw: "268万元"}]`，标题和正文都保留，脚本判断冲突 |
| `area` | `{value, basis}`，basis 用原文：建筑面积/产证面积/套内面积/使用面积/自述 |
| `fees` | 租房费用；`monthly_*` 为每月必付，`one_time_*` 为不可退一次性费用，未知写 null |
| `planned_months` | 计划居住月数，用于摊销一次性费用 |
| `payment` | `prepay_months`、`deposit_months`、`payee`（landlord/agent/platform/unknown）、`pay_before_viewing`、`untraceable_payment`、`rent_loan` |
| `publisher_role` | individual/agent/institution/self_claimed_owner/unknown |
| `verification_code` | 帖子展示的核验编号或 "shown"；未展示为 null。不要写入用户隐私 |

`exit_durations` 每条为 `{days, exited}`：`exited=true` 仅用于明确的“已租/已售”观察；仍在、删除、不可见都写 `false`（右删失）。

## 文本初筛记录

可选 `triage-<run_id>.jsonl` 保存 scripts/triage.py 输出，按 post_id 关联原始证据；labels 仅为模型推断。保留请求/实际模型、文本/问题哈希、usage、耗时和复核原因，不保存密钥。`requires_evidence_review` 与 `triage_review_required` 分开：前者对所有房源为 true，后者仅指初筛文本是否需要额外处理。
