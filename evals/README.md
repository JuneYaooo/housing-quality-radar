# 评测与验证记录

本目录回答三个不同的问题，结论不能互相替代。

| 问题 | 证据 | 状态 |
| --- | --- | --- |
| 规则层是否按调研后的口径计算？ | [cases_v1.jsonl](cases_v1.jsonl) 19 例（normal 5 / boundary 5 / regression 3 / hard 5 / adversarial 1），[run_rule_cases.py](scripts/run_rule_cases.py) 同一输入跑改前、改后；[逐例判定](rules-v0.3/judgments.jsonl)、[汇总](rules-v0.3/summary.json) | 2026-11-02 已运行：v0.2 书面规则 9/19，v0.3.0 代码 19/19 |
| 真实房源上输出什么？ | [三林真实案例](sanlin-20260921/report.md)（2026-09-21 浏览器抽样）及 [v0.3.0 回放](sanlin-20260921/v0.3/) | 回放已运行；未重新采集 |
| 找房报告整体质量如何？ | [评分量表](rubric_v1.md)、[协议](protocol_v1.json) | **not_run**：平台访问受限，见协议中的 blocker |

## 口径说明

- 规则层“改前”是把 v0.2 的 references/analysis.md 书面规则逐条复现成代码（v0.2 本身没有计算脚本），与“改后”使用完全相同的输入和判定函数。
- 19 例由作者依据新规则编写，改后全通过是预期结果。它证明代码执行了调研后的规则、v0.2 漏掉了哪些情形；不证明找房准确率，也不能外推到其他城市。
- 三林回放的输入是已保存的观察摘要。2026-11-02 匿名访问 4 条原帖均返回 300031，不代表房源已下架。
- [单元测试](unit-tests.txt)（35 项）用合成数据和模拟 API 检查程序行为。
- [前期 Jev 实测汇总](jev-historical-summary.json) 是 2026-09-21 的 6 次历史调用，本版没有新的 Jev 调用。
- [本次验证及文件哈希](current-validation.json) 绑定脚本、输入、输出和展示图。

## 复现

在仓库根目录运行 `python3 evals/scripts/run_rule_cases.py`（会覆盖 rules-v0.3/ 下的判定和汇总）和 `python3 -m unittest discover -s tests -v`。更多见 [docs/usage.md](../docs/usage.md)。

迭代过程、问题清单和“改/不改”理由见 [iteration_notes.md](iteration_notes.md)。
