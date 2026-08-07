# 多智能体数据分析引擎

基于多智能体协作的自动化数据分析引擎：用户输入中文业务问题并指定本地 CSV，七个 Agent 协作完成探查 → 规划 → 执行 → 审核 → 可视化 → 报告 → 评审，输出图文并茂的 Markdown 报告。支持离线 Mock 模式（无需 API Key）与真实 LLM 模式。

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖（在项目根目录）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -e .

# 2. 生成 demo 数据
.\.venv\Scripts\python.exe scripts\generate_demo_data.py

# 3. 离线运行（mock 模式）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\retail_sales.csv --question "总销售额是多少？" --mode mock

# 4. 真实 LLM 模式（先配置 .env：OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）
.\.venv\Scripts\python.exe scripts\run_analysis.py --data demo\data\retail_sales.csv --question "最近7天每日销售额的走势如何？" --mode real

# 5. 运行测试与评估聚合
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\evaluate.py
```

## 目录结构

```text
src/agentflow/
├── core/       # 消息、LLM、工具、执行后端、上下文、日志、预算、编排器
├── agents/     # 七个角色 Agent（explorer/planner/executor/inspector/visualizer/reporter/critic）
├── schemas/    # pydantic 模型（对应《输出格式设计.md》）
└── pipeline.py # 端到端组装 run_analysis
scripts/        # CLI 入口、demo 数据生成、评估聚合
demo/data/      # 固定验收数据集
outputs/        # 运行产物（不入 git）
```

## 文档地图

[项目总览.md](项目总览.md) 是总入口：文档地图、关键设计决策速查、代码生成检查表与阶段状态。设计文档包括需求分析、交互设计、输出格式设计、恢复与回滚设计、上下文与记忆设计、子 Agent 设计、安全与隔离设计、评估方案、技术实现方案。

## 验收用例（离线 Mock 模式）

| 用例 | 问题 | 预期 |
| :--- | :--- | :--- |
| TC-01 | 总销售额是多少？ | success，报告含数字结论 |
| TC-02 | 最近7天每日销售额的走势如何？ | success，含折线图 |
| TC-03 | 分析一下上周的利润情况（无利润列） | degraded，提示字段缺失 |
