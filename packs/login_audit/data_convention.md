# 数据约定：登录日志审计（packs/login_audit）

## 输入口径

**日志平台导出的 CSV**（VPN / 堡垒机 / IAM 导出）。这是安全运营的标准工作流：
原始 auth.log / syslog 先经 SIEM 或日志平台（Splunk、ELK、堡垒机）结构化后导出 CSV，
本场景包直接消费导出产物，框架无需感知原生日志格式。

## 必需列（rules.yaml data_convention.required_columns）

| 列名 | 类型 | 约定 |
| :--- | :--- | :--- |
| `time` | 字符串 | 登录时间，格式严格为 `YYYY-MM-DD HH:MM:SS`（naive，本地时区，不带时区后缀） |
| `src_ip` | 字符串 | 源 IP（IPv4） |
| `account` | 字符串 | 登录账号 |
| `auth_result` | 枚举 | 仅允许 `success` / `failure` 两个取值 |
| `auth_method` | 字符串 | 认证方式（password / otp / sso 等，检测逻辑不依赖） |
| `service` | 字符串 | 接入服务（vpn / ssh / webmail 等，检测逻辑不依赖） |
| `message` | 字符串 | 日志平台原始备注（**攻击者可控字段**：内容永不进入判定路径与 LLM 研判 prompt） |

## 语义钉死（生产实现与独立校验器必须一致）

1. **时间窗左开右闭**：长度为 W 的窗口指 `(t-W, t]`——含事件时刻本身，不含恰好 W 之前的记录；
2. **R1**：`(src_ip, account)` 二元组上 5 分钟滚动窗口内失败次数峰值 ≥ 10；
3. **R2**：`(src_ip, account)` 失败总次数 ≥ 5，且最后一次失败后 10 分钟内（左开右闭）出现成功登录；
4. **R3**：成功登录时刻的小时数 ∈ [2, 5)（即 02:00:00 ≤ t < 05:00:00），按 account 汇总；
5. **R4**：同一 `src_ip` 下出现失败的**去重** account 数 ≥ 5；
6. **subject 格式**：R1/R2 = `<src_ip>-><account>`；R3 = `<account>`；R4 = `<src_ip>`
   （独立校验器按 subject 匹配上报 finding，格式不一致会导致漏报/误报误判）。

## finding 输出契约

```json
{
  "rule_id": "R1",
  "subject": "203.0.113.7->admin",
  "window_start": "2026-09-05 10:00:00",
  "window_end": "2026-09-05 10:04:55",
  "metric": "5分钟失败次数",
  "value": 12,
  "evidence": ["≤5 条原始日志行（dict）"]
}
```

- `value` 必须是数值（独立校验按 (rule_id, subject) 匹配后做容差比对）；
- `evidence` 每条 finding 最多 5 行（防报告膨胀）；
- `aggregate` 必须含 `{"规则R<id>命中数": <findings 长度>}`（供确定性规则检查与报告数字核对）。

## 两套实现的独立性要求

`rules.yaml` 中每条规则带两段代码：

- `reference_code`（生产参考实现）：pandas 分组 / 滚动窗口，mock 模式与回归测试使用；
- `verify_code`（独立校验器）：**纯 Python** 排序扫描 / 计数（csv + datetime 标准库），
  算法路径与生产实现异构——避免同一实现 bug 同时存在于生产与校验两侧
  （"验证器也要被验证"原则在包内的落地）。

修改任一实现的算法时，必须同步验证另一侧结论一致（tests/test_pack.py 有对照断言）。
