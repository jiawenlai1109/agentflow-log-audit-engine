# 多智能体数据分析引擎（agentflow）

本地、可验证、可扩展的多智能体数据分析/审计运行时：自然语言提问 + 本地数据文件，七个 Agent（探查 → 规划 → 执行 → 审核 → 可视化 → 报告 → 评审）协作产出图文报告或安全审计报告。核心是 **harness engineering**——用确定性工程外壳（自愈执行、独立校验 producer≠verifier、错误路由、grants 授权、全量审计）包住概率性的 LLM 输出。

**两个内置场景**（场景包架构，换场景只加配置不改框架）：

| 场景 | 数据 | 产出 |
| :--- | :--- | :--- |
| 零售数据分析 | 门店销售 CSV | 图文分析报告（数字 + 趋势图） |
| **登录日志安全审计** | 日志平台导出的登录 CSV | 审计报告（发现清单 + 证据行 + 处置建议 + 研判摘要） |

支持离线 Mock 模式（无需 API Key，确定性可复现）与真实 LLM 模式（OpenAI 兼容协议）。

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -e .

# 2. 生成 demo 数据（零售 + 登录日志两套，seed=42 可复现）
.\.venv\Scripts\python.exe scripts\generate_demo_data.py
.\.venv\Scripts\python.exe scripts\generate_login_data.py

# 3a. 零售分析（mock 离线）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\retail_sales.csv --question "总销售额是多少？" --mode mock

# 3b. 登录日志安全审计（mock 离线，约 10 秒）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\login_auth.csv --question "对今天的登录日志做安全审计" --pack login_audit

# 3c. 真实 LLM 模式（先配置 .env：OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\login_auth.csv --question "对今天的登录日志做安全审计" --mode real --pack login_audit

# 4. 运行测试（66 个用例）与批量评估
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\run_batch.py --suite pack --mode mock   # 场景包批次
.\.venv\Scripts\python.exe scripts\evaluate.py                             # 聚合入评估记录.md
```

产物在 `outputs/run_<id>/`：`report.md`（报告）、`transcript.jsonl`（全量过程审计）、`evaluation.json`（状态/校验/成本指标）、`plan.json`（任务规划）。

## 场景包机制

场景 = `packs/<名称>/` 三件套，框架只提供机制，领域知识全部在包内：

```text
packs/login_audit/
├── rules.yaml            # 检测规则目录：R1 爆破 / R2 爆破后成功 / R3 非常规时段 / R4 口令喷洒
│                         #   每条含 severity、确定性处置建议、检测规格、
│                         #   reference_code（pandas 生产参考实现）+ verify_code（纯 Python 异构独立校验器）
├── report_template.md    # 审计报告模板（发现清单/处置建议/研判摘要/审计说明）
└── data_convention.md    # 数据约定（列映射、时间格式、窗口语义、subject 格式）
```

三条核心设计原则：

1. **LLM 不判危险**——检测标准来自确定性规则包，Planner 不调 LLM，LLM 只按规格写实现和写叙述；日志是攻击者可控输入（威胁模型 T4），判定权不放在攻击者可写的文本下游；
2. **producer ≠ verifier**——每条 finding 由算法路径异构的独立校验器重算比对，数值不一致直接 FAIL 并留重算值；
3. **空结果语义反转**——安全场景"无发现"是好消息（rows==0 → PASS），由规则包重载通用审核语义。

报告建议分三档：发现清单（证据层，数字确定性渲染）/ 处置建议（规则包 disposition）/ 研判摘要（LLM 推断，强制标注"处置前请人工复核证据行"）。

新增一个场景 = 写一个新包（规则 + 模板 + 约定），不新增 Agent、不改编排器。

## 验收用例

| 用例 | 输入 | 预期 |
| :--- | :--- | :--- |
| TC-01 | 零售 CSV + "总销售额是多少？" | success，报告含数字结论 |
| TC-02 | 零售 CSV + "最近7天每日销售额的走势如何？" | success，含折线图 |
| TC-03 | 零售 CSV + "分析一下上周的利润情况"（无利润列） | degraded，提示字段缺失（错误路由→重规划→澄清） |
| 场景包 E2E | 登录日志（含植入攻击）+ `--pack login_audit` | success，4 规则命中且独立校验一致 |
| 场景包空语义 | 登录日志（无攻击）+ `--pack login_audit` | success，"无发现" PASS |
| 注入防线 | 日志 message 字段含提示词注入文本 | 检测结论不变，注入文本作为证据行留档 |

## Web 前后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:5173（默认账号 admin / admin）
```

登录 → 数据管理（上传 CSV）→ 分析工作台（提问 + SSE 实时 Agent 进度）→ 会话管理（多轮记忆）→ 历史与报告。设计详见[前后端设计方案.md](前后端设计方案.md)。

## 目录结构

```text
src/agentflow/
├── core/       # 消息、LLM、工具（白名单+grants）、执行后端、上下文、记忆、
│               #   独立校验(verification.py)、场景包机制(pack.py)、预算、编排器
├── agents/     # 七个角色 Agent（explorer/planner/executor/inspector/visualizer/reporter/critic）
├── schemas/    # pydantic 模型（对应《输出格式设计.md》）
└── pipeline.py # 端到端组装 run_analysis（支持 pack 参数）
packs/          # 场景包（领域规则包 + 报告模板 + 数据约定）
scripts/        # CLI 入口、demo 数据生成（零售/登录日志）、批量跑测(run_batch)、评估聚合(evaluate)
demo/data/      # 固定验收数据集
tests/          # 66 个自动化测试（单元/机制/端到端/API 全流程）
outputs/        # 运行产物（不入 git）
```

## 文档地图

[项目总览.md](项目总览.md) 是设计文档总入口（需求/交互/输出格式/恢复回滚/上下文记忆/子 Agent/安全隔离/评估/技术实现）。面试向的完整讲解（痛点、策略清单、量化指标、Q&A）见[简历项目讲解文档.md](简历项目讲解文档.md)；批次级数字与缺陷记录见[评估记录.md](评估记录.md)。

## 测试与质量

```powershell
.\.venv\Scripts\python.exe -m pytest -q     # 66 passed
```

测试基线演进：18 → 31 → 39 → 48 → 54 → 66；real 模式经五批迭代收敛（2/2 success + 独立校验 8/8 + 评审 2/2 PASS），逐批数字与缺陷修复记录见评估记录.md。
