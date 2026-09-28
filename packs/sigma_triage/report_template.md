# 🚨 多源告警分诊报告（{{ pack_name }} v{{ pack_version }}）

> 生成时间：{{ timestamp }}
> 用户问题：{{ question }}
> 数据源：{{ data_sources }}
> 规则包：{{ rule_count }} 条规则（{{ rule_ids }}）
> 命中统计：{{ rule_stats }}
{% if aggregates_text %}> 规则关键指标：{{ aggregates_text }}
{% endif %}{% if failed_rules %}> ⚠️ 未能完成的规则：{{ failed_rules|join("、") }}（这些规则的结论缺失，不得当作"无风险"）
{% endif %}
> 独立复算：{{ verification_note }}

## 一、分诊队列（事实层，不含推断）

按 severity → 指标数值排序。这一节只写"规则算出了什么"，不写"这意味着什么"。

{% if findings %}
| 优先级 | 严重级 | 规则 | 主体 | 指标 | 数值 | 时间窗 |
| :--- | :--- | :--- | :--- | :--- | ---: | :--- |
{% for f in findings %}
| {{ loop.index }} | {{ f.severity }} | {{ f.rule_name }}（{{ f.rule_id }}） | {{ f.subject }} | {{ f.metric }} | {{ f.value }} | {{ f.window }} |
{% endfor %}
{% else %}
**本轮无命中。** 在安全场景里"无发现"是正常结果，不是失败：规则阈值未被打穿即为通过。
{% endif %}

## 二、证据链（可追溯到原始行）

{% if findings %}
每条发现附至多 {{ evidence_limit }} 行原始日志。证据行由规则实现从数据里取出，
不是事后补写；报告中的任何数字都应能在这里或规则关键指标行里找到出处。
{% for f in findings %}
- **{{ f.rule_id }} {{ f.subject }}**（{{ f.metric }} = {{ f.value }}，窗口 {{ f.window }}）
{% for line in f.evidence_lines %}
  - `{{ line }}`
{% endfor %}
{% endfor %}
{% else %}
无证据行（无命中）。
{% endif %}

## 三、处置建议（按规则目录给出，不由模型发明）

{% if findings %}
{% for f in findings %}
- **{{ f.rule_id }} {{ f.subject }}**：{{ f.disposition }}
{% endfor %}
{% else %}
- 无需处置动作。若本轮预期应有命中，请复核数据时间窗与规则阈值，而不是直接采信"干净"。
{% endif %}

## 四、研判摘要（推断层，允许不确定表述）

{{ narrative }}

## 五、分诊说明（口径与边界）

- 三源列名不统一：认证日志的 `src_ip` 与资产台账 / EDR 的 `主机` 由包内列别名认定为同一实体；
  别名只用于识别，原始列名未被改写，`sources/` 里的原件与 sha256 才是"当时读的就是这一份"的依据。
- 跨表规则在派发前经过基数预检：连接键与预期行数已确认，未通过预检的跨表任务不会被执行。
- 阈值取自实测背景噪声：非生产域主机也有多次失败、有高危 EDR 告警的主机也有一次偶发失败，
  这两类是刻意保留的误报陷阱，规则不应命中它们。
{% if review_issues %}
- 评审意见：
{% for issue in review_issues %}
  - [{{ issue.severity }}] {{ issue.message }}
{% endfor %}
{% endif %}
