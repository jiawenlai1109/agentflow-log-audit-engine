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

# 4. 运行测试（161 个用例）与批量评估
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\run_batch.py --suite pack --mode mock   # 场景包批次
.\.venv\Scripts\python.exe scripts\evaluate.py                             # 聚合入评估记录.md

# 5. 冻结评测集门禁（17 题 mock 全量，破了 exit 1）
.\.venv\Scripts\python.exe scripts\run_eval.py                             # 与 evals\baseline.json 比对
.\.venv\Scripts\python.exe scripts\run_eval.py --check-golden              # 只核对 golden 是否漂移
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

## 评估 harness

自动化评估是这套运行时的一部分，不是事后补的报表：

```text
evals/suite.yaml        # 冻结评测集 17 题（评估方案 §7 的 15 题 + 空语义 + 一致性）
evals/baseline.json     # 基线：逐题结论 + 聚合指标 + 指纹
scripts/run_eval.py     # runner → 断言 → 基线比对 → exit code（CI 门禁）
src/agentflow/core/grading.py  # 20 个确定性谓词 + 数字可追溯率 + 归因指纹
tests/test_grading.py   # 给尺子本身写的 58 个用例
.github/workflows/ci.yml # push/PR：golden 自检 → pytest → mock 评测集门禁
```

三条立场：① **grader 也守 producer ≠ verifier**——只读 `evaluation.json` / `report.md` / `transcript.jsonl` / `plan.json`，不读 LLM 自述；② **golden 独立重算**——suite 里的字面量与数据文件对不上时报"漂移"而非"系统失败"，两类红分开；③ **能力边界是记账不是宽容**——每题分 `gate`（破了就红）与 `gap`（已知做不到，红不阻塞，**变绿报 XPASS 逼重新分类**）。

核心指标是**数字可追溯率**：报告里的每个数字都要能在证据里找到出处（当前 mock 全量均值 65.69%，缺口来自 Reporter 明细表把样例行原样打进报告，已记为 gap）。

门禁有效性用**变异测试**验证（四条位点：空语义反转、谓词名拼错、拆掉评分器 IP 掩码均被抓住；改 `max_llm_calls` 漏过——顺藤挖出 `config/agents.yaml` 从未被加载的接线缺陷，详见工作日志 2026-09-28）。

## 验收用例

| 用例 | 输入 | 预期 |
| :--- | :--- | :--- |
| TC-01 | 零售 CSV + "总销售额是多少？" | success，报告含数字结论 |
| TC-02 | 零售 CSV + "最近7天每日销售额的走势如何？" | success，含折线图 |
| TC-03 | 零售 CSV + "分析一下上周的利润情况"（无利润列） | degraded，提示字段缺失（错误路由→重规划→澄清） |
| 场景包 E2E | 登录日志（含植入攻击）+ `--pack login_audit` | success，4 规则命中且独立校验一致 |
| 场景包空语义 | 登录日志（无攻击）+ `--pack login_audit` | success，"无发现" PASS |
| 注入防线 | 日志 message 字段含提示词注入文本 | 检测结论不变，注入文本作为证据行留档 |
| 鉴权 401 | 匿名请求 15 个受保护端点 | 全部 401（`/api/health`、`/api/auth/login` 除外） |
| 归属隔离 | 用户 B 访问用户 A 的数据集 / 会话 / 任务 / 报告 | 全部 404，不泄露资源是否存在 |
| 产物收口 | 同一图表 URL 去掉 `?t=`、或用 API token 冒充媒体 token 请求 | 均 401；`report.md` 经产物路由直读 404 |

## Web 前后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:5173（默认账号 admin / admin）
```

登录 → 数据管理（上传 CSV）→ 分析工作台（提问 + SSE 实时 Agent 进度）→ 会话管理（多轮记忆）→ 历史与报告。设计详见[前后端设计方案.md](前后端设计方案.md)。

**Web 层鉴权（2026-09-27 起强制）**：

- 除 `/api/health` 与 `/api/auth/login` 外，全部接口要求 `Authorization: Bearer <token>`；token 为加签自包含串，含 `exp` 与 `scope`，默认 12 小时过期（`APP_TOKEN_TTL_SECONDS` 可调）。
- 数据按 `user_id` 隔离：数据集 / 会话 / 任务 / 报告 / 历史列表都只看得见自己提交的资源；跨用户访问统一返回 404（不区分"不存在"与"别人的"，避免资源枚举）。
- 口令存储为加盐 PBKDF2-HMAC-SHA256；种子账号口令可用 `ADMIN_PASSWORD` 覆盖（默认 `admin`，启动时会告警）。
- 产物目录不再公开挂载：`/outputs/<run>/<file>` 只放行图片，且需报告接口签发的**只读媒体 token**（`?t=`，900 秒、绑定单个 run、scope 与 API token 互斥）；`report.md`、`evaluation.json` 只能经带归属校验的 API 读取。
- 环境变量：`APP_SECRET`（签名密钥，未设置则每次启动随机、重启即令全部 token 失效）、`ADMIN_PASSWORD`、`APP_TOKEN_TTL_SECONDS`。
- 已知代价：CLI 直跑产生的 run 与鉴权上线前的历史产物不出现在 Web 历史列表中（无 `jobs` 归属记录 = 默认拒绝）。

## 目录结构

```text
src/agentflow/
├── core/       # 消息、LLM、工具（白名单+grants）、执行后端、上下文、记忆、
│               #   独立校验(verification.py)、场景包机制(pack.py)、预算、编排器
├── agents/     # 七个角色 Agent（explorer/planner/executor/inspector/visualizer/reporter/critic）
├── schemas/    # pydantic 模型（对应《输出格式设计.md》）
└── pipeline.py # 端到端组装 run_analysis（支持 pack 参数）
packs/          # 场景包（领域规则包 + 报告模板 + 数据约定）
evals/          # 冻结评测集 suite.yaml（17 题）+ 基线 baseline.json
scripts/        # CLI 入口、demo 数据生成（零售/登录日志）、批量跑测(run_batch)、评估聚合(evaluate)、门禁(run_eval)
.github/        # CI：golden 自检 → pytest → mock 评测集门禁 → 前端构建
demo/data/      # 固定验收数据集
tests/          # 161 个自动化测试（单元/机制/端到端/API 全流程/鉴权与隔离/评分器/配置接线）
outputs/        # 运行产物（不入 git）
```

## 文档地图

[项目总览.md](项目总览.md) 是设计文档总入口（需求/交互/输出格式/恢复回滚/上下文记忆/子 Agent/安全隔离/评估/技术实现）。批次级数字与缺陷记录见[评估记录.md](评估记录.md)。**六项优化的总纲、里程碑依赖与逐任务进度见[优化总纲与进度清单.md](优化总纲与进度清单.md)。**

## 测试与质量

```powershell
.\.venv\Scripts\python.exe -m pytest -q     # 234 passed
.\.venv\Scripts\python.exe scripts\run_eval.py   # 17/17 pass，gate 断言 56 全绿
```

测试基线演进：18 → 31 → 39 → 48 → 54 → 66 → 94（+28 鉴权与数据隔离用例）→ 152（+58 评分器用例）→ 158（+6 配置接线用例）→ 161（+3 M0 复检收口）→ 179（+17 Bundle 与异构入包）→ 201（+22 join 派发前预检，其中 3 例端到端）→ 214（+13 表级授权与 join 重放校验）→ 234（+20 Bundle 多文件上传，含分片/异步/越权）；real 模式经五批迭代收敛（2/2 success + 独立校验 8/8 + 评审 2/2 PASS），逐批数字与缺陷修复记录见评估记录.md，M1 变异测试四条结论与 M0 复检见工作日志 2026-09-28。
