# 🔐 登录日志安全审计报告

> 生成时间：{{ timestamp }}
> 用户问题：{{ question }}
> 审计范围：{{ row_count }} 条登录记录
> 检测规则包：{{ pack_name }} v{{ pack_version }}（{{ rule_count }} 条规则：{{ rule_ids }}）
> 规则命中统计：{{ rule_stats }}

## 一、发现清单（证据层）

{% if findings %}
| 严重级 | 规则 | 主体 | 指标 | 数值 | 时间窗 |
| :--- | :--- | :--- | :--- | ---: | :--- |
{% for f in findings %}
| {{ f.severity }} | {{ f.rule_name }}（{{ f.rule_id }}） | {{ f.subject }} | {{ f.metric }} | {{ f.value }} | {{ f.window }} |
{% endfor %}

### 证据行（每条发现至多 {{ evidence_limit }} 行日志原文）

{% for f in findings %}
- **{{ f.rule_id }} {{ f.subject }}**：
{% for line in f.evidence_lines %}
  - `{{ line }}`
{% endfor %}
{% endfor %}

{% else %}
**无发现**：全部检测规则未命中（独立复算一致）。未检出异常登录行为。
{% endif %}

## 二、处置建议（来自规则包，确定性）

{% if findings %}
{% for f in findings %}
- **{{ f.rule_id }} {{ f.subject }}**（{{ f.severity }}）：{{ f.disposition }}
{% endfor %}
{% else %}
本次无需处置动作。
{% endif %}

## 三、研判摘要（推断层）

{{ narrative }}

> ⚠️ 本节为模型推断，仅基于上述发现清单；处置前请人工复核证据行原文。
{% if review_issues %}

### 评审修订说明

{% for issue in review_issues %}
- {{ issue.section }}：{{ issue.message }}
{% endfor %}
{% endif %}

## 四、审计说明

- 检测逻辑：确定性规则包（阈值与窗口见 `packs/{{ pack_name }}/rules.yaml`），LLM 不参与危险判定；
- 数字校验：每条命中的数值均经独立复算比对（producer ≠ verifier）——{{ verification_note }}；
- 全程留痕：本轮全部 Agent 消息与工具调用见 `transcript.jsonl`。
