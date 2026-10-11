# agentflow

[![python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![fastapi](https://img.shields.io/badge/API-FastAPI-009688)](app)
[![vue](https://img.shields.io/badge/UI-Vue%203-42b883)](frontend)
[![offline](https://img.shields.io/badge/offline-no%20API%20Key-lightgrey)](#快速开始)

**用自然语言问本地数据，拿一份能对账的报告。**

一个多智能体数据分析 / 审计运行时：你把 CSV 丢进来、用一句话问问题，七个 Agent 协作跑完
探查 → 规划 → 执行 → 审核 → 可视化 → 报告 → 评审，产出图文分析报告或安全审计报告。

它的立足点不是"接上了大模型"，而是**报告里的数字能不能被验证**：检测标准写在确定性的场景包里，
每条结论都由一份算法路径不同的独立实现重算比对，模型只负责规划与解读，不负责判定。

---

## 解决的场景

| 场景 | 输入 | 产出 |
| :--- | :--- | :--- |
| 业务数据分析 | 门店销售 CSV | 图文报告：关键指标 + 趋势图 + 结论 |
| 登录日志安全审计 | 日志平台导出的认证 CSV | 审计报告：发现清单 + 原始证据行 + 处置建议 |
| **SOC 多源告警分诊** | 认证日志 + 资产台账 + EDR 告警（三张表，列名不统一） | 分诊报告：按严重级排序的处置队列 + 证据链 + 研判摘要 |

第三个场景是这个项目要证明的事：**"生产域主机的异常告警"这句话，在只有认证日志的世界里无法回答**——
`是否生产` 在资产台账里。它需要的是跨表寻址、列别名归一、连接基数预检，而不是给通用 agent 加一句 prompt。

## 效果

| 指标 | 读数 |
| :--- | :--- |
| 数字可追溯率（报告里每个结论数字都能找到出处） | 冻结评测集 28 题全量均值 **100%** |
| 每条发现的独立复算一致率 | **5,810 / 5,810**（100 万行数据，四规则逐条重算比对） |
| 100 万行 × 4 条规则的端到端耗时 | **28.7 秒**（离线确定性模式，引擎/沙箱/复算全部真实执行） |
| 回归 | 冻结评测集当**合并门禁**，破了直接 exit 1；离线模式零 API Key、可复现 |
| 结论错误的可见性 | 每条发现的 `(主体, 指标, 数值)` 与独立复算逐格比对——把 A 机器的 7 次写成 9 次会判红 |

## 怎么做到的

- **判定权不下放给模型。** 检测标准来自场景包（`rules.yaml`：severity / 阈值 / 处置建议 / 检测规格）；
  日志属于攻击者可控输入，外部文本一律标 `untrusted`，只作证据，不进判定的那一步。
- **生产 ≠ 校验。** 每条规则同时带一份 pandas 参考实现与一份纯标准库的异构校验器，独立复算后再对齐。
- **报告分三档，且分档是闸门不是排版。** 事实层（规则算出了什么）/ 处置层（按目录给）/ 推断层（模型解读）。
  系统检查"算出来的每条发现都出现在事实层与处置层"、"推断层没有凭空出现的数字"。
- **换场景只加一个包，不改框架。** 跨表规则按**角色**寻址数据（`requires: {auth: [...], assets: [是否生产]}`），
  所以上传顺序变了、表 id 变了，结论不会跟着变。
- **能力面可关停、可归因。** 方法以 Skill 装载且不得扩权（用运行时同一个白名单函数校验），
  外部事实走 MCP 且默认只读、只作证据。
- **它是个服务，不只是一个脚本。** 队列 + 独立 worker + 带租约的原子认领、事件落库可断线续读、
  幂等提交、全链路 trace、多租户隔离与配额。

## 技术栈

**后端** Python 3.11 · FastAPI · pandas · pydantic v2 · SQLAlchemy async + Alembic（数据源由环境变量决定，默认 SQLite）·
标准库实现的 LLM 客户端（OpenAI 兼容协议，带退避重试与降级链） · 子进程沙箱执行 · 纯标准库 MCP server
**前端** Vue 3 · Vite · Element Plus（鉴权流 + SSE 实时进度 + 报告与证据链视图）
**质量** pytest · 冻结评测集与基线比对 · 变异测试（验证判据真的会红）

## 架构

```mermaid
flowchart LR
    Q[问题 + 数据文件] --> IN[摄取 / 归一化]
    IN --> EX[探查] --> PL[规划]
    PL -->|任务 DAG| PF[跨表基数预检<br/>+ 表级授权]
    PF --> EXE[执行<br/>沙箱子进程]
    EXE --> INS[审核<br/>异构校验器重算]
    INS --> VIS[可视化] --> REP[报告]
    REP --> CR[评审<br/>确定性闸门 + 语义评审]
    CR -.->|不合规 → 回流重写| REP
    INS -.->|数值不一致 → 重做| EXE
```

只有**规划 / 执行 / 可视化 / 报告 / 评审**会用到模型，摄取、独立复算、闸门判定与产物落盘都是确定性代码——
这是同一套流程可以离线复现、可以当门禁的原因。

## 一份真实产出（SOC 三源分诊）

```markdown
> 用户问题：生产域主机的异常告警有哪些？哪些需要立刻处置
> 数据源：auth.csv 282 行、assets.csv 24 行、edr.csv 4 行
> 命中统计：T1=1，T3=4，T4=1
> 独立复算：全部命中数值经独立复算比对一致

| 优先级 | 严重级   | 规则                                     | 主体               | 指标                 | 数值 |
| :----- | :------- | :--------------------------------------- | :----------------- | :------------------- | ---: |
| 1      | critical | 生产域主机大量认证失败（资产加权）（T3） | 10.0.0.15          | 生产域失败次数       |    3 |
| 5      | critical | EDR 高危告警且伴随认证失败（T4）         | 10.0.0.15          | 高危告警伴随失败次数 |    3 |
| 6      | high     | 认证爆破（T1）                           | 203.0.113.7->admin | 窗口内失败次数       |    9 |
```

真实 LLM 模式下，模型在这一份报告里做的是**跨规则的判断**：它认出 `10.0.0.15` 同时命中 T3 与 T4、
因此优先级高于只命中 T3 的三台主机，并把它排到处置第一位——而它没有引入任何新的数字。
每条发现下面都跟着原始日志行，处置建议取自规则目录，不由模型发明。

## 快速开始

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -e .

python scripts/generate_triage_data.py            # 三源演示数据（固定 seed）
python scripts/run_analysis.py \
  --data demo/data/triage/auth.csv demo/data/triage/assets.csv demo/data/triage/edr.csv \
  --question "生产域主机的异常告警有哪些？哪些需要立刻处置" --pack sigma_triage --mode mock
```

Windows 用 `.\.venv\Scripts\python.exe` 顶替 `python`。产物在 `outputs/run_<id>/`：
`report.md` 给人读，`evaluation.json` 给机器读，`transcript.jsonl` 是逐事件审计。

真实 LLM 模式把 `--mode mock` 换成 `--mode real`（需要一份 `.env`，建议先跑
`python scripts/preflight_llm.py --models <型号>` 做凭据与响应形状预检）。

## 换场景

```text
packs/<名称>/
├── rules.yaml          # 规则目录：severity · 阈值 · 处置建议 · 参考实现 + 异构校验器
├── report_template.md  # 报告模板（分档结构）
└── data_convention.md  # 列映射 · 时间格式 · 窗口语义 ·（多源）列别名
```

新增一个场景 = 写一个新包，不新增 Agent、不改编排器。内置两个当参照：`login_audit`（单表 4 规则）、
`sigma_triage`（三源 3 规则，含资产加权与跨表合取）。

## Web 界面

```bash
python -m uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev      # http://localhost:5173
```

登录 → 上传数据（支持一次多文件建三源 Bundle）→ 工作台提问并看实时 Agent 进度 → 报告与证据链 → 会话多轮 → 历史。
接口全量鉴权，数据按企业隔离，跨企业访问统一 404。

## 测试与评估

```bash
python -m pytest -q                    # 全量测试
python scripts/run_eval.py             # 冻结评测集 + 回归门禁（破了 exit 1）
python scripts/run_eval.py --check-golden
```

## 文档

[项目总览.md](项目总览.md) 是设计文档入口；架构与安全边界见
[前后端设计方案.md](前后端设计方案.md)、[安全与隔离设计.md](安全与隔离设计.md)、
[子Agent设计.md](子Agent设计.md)、[评估方案.md](评估方案.md)。
指标逐批读数与缺陷台账在 [评估记录.md](评估记录.md) 与 [工作规划.md](工作规划.md)。

许可：尚未指定。
