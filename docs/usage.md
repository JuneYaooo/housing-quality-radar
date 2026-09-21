# 安装与运行

## 安装

Python辅助脚本要求3.10+，仅使用标准库。Skill主体由宿主Agent执行。

```bash
git clone https://github.com/JuneYaooo/housing-quality-radar.git \
  ~/.codex/skills/xiaohongshu-housing-radar
```

已有同名目录先检查自己的修改，再更新。也可克隆到工作目录后运行 `bash install_as_skill.sh`；安装器会备份原安装，默认目标 `${CODEX_HOME:-$HOME/.codex}/skills`，可用首个参数指定目标父目录。

## 常规模式（agent）

告诉Agent“不使用Jev”，配置 `decision_pipeline.mode` 为 `agent`。Agent直接完成全文、账号和图片分析，不调用TypeSafe。日期和金额使用代码计算。通常不必运行辅助脚本。

需要整理批量文本时可执行：

```bash
python3 scripts/triage.py evals/sanlin-20260921/triage-input.jsonl \
  --mode agent --output runs/agent-queue.jsonl
```

这个脚本只整理待分析队列，不会在本地启动另一个大模型。Agent必须读取原始文本，完成分类、依据说明、图片与账号复核，再生成最终报告。

## Jev模式（jev）

在宿主运行环境配置 `TYPESAFE_API_KEY`，不要把密钥写入仓库、参数或报告。配置 `decision_pipeline.mode` 为 `jev`，Agent脱敏并整理输入后调用：

```bash
python3 scripts/triage.py /absolute/path/redacted-input.jsonl \
  --mode jev --output /absolute/path/new-triage.jsonl --max-requests 20
```

输入为JSONL，每条有唯一的字符串post_id和非空text。只有text发给TypeSafe。脚本不自动脱敏；输出路径必须是新文件。

`--enable-jev`保留为旧版兼容参数，与`--mode`互斥。旧配置仅有`jev.enabled`时，Agent将其迁移成mode；显式mode优先。Jev低置信、异常或调用上限不会丢掉候选；Agent继续复核。

输出中的mode表示请求模式，review_reasons表示降级/待复核原因；缺Key时不能将请求模式误报为实际成功调用。`triage_policy=shadow`表示初筛只提供标签，候选全量保留。

## 数据与采集

运行数据保存在用户工作目录 `housing-radar/<watch_id>/`。浏览器成功后不为同一字段重复调用付费采集；外部工具需单独安装。[来源适配](../references/source-adapters.md) · [决策与复核规则](../references/decision-pipeline.md)

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

README中的真实案例来自已有浏览器采集证据，不是此脚本生成的模型推断。单测用模拟响应验证程序边界，不作为真实找房效果展示。评测记录在`evals/`，运行缓存放`runs/`且不提交。
