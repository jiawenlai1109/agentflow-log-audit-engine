# 多智能体数据分析引擎（agentflow）

本地、可验证、可扩展的多智能体数据分析/审计运行时：自然语言提问 + 本地数据文件，七个 Agent（探查 → 规划 → 执行 → 审核 → 可视化 → 报告 → 评审）协作产出图文报告或安全审计报告。核心是 **harness engineering**——用确定性工程外壳（自愈执行、独立校验 producer≠verifier、错误路由、grants 授权、全量审计）包住概率性的 LLM 输出。

**三个内置场景**（场景包架构，换场景只加配置不改框架）：

| 场景 | 数据 | 产出 |
| :--- | :--- | :--- |
| 零售数据分析 | 门店销售 CSV | 图文分析报告（数字 + 趋势图） |
| 登录日志安全审计 | 日志平台导出的登录 CSV | 审计报告（发现清单 + 证据行 + 处置建议 + 研判摘要） |
| **SOC 多源告警分诊** | 认证日志 CSV + 资产台账 CSV + EDR 告警 CSV（三源列名不统一） | 分诊报告（分诊队列 + 证据链 + 处置建议 + 研判摘要 + 分诊说明） |

第三个场景是**跨表**的：「生产域主机的异常告警」这句话在只有认证日志的世界里无法回答——`是否生产` 在资产台账里。这类判断无法靠给通用 agent 加一句 prompt 得到，因为需要的列不在它拿到的那张表上。

支持离线 Mock 模式（无需 API Key，确定性可复现）与真实 LLM 模式（OpenAI 兼容协议）。

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -e .

# 2. 生成 demo 数据（零售 + 登录日志 + SOC 三源，seed 固定可复现）
.\.venv\Scripts\python.exe scripts\generate_demo_data.py
.\.venv\Scripts\python.exe scripts\generate_login_data.py
.\.venv\Scripts\python.exe scripts\generate_triage_data.py
.\.venv\Scripts\python.exe scripts\generate_triage_data.py --variant clean      # 零命中、贴阈值
.\.venv\Scripts\python.exe scripts\generate_triage_data.py --variant injected   # 同上 + 提示词注入列

# 3a. 零售分析（mock 离线）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\retail_sales.csv --question "总销售额是多少？" --mode mock

# 3b. 登录日志安全审计（mock 离线，约 10 秒）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\login_auth.csv --question "对今天的登录日志做安全审计" --pack login_audit

# 3c. SOC 多源分诊（一次传三份异构文件，跨表规则按角色寻址）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\triage\auth.csv demo\data\triage\assets.csv demo\data\triage\edr.csv --question "生产域主机的异常告警有哪些？哪些需要立刻处置" --pack sigma_triage

# 3d. 真实 LLM 模式（先配置 .env：OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\login_auth.csv --question "对今天的登录日志做安全审计" --mode real --pack login_audit

# 4. 运行测试与批量评估
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\run_batch.py --suite pack --mode mock   # 场景包批次（P1/P2 登录审计 + P3 多源分诊）
.\.venv\Scripts\python.exe scripts\evaluate.py                             # 聚合入评估记录.md

# 5. 冻结评测集门禁（27 题 mock 全量，破了 exit 1）
.\.venv\Scripts\python.exe scripts\run_eval.py                             # 与 evals\baseline.json 比对
.\.venv\Scripts\python.exe scripts\run_eval.py --check-golden              # 只核对 golden 是否漂移
```

产物在 `outputs/run_<id>/`：`report.md`（报告）、`transcript.jsonl`（全量过程审计）、`evaluation.json`（状态/校验/成本指标）、`plan.json`（任务规划）。

## 接第三方网关 / 思考档模型

接线只发四个字段（`model` / `messages` / `temperature` / `max_tokens`），所以任何 OpenAI 兼容端点都接得上；Anthropic 原生 `/v1/messages` 形状不同，要走兼容网关。思考档有三件事要知道：

1. **正文缺席有两种字面值，判据看签名不看字面值**。`content` 非字符串，**或**"空串 + `finish_reason=length` + reasoning 在场"，都判成没有可用正文并落成可诊断的 `LLMError`（形状细节带 `content_kind: null | blank`），走既有的 `LLM_ERROR` 错误路由，不会再把 `None` 或空串漏到下游炸成 `AttributeError`。每一次空正文都留在 `evaluation.json.llm_empty_content` 与 transcript 的 `llm_empty_content` 事件里，**只记形状不记正文**。
   实测背书（2026-10-06，InkStone）：`Agents-A1` 默认档交回 `content=''`、`finish_reason=length`、reasoning 1019 字、`reasoning_tokens=300`＝整个 `max_tokens`——那是思考预算被吃满的同一个形状换了个字面值，而在此之前它被当成"答成功了"（缺陷 #42：空串一路漏到下游，Executor 拿到空代码、Inspector 拿到空 JSON，报出来的错与真因毫无关系）。反过来，`content=''` + `stop` + 无 reasoning 仍按"模型什么都没说"原样交回，两类不许混成一个错误。
2. **按角色决定开不开思考**：`config/agents.yaml` 的 `llm.thinking: disabled`（或按角色 `agents.executor.thinking`）。留空 = 连 `thinking` 字段都不发，请求体与改造前逐字节相同——上游认不认这个字段各网关不同，**配置写了不等于思考真关了**，用第 3 条判断。
3. **救回来还是报错**：`llm.thinking_budget_retry: true` 时，只有"reasoning 在场 + `finish_reason=length`"这个签名才会把 `max_tokens` 按 `thinking_budget_factor`（默认 2）提一次，封顶 `max_tokens_cap`（默认 8000），并把 `recovered_with` 记进留痕。默认关：多打一次真实调用就是要多花一次钱。空串白卷与 `null` 白卷走同一条救法。
4. **网关返的不是 JSON 时不再打穿整条 run**：2xx + 非 JSON（配额页 / 登录页 / Cloudflare 页）判成 `LLMError`，附 HTTP 状态与**抹掉凭据后**的前 120 字；顶层不是对象（JSON 数组）同样判成结构异常。404 直接把两种来路说出来：`OPENAI_BASE_URL` 该带 `/v1` 没带，或该站只提供 Anthropic `/v1/messages` / OpenAI Responses 协议（本引擎暂不接）。这几类**都不退避重试**——重试一张配额页只是白烧一次调用。

### 实测型号兼容性（2026-10-06，同一个 key、同一句 prompt）

平台是 Intern InkStone，`GET /v1/models` 列 10 个型号，全部支持 OpenAI Chat（另外两种协议引擎不接）。每型号两次极小调用（默认档 / `thinking=disabled`），判据不是"HTTP 200"而是引擎真正需要的三件事：有没有可用正文、正文能不能直接 `json.loads`、每轮几秒。

| 型号 | 默认档 | `thinking=disabled` | 预检结论 |
|---|---|---|---|
| `kimi-k2.6` | 可用 1.6s | 可用 0.9s | usable |
| `minimax-m3` | 可用 1.0s | 可用 0.9s | usable |
| `qwen3.8-27b` | 可用 4.4s | 可用 0.8s | usable |
| `deepseek-v4-flash-0731` | 可用 4.5s | 可用 0.9s | usable |
| `deepseek-v4-pro-0813` | 可用 7.0s | 可用 1.5s | usable |
| `Atria-Dawn-Preview` | 可用 6.3s | 可用 4.2s | usable |
| `deepseek-v4-flash-vision` | 可用 26.0s（上一轮 2.3s） | 可用 0.9s | usable（当前配置） |
| `glm-5.3` | 可用 10.6s | 可用 2.8s，571 字散文裹 JSON，运行时取得出 | usable |
| `Agents-A1` | **交白卷**：空串 + `finish_reason=length` + reasoning 997 字（#42 形状）3.0s | 可用 0.6s | usable_if_thinking_disabled |
| `intern-s2` | **交白卷**：无正文 + `length` + reasoning 1077 字，21.4s | 可用 19.8s（上一轮 1.1s） | usable_if_thinking_disabled |

10 个型号：**8 个 usable、2 个必须配 `thinking: disabled`、0 个判死**。

**判据必须与运行时同一把尺**：预检问的是"引擎能不能取出可用正文"（`core/llm.extract_json`，容忍代码围栏与散文包裹），不是"整段是不是一个 JSON"。我第一版探针用了严格的 `json.loads(整段)`，把 `glm-5.3` 关思考后的输出判成"破 JSON 契约"——量具比判据更严就会造出系统其实没坏的假黑，2026-10-06 复测后已改判 usable。

**耗时不是 thinking 主导，是网关抖动主导**：同一型号两轮之间，`deepseek-v4-flash-vision` 默认档 2.3s → 26.0s，`intern-s2` 关思考档 1.1s → 19.8s。跨批次比较 real 的耗时或结论之前，先排掉这条（同一条线上还实测过同一句 prompt 的 `prompt_tokens` 报 96 与 17）。

复跑与强制：`python scripts/preflight_llm.py`（会产生真实调用，`--models` / `--levels` 可收窄），
结果写 `.appdata/llm_preflight.json`（**无正文、无凭据**）。`run_eval --mode real` 在凭据预检之后
会读这张表：**只有"探过且判死"才中止**，没缓存只出提示不拦——把"缓存必须存在"变成隐性前置条件，
等于让预检自己变成一个新的 fail-open 面。

用同一网关跑 real 批次时注意归因边界：一个网关名背后可能路由到多个上游（实测同一句 prompt 两次 `prompt_tokens` 报 96 与 17），所以 real 数字要跨批次对比，得先固定 endpoint/模型名并记录返回的 `model` 串，否则分不清是系统退化了还是上游换了。mock 门禁不受影响（零网络、确定性），CI 只跑 mock。

## 场景包机制

场景 = `packs/<名称>/` 三件套，框架只提供机制，领域知识全部在包内：

```text
packs/<名称>/
├── rules.yaml            # 检测规则目录：每条含 severity、确定性处置建议（disposition）、检测规格、
│                         #   reference_code（pandas 生产参考实现）+ verify_code（纯 Python 异构独立校验器）
│                         #   登录审计 4 条：R1 爆破 / R2 爆破后成功 / R3 非常规时段 / R4 口令喷洒
│                         #   多源分诊 3 条：T1 认证爆破 / T3 生产域大量失败（资产加权）/ T4 EDR 高危 ∧ 认证失败
├── report_template.md    # 报告模板（登录审计：发现清单/处置建议/研判摘要/审计说明；分诊：五档见下）
└── data_convention.md    # 数据约定（列映射、时间格式、窗口语义、subject 格式；多源包另有 column_aliases）
```

三条核心设计原则：

1. **LLM 不判危险**——检测标准来自确定性规则包，Planner 不调 LLM，LLM 只按规格写实现和写叙述；日志是攻击者可控输入（威胁模型 T4），判定权不放在攻击者可写的文本下游；
2. **producer ≠ verifier**——每条 finding 由算法路径异构的独立校验器重算比对，数值不一致直接 FAIL 并留重算值；
3. **空结果语义反转**——安全场景"无发现"是好消息（rows==0 → PASS），由规则包重载通用审核语义。

多源场景再加一条：**规则按角色寻址数据，不按表 id**。规则写 `requires: {auth: [auth_result, time], assets: [是否生产]}`，运行时把角色解析成真实表并注入 `DATA_PATH_AUTH` / `DATA_PATH_ASSETS`；实现与校验器共用同一个解析函数，所以两侧不可能各读一份数据。表 id（`t1`/`t2`）取决于用户先上传哪个文件——写死 id 的规则换一次上传顺序就静默指向别的表，而"静默失效的授权规则比没有规则更糟"。跨表连接在派发前经过基数预检，`src_ip` 与 `主机` 由包内列别名认定为同一实体（别名只用于识别，不改写归一化 CSV 的列名）。

报告建议分三档：发现清单（证据层，数字确定性渲染）/ 处置建议（规则包 disposition）/ 研判摘要（LLM 推断，强制标注"处置前请人工复核证据行"）。

三档不只是排版：包在 `report_layers` 里承诺哪几档算事实、哪几档算建议、哪几档算推断，`core/report_lint.py` 就把承诺变成确定性闸门——**Critic 运行时判红并回流重写，评分器 `report_layers` 谓词负责回归**，两侧共用同一份实现。断言的是：每条 finding 的主体都要出现在事实层与建议层（算出来没说出去 = 破口、有发现没处置 = 报告不可行动），且推断层出现的每个"构成结论的数字"都能在前面几档或规则阈值里找到出处（模型可以解读，不可以造数）。口径与追溯率一致：小整数不算结论，所以它拦得住"凭空写 1200 台"，拦不住"把 3 台说成 4 台"——这条边界本身有用例钉着，防止将来把它当成完备防线。

新增一个场景 = 写一个新包（规则 + 模板 + 约定），不新增 Agent、不改编排器。

## 能力面：Skill 与 MCP（M4 骨架）

领域知识放场景包，**方法**放 skill，**外部事实**走 MCP。三者都是确定性、可版本化、可关停的载体——
LLM 只写实现与叙述，判定权不下放（I2）。

```text
prompts/<角色>.md           # 9 份 system prompt（7 角色 + 摘要器 + 数据内容防线），frontmatter 带 version
skills/<方法名>/
├── SKILL.md                # L1 索引（name+description，进所有已配角色的 prompt 一行目录）
│                           # L2 正文（只进 applies_to 点名的角色，长度有上限：常驻成本要有人签字）
└── references/*.yaml       # L3 明细：决策期按需读取，每次读取在 transcript 留 hash
config/mcp.yaml             # 外部 server 白名单、能力分级、出站 SQL（配置给出，不由模型现编）
```

装载链条上有三条不谈判的规矩：

1. **skill 不得自带扩权**。`requires_tools` 与运行时白名单（`registry.whitelist_for()`，也就是强制调用时用的同一个函数）取交集，交集不满 ⇒ **整只拒装**并写 `skill_refused_no_escalation`。不做"只注入能装的那半只角色"：半份方法正文比没有方法更危险。`applies_to` 点了不存在的角色同样拒装——否则"名字写错"会表现成"方法安静地没生效"。
2. **方法要被确定性消费**。`chart_selection` 的规则表就是 `visualizer` 选图的依据（`when` 是封闭谓词集合，不是表达式语言：给 yaml 开 `eval` 等于把代码执行权交给配置文件）。关掉它，系统没有选图依据 ⇒ **不画图并说明原因**，而不是退回一套藏在代码里的默认规则。
3. **能力面必须可归因**。skill 文件 hash 进 `run_config.json`（`skill:<名称>`），注入动作与 L3 读取进 transcript，装了/拒了/关了连同原因进 `evaluation.json`。关掉一只 skill 之后 E26 的 `chart_type` 与 `skills_active` 两条 gate 会同时红——这条已用变异复测验证过，不是推断。

MCP 侧复用现有的四条强制链（白名单 / 参数守卫 / 审计 / 预算），工具以 `mcp:<server>:<tool>` 注册进同一张 ToolRegistry，**不另开通道**。三条安全线：外部结果一律标 `untrusted` 且默认 `evidence_only`（只作证据，进不了数字来源池，所以它一旦被引用追溯率就判红）；默认零文件系统权限（路径类入参按键拒）；出站数据标记（发了哪些键、什么形状、给了哪个 server）。能力分 `read/compute/write/network` 四级，后两级没有人在环批准就拒——**且请求根本不会发出**。骨架自带的第一个 server 是只读 SQLite 直连（stdlib、行分隔 JSON-RPC over stdio）；resources/prompts/sampling、HTTP 传输与真实 npm server 的互操作尚未实现，边界写进 `core/mcp.py` 的 docstring。

### Web 侧也能跑场景包（#33）

`GET /api/packs` 列出可用场景包（连**装载失败的目录和原因**一起给），`POST /api/analyze` 收 `pack` 字段，于是 SOC 多源分诊不再只能从 CLI 进：

```powershell
# 建三源 Bundle → 带包运行 → 轮询 job → 取报告
curl -X POST localhost:8000/api/bundles -H "Authorization: Bearer $TOK" -F "files=@demo/data/triage/auth.csv" -F "files=@demo/data/triage/assets.csv" -F "files=@demo/data/triage/edr.csv"
curl -X POST localhost:8000/api/analyze -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" `
  -d '{"question":"生产域主机的异常告警有哪些？哪些需要立刻处置","bundle_id":"<上一步返回>","mode":"mock","pack":"sigma_triage"}'
```

边界上有四条 422（都不留 `jobs` 行）：包名不在名单或形状不合法（`../`、大小写、空串）、数据缺包要求的列（**点名缺哪几列**）、`pack` 与会话续轮同时出现、请求批准了不在 `grantable_approvals` 名单里的外部工具。最后一条是"批准的第二入口不变成后门"的护栏：**运维点名哪些工具可由请求签字**，过滤只在 `McpHub.resolve_approvals` 一处发生——闸门与审计同一次解析，不会出现"审计写着没生效、系统其实批了"的分叉。`AnalyzeRequest` 是 `extra="forbid"`：拼成 `packk` 的键不会被静默当成"没选包"。

前端还差三件事（Workbench 只有单数据集选择器、`DatasetsView` 那条"追问"是第二个无进度流的调用方、选包时要禁会话），已单列待办；活体探针是 `scripts/e2e_packs.py`（真起服务 + 真 multipart + 临时库，14 项断言；不碰本机 `.appdata/app.db`）。

## 评估 harness

自动化评估是这套运行时的一部分，不是事后补的报表：

```text
evals/suite.yaml        # 冻结评测集 27 题（§7 的 15 题 + 空语义 + 一致性 + 3 道多源 + 5 道 SOC 对抗 + 2 道能力面）
evals/baseline.json     # 基线：逐题结论 + 聚合指标 + 指纹（prompt / skill / pack / harness 四类 hash）
scripts/run_eval.py     # runner → 断言 → 基线比对 → exit code（CI 门禁）
src/agentflow/core/grading.py  # 29 个确定性谓词 + 数字可追溯率 + 归因指纹
tests/test_grading.py   # 给尺子本身写的 66 个用例
.github/workflows/ci.yml # push/PR：golden 自检 → pytest → mock 评测集门禁
```

三条立场：① **grader 也守 producer ≠ verifier**——只读 `evaluation.json` / `report.md` / `transcript.jsonl` / `plan.json`，不读 LLM 自述；② **golden 独立重算**——suite 里的字面量与数据文件对不上时报"漂移"而非"系统失败"，两类红分开；③ **能力边界是记账不是宽容**——每题分 `gate`（破了就红）与 `gap`（已知做不到，红不阻塞，**变绿报 XPASS 逼重新分类**）。

还有一条 2026-10-05 补上的立场，来自运行时复检抓出的量具自身缺陷：聚合指标的门禁**只收"可比且会变坏"的量**。平均 LLM 调用次数留作硬闸门（与硬件无关，涨过 25% 拦在合并前）；平均耗时移出阻塞集、只打印提示——墙钟在 CI 与本机之间、空载与满载之间没有可比性，留在阻塞集里等于给门禁装一个必然抖的触发器，真回归会被噪声淹掉。判据只有一份（`compare_baseline` 返回"判红 / 提示"两栏，调用点必须都接走），且**提示不许借用判红那条通道**——与 #13「运维失败不进内容结论」是同一个立场。

同一天再加一条**每跑自检**：表级授权（`dataset_refs` 那道闸门）2026-09-28 就实现了，但评测层一直看不见它——mock 规划器不会生成"读未声明表"的任务，于是"闸门被摘掉"在 27 题里全绿。现在引擎在每次运行收尾自己拿一张未声明的表走**同一个调用点**要权限，被拒才记 `passed`；结果同时落 `evaluation.json`、`transcript`（带 `probe` 标记，免得留痕被读成"模型试图越权"）与新谓词 `table_denied`。三种状态分开：`passed` / `skipped`（单表 run 按 `dataset_scope` 不做收窄，**跳过不许伪装成通过**）/ `violated`（该拒没拒）。runner 侧另有一条不写在题里的下限，27 题每题都挂——**能力可以某次测不着，但不能没人知道它没接上线**。

最后一条防线补的是追溯率的盲区。一份真实分诊报告 530 处数字，三道过滤后剩 38 处，进追溯率这条线的只有 1 个——`生产域失败次数 = 7`、`命中统计 T1=1` 这些最要紧的数全在"一百以下不核对"那一段；而追溯率只问"这个数在不在账本里"，不问"这个数配的是不是这个主体"。所以加了谓词 `fact_triples`：**事实层每一行的 (主体, 指标, 数值) 与账本双向逐格相等**，账本只收"已被独立复算背书"的发现（`verdict.verification == ok`）。用例把存在理由钉死：把 10.0.0.7 的 7 印成 9（9 也在同一本账里），**追溯率前后都是 1.0 一分不掉**，而这条线判红。两条命门不许改：整格相等不用子串（时间窗里的 `13` 不算数值）、按格子集合不按列位置（"按 task_id 位置贴标签"是同族错误）。

核心指标是**数字可追溯率**：报告里的每个数字都要能在证据里找到出处（当前 mock 全量 27 题均值 100.00%）。
口径修正记录：2026-09-28 之前是 65.69%，差值来自**量具**——样例行经 `astype(str)` 后是字符串，整格数字因此没进证据池，报告引用真实数据数字被误判为"追不到出处"。修池子后 E01–E05 的追溯率断言由 gap 升为 gate（XPASS 逼出来的重新分类）。**指标上涨是测量修正，不是能力提升**；「样例行原文进报告」仍是展示层缺陷，另案跟。
第二次同类修正（M3-4）：标识符里的数字段（`sha256` 的 256、`utf-8` 的 8）不再算结论数字，多源报告引用的**每张表行数**进了证据池。改完对同一批产物跑新旧两套口径逐题对照：**旧 20 题一分未变**，所以 90.00% → 92.00% 全部来自新增题的题集构成——又是构成变化，不是能力提升。
第三次（2026-09-29，#4/#5 降级路径）：92.59% → 100.00%。原因只有一个——降级报告不再把子进程的原始 traceback 粘进正文，E07/E09 里那两个来自 `pandas/core/frame.py` 的行号（4378 / 3648）从此不出现在被核对的文本中。**这是展示层修复消掉了量具噪声，不是系统更准了**；同一批其余 25 题的追溯率一分未变。同一条修复的另一面是隐私：面向用户的报告里不再有 `.venv/...` 与项目绝对路径，完整 traceback 仍然逐字留在 `transcript.jsonl` 的 `run_failed_traceback` 事件里（**展示层脱敏、事实层不删**）。

门禁有效性用**变异测试**验证。M1 四条位点：空语义反转、谓词名拼错、拆掉评分器 IP 掩码均被抓住，改 `max_llm_calls` 漏过——顺藤挖出 `config/agents.yaml` 从未被加载的接线缺陷。M3 又加六条：删列别名、处置建议截断、注入串放进 disposition ⇒ **评测层报红并点名主体**；忽略 `primary_ref`、角色解析失败时退回主表、报告按 task_id 位置贴标签 ⇒ **只有单元层报红**，评测层对这三条是盲的（当前包的规则都读角色 env、三张表总在），盲区已归因并记在案上而不是当成已覆盖。M4 再加四条，全部围绕"能力面关了要能看出来"：`skills.disabled: [chart_selection]` ⇒ `chart_success` 由 1 变 None、task2 由 line 变 none，喂进 runner 后 E03/E04/E26 三题共 **5 条 gate 断言红**并被基线比对判成回归；删掉 `config/mcp.yaml` ⇒ E27 的 `external_evidence` 与 `transcript_has` 两红（评测器自己报 `E27 回归：pass → fail`，不需要人读产物）；把 `mcp:soc_intel:query` 从 explorer 白名单摘掉 ⇒ E27 红且失败原因直接写着"越权工具调用"，而**分析本身仍以 success 收口**（外部证据缺席不许改判定，也不该打死运行）；把只存在于外部库的数字 4242 手工写进报告 ⇒ 追溯率下跌且该数进未追溯清单。详见工作日志 2026-09-28。

## 验收用例

| 用例 | 输入 | 预期 |
| :--- | :--- | :--- |
| TC-01 | 零售 CSV + "总销售额是多少？" | success，报告含数字结论 |
| TC-02 | 零售 CSV + "最近7天每日销售额的走势如何？" | success，含折线图 |
| TC-03 | 零售 CSV + "分析一下上周的利润情况"（无利润列） | degraded，提示字段缺失（错误路由→重规划→澄清） |
| 场景包 E2E | 登录日志（含植入攻击）+ `--pack login_audit` | success，4 规则命中且独立校验一致 |
| 场景包空语义 | 登录日志（无攻击）+ `--pack login_audit` | success，"无发现" PASS |
| 多源跨表 | 三张表（认证 / 资产 / EDR）+ `--pack sigma_triage` | success，命中集合 == 从原始 CSV 独立数出的集合；两类误报陷阱均不报 |
| 上传顺序无关 | 同一批三份文件换两种顺序上传 | 分诊队列逐行一致（表 id 变了，角色解析没变） |
| 分档闸门 | 删掉报告里一条发现对应的处置建议 | Critic 与 `report_layers` 谓词**同时**点名该主体（两侧同一份实现） |
| 缺表必须拒绝 | 只给认证日志 + EDR（无资产台账） | `degraded`，报告点名缺 `是否生产`；不出具一份「看起来已完成」的分诊队列 |
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
│               #   独立校验(verification.py)、场景包机制(pack.py)、报告分档闸门(report_lint.py)、
│               #   提示词装载(prompts.py)、skill 装载(skill.py)、MCP client(mcp.py)、预算、编排器
├── agents/     # 七个角色 Agent（explorer/planner/executor/inspector/visualizer/reporter/critic）
├── mcp_servers/# 自带的 MCP server（骨架）：只读 SQLite 直连，行分隔 JSON-RPC over stdio
├── schemas/    # pydantic 模型（对应《输出格式设计.md》）
└── pipeline.py # 端到端组装 run_analysis（支持 pack / skills_dir / mcp_config 参数）
prompts/        # 9 份 system prompt（7 角色 + 摘要器 + 数据内容防线），frontmatter 带 version
skills/         # 方法包：chart_selection（含 references/rules.yaml 规则表）/ cross_table_triage
packs/          # 场景包（login_audit 单表 4 规则 / sigma_triage 三源 3 规则，含跨表加权）
config/         # agents.yaml（角色白名单与预算、skills.disabled）+ mcp.yaml（外部 server 与出站 SQL）
evals/          # 冻结评测集 suite.yaml（27 题）+ 基线 baseline.json
scripts/        # CLI 入口、demo 数据生成（零售/登录日志/SOC 三源/外部情报库）、批量跑测(run_batch)、评估聚合(evaluate)、门禁(run_eval)
.github/        # CI：golden 自检 → pytest → mock 评测集门禁 → 前端构建
demo/data/      # 固定验收数据集（含 triage/ 三源与 soc_intel.sqlite）
tests/          # 524 个自动化测试（单元/机制/端到端/API 全流程/鉴权与隔离/评分器/场景包/报告分档/skill/MCP/门禁容差/授权自检/LLM 响应形状/三元组等值）
outputs/        # 运行产物（不入 git）
```

## 文档地图

[项目总览.md](项目总览.md) 是设计文档总入口（需求/交互/输出格式/恢复回滚/上下文记忆/子 Agent/安全隔离/评估/技术实现）。批次级数字与缺陷记录见[评估记录.md](评估记录.md)。**六项优化的总纲、里程碑依赖与逐任务进度见[优化总纲与进度清单.md](优化总纲与进度清单.md)。**

## 测试与质量

```powershell
.\.venv\Scripts\python.exe -m pytest -q     # 524 passed
.\.venv\Scripts\python.exe scripts\run_eval.py   # 27/27 pass，gate 断言 192 全绿（gap 12 条按设计全红）
```

测试基线演进：18 → 31 → 39 → 48 → 54 → 66 → 94（+28 鉴权与数据隔离用例）→ 152（+58 评分器用例）→ 158（+6 配置接线用例）→ 161（+3 M0 复检收口）→ 179（+17 Bundle 与异构入包）→ 201（+22 join 派发前预检，其中 3 例端到端）→ 214（+13 表级授权与 join 重放校验）→ 234（+20 Bundle 多文件上传，含分片/异步/越权）→ 242（+5 追溯率谓词与数字池、+3 路径形态归一）→ 248（+6 包内列别名贯通跨表 join）→ 268（+20 SOC 多源分诊场景包，期望值全部从原始 CSV 独立重算）→ 289（+18 报告分档闸门、+3 评分器与量具用例）→ 366（+77 M4 能力面：prompts 32 / skill 25 / MCP 20）→ 371（+5 配置路径不再静默退默认，含变异复测用例）→ 411（+40 场景包经 Web 跑通：包名授权面、批准的可授予名单、缺列派发前预检）→ 412（+1 评测 runner 在 stdout 重定向时不再把全绿运行报成崩溃）→ 418（+6 评审器掉线不再伪装成内容结论，含"测接线"的用例）→ 424（+6 降级路径两条：错误分类可行动、报告正文不再带环境布局）→ 431（+7 守卫异常只作废自己、Bundle 缓存按顺序发布）→ 447（+16 脚本输出编码按族加下限）→ 450（+3 CI 首跑两条红的回归：摘要行不被截断吃掉、只剩栈帧时不端 `File "…"` 给用户）→ 462（+12 聚合容差的方向与「判红 / 只记录」两栏分工，含一条测接线的 AST 用例）→ 472（+10 表级授权的每跑自检：引擎自己拿未声明的表试一次，谓词 `table_denied` + runner 下限，含一条测接线的用例）→ 479（+7 LLM 响应形状：`content=null` 判成可诊断的 LLM 错误、`thinking` 档位按角色透传）→ 484（+5 空正文留痕与"预算被思考吃满"的提额重试，含封顶与签名判据）→ 497（+13 事实层三元组等值：`lint_fact_triples` 这把尺 8 例 + 谓词接线 5 例，E14/E16/E21/E23/E24 五题消费）→ 504（+7 网关交白卷按签名判定而非按字面值，加三处传输层兜底：非 JSON 判可诊断、顶层非对象判结构异常、404 说两种来路；含一条"错误正文不许回显凭据"的用例）→ 524（+20 型号预检：判据与运行时同尺、五档结论、缓存只留形状、run_eval 的 real 路径真读了这张表；+1 HTTP 错误正文也不回显凭据）；real 模式经五批迭代收敛（2/2 success + 独立校验 8/8 + 评审 2/2 PASS），逐批数字与缺陷修复记录见评估记录.md，M1 变异测试四条结论与 M0 复检见工作日志 2026-09-28。
