"""LLM 接入层：OpenAI 兼容客户端（退避重试 + usage 核算）+ MockLLM 离线模式 + 结构化输出校验。"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 响应结构异常 / 预算耗尽）。"""


class OutputValidationError(RuntimeError):
    """LLM 输出无法解析为期望的结构。"""


def extract_json(text: str) -> Any:
    """从 LLM 输出中提取首个完整 JSON 对象或数组。

    容错链：剥离 Markdown 围栏 → 定位首个 { 或 [ → 括号平衡扫描 → json.loads。
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    first_open = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            first_open = i
            break
    if first_open < 0:
        raise ValueError("输出中未找到 JSON 起始符")

    stack: list[str] = []
    for i in range(first_open, len(text)):
        ch = text[i]
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            open_ch = stack.pop()
            if (open_ch == "{" and ch != "}") or (open_ch == "[" and ch != "]"):
                break
            if not stack:
                return json.loads(text[first_open : i + 1])
    raise ValueError("无法定位完整的 JSON 结构")


class BaseLLM(ABC):
    """LLM 统一接口。

    v1.2：预算计数点在每次真实 API 调用上——complete_structured 的内部校验重试、
    Summarizer、重规划等所有路径都会经过 _spend()，"30 次硬预算"不再被低估。
    """

    budget: Any = None  # BudgetCounter（由 pipeline 注入）
    agent_local = threading.local()  # 线程本地 agent 名（用于 usage 归因）

    def _spend(self) -> None:
        if self.budget is not None and not self.budget.spend():
            raise LLMError(f"LLM 调用预算耗尽（上限 {self.budget.limit} 次）")

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        """返回纯文本补全结果。"""

    def complete_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        max_retries: int = 2,
    ) -> BaseModel:
        """带 pydantic 校验的结构化输出，失败携带错误重试（≤ max_retries）。"""
        last_error: Exception | None = None
        current_messages = list(messages)
        for _ in range(max_retries + 1):
            self._spend()
            raw = self.complete(
                system=system,
                messages=current_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                data = extract_json(raw)
                return schema.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - 统一转成校验失败
                last_error = exc
                current_messages = current_messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"输出格式错误：{exc}\n请重新输出符合要求的 JSON 结构。",
                    },
                ]
        raise OutputValidationError(
            f"连续 {max_retries + 1} 次输出无法通过 {schema.__name__} 校验：{last_error}"
        ) from last_error


class OpenAILLM(BaseLLM):
    """OpenAI 兼容 Chat Completions 客户端（标准库实现，零重依赖）。

    v1.2：429/5xx/网络超时传输层退避重试（尊重 Retry-After）；采集 usage token 计入预算。
    """

    RETRYABLE_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMError("未配置 OPENAI_API_KEY（可写入 .env 或环境变量）")
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        self.timeout = timeout
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", str(max_retries)))

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "ignore")[:500]
                if exc.code in self.RETRYABLE_CODES and attempt < self.max_retries:
                    time.sleep(self._backoff(attempt, exc.headers.get("Retry-After")))
                    attempt += 1
                    continue
                raise LLMError(f"LLM HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt, None))
                    attempt += 1
                    continue
                raise LLMError(f"LLM 网络错误或超时: {exc}") from exc
        usage = data.get("usage") or {}
        if usage and self.budget is not None:
            agent = getattr(self.agent_local, "agent", "unknown")
            self.budget.add_tokens(
                agent,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
            )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常: {data}") from exc

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """指数退避 + 随机抖动；服务端给了 Retry-After 则尊重之。"""
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(2**attempt, 8) + random.uniform(0, 0.5)


class MockLLM(BaseLLM):
    """离线确定性 LLM：按 Agent 名返回预设输出，用于验收与测试。"""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides = overrides or {}

    ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
        "explorer": ("探查", "画像"),
        "planner": ("规划",),
        "executor": ("数据工程师",),
        "inspector": ("审核",),
        "visualizer": ("可视化",),
        "reporter": ("汇报",),
        "critic": ("评审",),
    }

    def _agent_name(self, system: str) -> str:
        lowered = system.lower()
        for name, keywords in self.ROLE_KEYWORDS.items():
            if any(k in system or k in lowered for k in keywords):
                return name
        return "default"

    def _executor_code(self, messages: list[dict[str, str]]) -> str:
        """按任务描述生成确定性代码（离线模式）：求和 / 按日期趋势 / 类别对比 / 缺失列报错。"""
        first = messages[0]["content"] if messages else ""
        import ast
        import re

        def extract(label: str) -> list[str]:
            match = re.search(rf"{label}：(\[.*?\])", first)
            if not match:
                return []
            try:
                value = ast.literal_eval(match.group(1))
                return list(value) if isinstance(value, list) else []
            except (ValueError, SyntaxError):
                return []

        required = extract("必需列")
        available = extract("可用列")
        desc_match = re.search(r"任务：(.+)", first)
        desc = desc_match.group(1) if desc_match else ""
        header = (
            "import json, os\n"
            "import pandas as pd\n"
            "df = pd.read_csv(os.environ['DATA_PATH'])\n"
            "num_cols = df.select_dtypes(include='number').columns.tolist()\n"
            f"_TASK_DESC = {desc!r}\n"
        )
        if required and any(col not in available for col in required):
            missing = next(col for col in required if col not in available)
            return header + f"print(df[{missing!r}].sum())\n"
        # 跨表任务（M2-3）：prompt 里列出了几张表的 env 路径与 join 键，确定性做真 join。
        # 必须排在"总/合计"分支之前——"汇总"里含"总"，否则会被单表求和抢走。
        refs = re.findall(r"os\.environ\['DATA_PATH_(T\d+)'\]", first)
        key_match = re.search(r"join 键：([^（\n,]+)", first)
        if len(refs) == 2 and key_match:
            return (
                "import json, os\n"
                "import pandas as pd\n"
                f"_LEFT = pd.read_csv(os.environ['DATA_PATH_{refs[0]}'])\n"
                f"_RIGHT = pd.read_csv(os.environ['DATA_PATH_{refs[1]}'])\n"
                f"_KEY = {key_match.group(1).strip()!r}\n"
                "merged = _LEFT.merge(_RIGHT, on=_KEY, how='inner')\n"
                "num = next((c for c in merged.select_dtypes(include='number').columns if c != _KEY), None)\n"
                "cats = [c for c in merged.select_dtypes(include=['object']).columns\n"
                "          if c != _KEY and not any(k in str(c) for k in ('日期', 'date', '时间'))]\n"
                "out = {'rows': int(len(merged)), 'columns': list(merged.columns),\n"
                "       'head': merged.head(5).astype(str).to_dict(orient='records'),\n"
                "       'aggregate': {'join_行数': int(len(merged))}}\n"
                "if num is not None and cats:\n"
                "    agg = merged.groupby(cats[0])[num].sum().sort_values(ascending=False)\n"
                "    out = {'rows': int(len(agg)), 'columns': [cats[0], num],\n"
                "           'head': [{cats[0]: str(k), num: float(v)} for k, v in agg.items()],\n"
                "           'aggregate': {'join_行数': int(len(merged)), '合计_' + num: float(merged[num].sum()),\n"
                "                           '最高_' + cats[0]: str(agg.index[0])}}\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
            )
        if any(k in desc for k in ("总", "合计", "sum")):
            return (
                header
                + "num = num_cols[0]\n"
                + "total = float(df[num].sum())\n"
                + "print(json.dumps({'rows': int(len(df)), 'columns': list(df.columns), "
                + "'head': df.head(5).astype(str).to_dict(orient='records'), "
                + "'aggregate': {'合计_' + num: total}}, ensure_ascii=False))\n"
            )
        if any(k in desc for k in ("按日期", "每日", "趋势", "走势")):
            return header + """
date_col = None
for c in df.columns:
    if any(k in str(c) for k in ('日期', 'date', '时间')):
        date_col = c
        break
if date_col is not None:
    df[date_col] = pd.to_datetime(df[date_col])
    num = num_cols[0] if num_cols else None
    if num is not None:
        agg = df.groupby(df[date_col].dt.date)[num].sum().sort_index()
        agg = agg.tail(7) if len(agg) > 30 else agg
        head = [{'日期': str(k), num: float(v)} for k, v in agg.items()]
        out = {'rows': len(head), 'columns': [date_col, num], 'head': head}
    else:
        out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
else:
    out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
print(json.dumps(out, ensure_ascii=False))
"""
        if any(k in desc for k in ("对比", "哪个", "最高", "排名")):
            return header + """
cat_cols = df.select_dtypes(include=['object']).columns.tolist()
cat_cols = [c for c in cat_cols if not any(k in str(c) for k in ('日期', 'date'))] or cat_cols
num = None
for kw in ('利润', '利润率', '销售额', '销量'):
    if kw in _TASK_DESC:
        num = next((c for c in num_cols if kw in str(c)), None)
        if num:
            break
num = num or (num_cols[0] if num_cols else None)
if cat_cols and num:
    import re as _re
    _m = _re.search(r'前(\\d+)', _TASK_DESC)
    _top = int(_m.group(1)) if _m else 10
    agg = df.groupby(cat_cols[0])[num].sum().sort_values(ascending=False).head(_top)
    head = [{cat_cols[0]: str(k), num: float(v)} for k, v in agg.items()]
    out = {'rows': len(head), 'columns': [cat_cols[0], num], 'head': head}
else:
    out = {'rows': int(len(df)), 'columns': list(df.columns), 'head': df.head(5).astype(str).to_dict(orient='records')}
print(json.dumps(out, ensure_ascii=False))
"""
        return (
            header
            + "print(json.dumps({'rows': int(len(df)), 'columns': list(df.columns), "
            + "'head': df.head(5).astype(str).to_dict(orient='records')}, ensure_ascii=False))\n"
        )

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        name = self._agent_name(system)
        if name in self.overrides:
            return self.overrides[name]
        if name == "executor":
            return self._executor_code(messages)
        defaults = {
            "planner": '{"question": "", "time_base": null, "tasks": []}',
            "inspector": '{"task_id": 1, "status": "PASS", "checks": [], "suggestion": null}',
            "critic": '{"verdict": "PASS", "rounds": 1, "issues": []}',
            "visualizer": (
                "import os\n"
                "import matplotlib\n"
                "matplotlib.use('Agg')\n"
                "import matplotlib.pyplot as plt\n"
                "plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']\n"
                "plt.rcParams['axes.unicode_minus'] = False\n"
                "import pandas as pd\n"
                "df = pd.read_csv(os.environ['DATA_PATH'])\n"
                "num_cols = df.select_dtypes(include='number').columns.tolist()\n"
                "ctype = os.environ.get('CHART_TYPE', 'bar')\n"
                "if ctype == 'line' and num_cols:\n"
                "    for c in df.columns:\n"
                "        if any(k in str(c) for k in ('日期', 'date')):\n"
                "            df[c] = pd.to_datetime(df[c])\n"
                "            df = df.set_index(c)\n"
                "            break\n"
                "    df.groupby(df.index)[num_cols[0]].sum().tail(30).plot(kind='line', title='Trend ' + num_cols[0])\n"
                "elif num_cols:\n"
                "    df[num_cols[0]].head(20).plot(kind='bar', title='Top-20 ' + num_cols[0])\n"
                "else:\n"
                "    df.head(10).plot(kind='bar')\n"
                "plt.tight_layout()\n"
                "plt.savefig(os.environ['CHART_PATH'], dpi=100)\n"
            ),
            "reporter": (
                "【总体概况】本次分析共完成若干任务，数据规模以表格为准。\n"
                "【趋势分析】趋势变化请结合图表查看。\n"
                "【结论建议】建议重点关注表格中的关键指标与异常值。"
            ),
        }
        if name in defaults:
            return defaults[name]
        return json.dumps(
            {"mock": True, "agent": name, "messages_count": len(messages)},
            ensure_ascii=False,
        )
