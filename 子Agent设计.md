# 子 Agent 设计（v1.1）

**项目**：基于多智能体协作的自动化数据分析引擎
**配套文档**：[交互设计.md](交互设计.md) / [输出格式设计.md](输出格式设计.md) / [恢复与回滚设计.md](恢复与回滚设计.md) / [上下文与记忆设计.md](上下文与记忆设计.md)

## 1. 总体设计原则

1. **工作流优先，全自主 Agent 慎用**：业界主流观点（Anthropic "Building Effective Agents"）认为大多数生产级"Agent"实际是固定控制流的工作流；能用确定性管道解决就不要让 LLM 动态决策。本项目 7 个 Agent 全部是**工作流节点**，控制流由 Orchestrator 确定，Agent 只负责各自环节。
2. **每个 Agent 采用一种成熟单 Agent 模式**：工具调用、计划-执行、代码解释器、规则+反思、落地生成，各取所长，不混用。
3. **可验证的用代码，不可验证的才用 LLM**：列名、数字、路径、规则全部由代码保证；LLM 只做语义判断（能否回答、是否有洞察）。
4. **输出全部走 pydantic schema 校验**：工具 schema 设计比模型选择更重要，结构化输出失败自动重试。
5. **框架是战术选择，模式是战略决策**：当前自研轻量编排层 + 成熟模式组合，迁移到 LangGraph / Microsoft Agent Framework 时 Agent 协议不变。

## 2. 模式选型总表

| Agent | 采用模式 | 确定性部分（代码） | LLM 部分 | 温度 |
| :--- | :--- | :--- | :--- | :--- |
| Explorer | 工具调用 + 确定性画像 | 编码检测、列名/类型/缺失率/日期解析 | 问题描述与 issue 语义归纳（可选） | 0.2 |
| Planner | 计划-执行（Plan 阶段） | 列名校验、depends_on 拓扑校验、时间基准解析 | 任务拆分 | 0.2 |
| Executor | 代码解释器（写码-执行-调试） | 子进程执行、错误分类、超时 | 生成代码、根据报错修码 | 0.1 |
| Inspector | 规则引擎 + 语义审核 | 空结果/行数/负值/日期范围/列完整性规则 | "能否回答原始问题"语义判断 | 0.2 |
| Visualizer | 代码解释器（图表版） | 选图规则、PNG 产物校验 | 生成 matplotlib 代码 | 0.1 |
| Reporter | 落地生成（Grounded Generation）+ 模板 | 模板、数字填充、图表嵌入 | 叙述文字与洞察 | 0.4 |
| Critic | 反思（锚定可验证信号） | 章节/路径/数字一致性检查 | 语义质量评审 | 0.3 |

## 3. 公共基座 BaseAgent

所有 Agent 继承同一基座，职责：

- **统一 LLM 调用**：`call_llm(system, messages, response_schema, temperature)` → JSON 解析容错（剥离代码围栏、截取 JSON）→ pydantic 校验 → 失败携带错误重试（≤ 2 次）；
- **L1 消息历史**：有界 MessageHistory（默认 8 条，见上下文与记忆设计）；
- **工具注册表**：Agent 声明可用工具（如 execute_code / profile_csv）；
- **transcript 记录**：每次调用写入 prompt 摘要、输出、耗时、attempts；
- **预算计数**：每次 LLM 调用计入总预算（30 次）；
- 每个 Agent 实现 `run(context, message) -> AgentMessage`，输入输出遵循输出格式设计.md。

## 4. 各 Agent 详细设计

### 4.1 Explorer（数据探查员）

- **模式**：工具调用 + 确定性画像。探查是确定性任务，LLM 幻觉列名是灾难，因此列名/类型/缺失率全部由 pandas 代码计算，LLM 只做可选的语义归纳。
- **内部流程**：编码检测（utf-8-sig → GBK/GB18030 回退）→ 读取 CSV → 画像（dtype、missing_rate、unique_rate、样例、日期解析、min/max）→ 生成 SchemaProfile（pydantic）→ issues 规则（缺失率超阈值、全空列、日期解析失败）。
- **工具**：`profile_csv(data_path)`。
- **LLM 策略**：可选，用于把统计翻译成对 Planner 友好的描述与 issue 判断；不参与列名生成。
- **失败处理**：文件不存在/编码失败 → EMPTY_RESULT / CODE_ERROR，直接走降级路径。
- **边界**：大文件限制行数采样（可配置 max_sample_rows）；日期多格式容错。

### 4.2 Planner（规划师）

- **模式**：计划-执行（Plan-and-Solve）的 Plan 阶段：先拆解再执行，任务清单作为 Executor 的"锚定计划"。
- **输入**：question + SchemaProfile + 会话上下文（滚动摘要 + 近期原文）。
- **内部流程**：组装上下文 → LLM 生成 TaskList → schema 校验（required_columns ⊆ 真实列名、1~5 个任务、depends_on 拓扑有序）→ 校验失败携带错误重规划（≤ 2 次）→ 相对时间解析为 time_base。
- **LLM 策略**：温度 0.2；few-shot 给三类典型问题（汇总/趋势/对比）各一例；要求输出合法 JSON 数组（容错解析兜底）。
- **失败处理**：连续重规划失败 → LLM_ERROR，走降级；问题模糊 → 在 description / time_base 中显式声明假设。
- **边界**：任务数超过 5 → 截断并提示；空列名校验直接重规划。

### 4.3 Executor（执行者）— 核心亮点

- **模式**：代码解释器（Code Interpreter）：写码 → 执行 → 调试循环，是业内验证最充分的数据分析 Agent 模式。
- **内部流程**：
  1. LLM 生成纯 Python 代码（禁止 Markdown 围栏）；
  2. 写入 `work/<task_id>/script.py`；
  3. 本地子进程执行（`python -I`，cwd=任务私有 work 目录，env 注入 DATA_PATH / ARTIFACTS_DIR / 超时 30s）；
  4. 成功 → 结果摘要（rows/columns/head）+ 中间文件；失败 → 错误（stderr + error_class）回传 LLM 修码，最多 3 次，每次尝试独立 attempt_N 子目录；
  5. 重试耗尽 → 错误分类：MISSING_COLUMN / EMPTY_RESULT / CODE_ERROR / TIMEOUT。
- **代码规范 Prompt**：只读 DATA_PATH、结果写 ARTIFACTS_DIR、禁止访问网络、禁止写源数据目录、必须 try/except 输出结构化错误。
- **LLM 策略**：温度 0.1（代码确定性优先）；建议配置代码能力强的模型。
- **安全**：本地执行非沙箱（见需求 2.4），提示用户仅对可信数据使用；真隔离留 Docker 路线图。
- **边界**：结果过大时截断 head 摘要；浮点数保留有效位。

### 4.4 Inspector（审核员）

- **模式**：规则引擎 + 语义审核。反思模式的教训：没有可验证锚点（oracle）的纯自我批评会自证偏差，因此**可验证的用代码规则，不可验证的才用 LLM**。
- **确定性规则**（代码实现，不调 LLM）：
  - empty_check：空结果结合任务上下文判 FAIL / WARN；
  - row_count_check：对比源数据规模与筛选条件；
  - negative_value_check：负销售额按业务上下文 WARN（退款合法）；
  - date_range_check：日期列格式与范围；
  - column_completeness_check：必需列是否齐全。
- **LLM 语义检查**：仅"结果能否回答原始问题"一项，输出 PASS/WARN/FAIL + suggestion。
- **输出**：Verdict（pydantic），FAIL 时 suggestion 必填。
- **LLM 策略**：温度 0.2；输入含原始问题与 SchemaProfile。
- **边界**：规则与语义结论冲突时以规则为准并记录。

### 4.5 Visualizer（可视化师）

- **模式**：代码解释器（图表版）+ 确定性选图规则。图表类型由数据特征决定（时间序列→line、类别 Top-N→bar、占比且类别 ≤8→pie、分布→hist），LLM 不拍板。
- **内部流程**：选图 → LLM 生成 matplotlib 代码（注入中文字体 preamble：微软雅黑/SimHei）→ 子进程执行 → 校验 PNG 存在且非空 → 生成 FigureResult（file_path + note）。
- **note 由真实数据计算**：如最高/最低点由代码算出来填入，不允许 LLM 编造。
- **LLM 策略**：温度 0.1；输出 code 字段必须符合"纯代码"约束。
- **失败处理**：执行失败重试 ≤ 2 次；仍失败 → 该任务无图，报告标注"图表生成失败"。
- **边界**：图表尺寸、dpi、颜色数配置化；饼图类别超限自动降级为柱状 Top-N。

### 4.6 Reporter（汇报者）

- **模式**：落地生成（Grounded Generation）+ 模板约束，防幻觉的核心设计：**数字全部由代码预先算好，LLM 只写叙述**。
- **内部流程**：接收全部 TaskExecutionResult（只含真实摘要）+ FigureResult 路径 + time_base + 会话上下文 → 按固定模板生成报告（总体概况/数据详情/趋势分析/结论建议）→ 关键数字以占位符/表格形式由代码填充 → 图表用相对路径嵌入 → 顺带产出 1~3 句 summary（供会话记忆）。
- **Prompt 约束**：强制"只能引用输入中提供的数字，禁止计算或猜测"。
- **降级模式**：存在 failed 任务 → 降级报告模板（失败原因 + 建议）。
- **LLM 策略**：温度 0.4（叙述性）；建议配置叙述能力强的模型。
- **边界**：报告长度限制；中文字体与编码固定 UTF-8。

### 4.7 Critic（评审员）

- **模式**：反思（Reflection），对应 Anthropic 的 Evaluator-optimizer 环（生成-评审-重写 ≤ 2 轮），但**评审必须锚定可验证信号**。
- **确定性检查**（代码）：报告存在、必需章节齐全、图表路径有效、报告数字与 artifacts 比对一致。
- **LLM 语义评审**：结论是否有数据支撑、是否回答原始问题、洞察质量 → issues 列表（severity/section/message）。
- **回环**：FAIL → 携带 issues 返回 Reporter 重写（≤ 2 轮）；重写后再评审，仍不过则输出当前版本并标注"未经评审通过"。
- **LLM 策略**：温度 0.3；评审与生成使用不同角色视角，降低"自己审自己"盲区（可配置不同模型）。
- **边界**：确定性数字比对失败直接 FAIL，不进入 LLM 评审。

## 5. 框架选型说明

### 5.1 现状：自研轻量编排层 + 成熟模式

依据研究结论：

- 主流框架心智模型各不相同：LangGraph（状态图）、CrewAI（角色团队）、AutoGen（群聊，已进入维护模式，微软推荐新项目用 Agent Framework）、Microsoft Agent Framework（数据流 Workflow）、OpenAI Agents SDK（工具循环 + handoff）；
- CrewAI 这类高层抽象在"自定义重试、部分失败处理、复杂同步"上反而受限——恰好是本项目的核心需求；
- LangGraph 的状态图能力（条件边、重试回边）与我们的 DAG 调度语义一致，但引入它意味着重依赖、调试黑盒、学习成本；自研 300~500 行编排层即可覆盖，且完全透明、便于教学与评审。

### 5.2 迁移路径

- 未来若需要复杂动态分支、人机协同环、多模型路由，可将 Orchestrator 替换为 LangGraph 或 Microsoft Agent Framework；
- 替换边界：Agent 消息协议（AgentMessage + pydantic schema）不变，只重写编排层；
- 单 Agent 内部模式（代码解释器、反思等）在任何框架下都成立，不受编排层影响。

### 5.3 何时值得引入框架

- 控制流在设计期不可知（需要 LLM 动态路由）；
- 需要多人机检查点、图级断点续跑；
- 团队已在使用某生态（如 Azure Foundry 全家桶）时选 Agent Framework。

## 6. 提示词与输出可靠性约定

每个 Agent 的 system prompt 统一结构：

1. 角色与目标（一句话）；
2. 输入说明（字段与来源）；
3. 输出 JSON schema（示例）；
4. few-shot 示例（按需）；
5. 硬约束（只输出 JSON / 只输出代码 / 只引用给定数字）；
6. 失败时如何输出（error 字段约定）。

JSON 容错链：剥离 Markdown 围栏 → 提取首个合法 JSON → pydantic 校验 → 失败携带错误重试（≤ 2 次）→ 仍失败标记 LLM_ERROR。

## 7. LLM 参数总表

| Agent | 温度 | max_tokens（默认） | 结构化重试 | 备注 |
| :--- | :--- | :--- | :--- | :--- |
| Explorer | 0.2 | 800 | ≤ 2 | 可跳过 LLM 纯确定性 |
| Planner | 0.2 | 1500 | ≤ 2 | few-shot 三类问题 |
| Executor | 0.1 | 2000 | 自愈 ≤ 3 | 建议代码强模型 |
| Inspector | 0.2 | 500 | ≤ 2 | 规则优先 |
| Visualizer | 0.1 | 1500 | ≤ 2 | 中文字体 preamble |
| Reporter | 0.4 | 2000 | ≤ 2 | 叙述强模型 |
| Critic | 0.3 | 800 | ≤ 2 | 可独立模型 |

所有参数均可在 config/agents.yaml 中按 Agent 覆盖；MockLLM 模式为全部 Agent 提供确定性降级输出，保证离线可验收。
