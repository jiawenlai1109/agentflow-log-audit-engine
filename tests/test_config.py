"""配置接线测试。

这条链曾经整条是断的：`load_config(None)` 直接返回 DEFAULT_CONFIG，
于是 config/agents.yaml 里的旋钮一个都不生效，改它也不会改变归因指纹。
用例由变异测试（把 max_llm_calls 改 5 却跑出 7 次调用）反推出来。
"""

from pathlib import Path

from agentflow.core.config import DEFAULT_CONFIG, DEFAULT_CONFIG_PATH, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_file_is_found():
    assert DEFAULT_CONFIG_PATH == PROJECT_ROOT / "config" / "agents.yaml"
    assert DEFAULT_CONFIG_PATH.exists(), "默认配置路径解析错了，YAML 又会被静默忽略"


def test_yaml_values_take_effect_without_explicit_path():
    import yaml

    declared = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    config = load_config(None)
    for section in ("llm", "execution", "agents"):
        for key, value in (declared.get(section) or {}).items():
            assert config[section][key] == value, f"{section}.{key} 没吃到 YAML 的值"
    assert config["agents"], "agents 段应随 YAML 生效（各 Agent 的 temperature/max_tokens）"


def test_max_llm_calls_is_read_from_yaml(tmp_path):
    """变异 A 的可回归形式：改 YAML 里的成本上界，生效值必须跟着变。"""
    target = tmp_path / "agents.yaml"
    target.write_text("execution:\n  max_llm_calls: 5\n", encoding="utf-8")
    assert load_config(target)["execution"]["max_llm_calls"] == 5


def test_partial_yaml_does_not_drop_other_defaults(tmp_path):
    """section 合并：YAML 只写一个键时，其余键仍取默认值而不是消失。"""
    target = tmp_path / "agents.yaml"
    target.write_text("execution:\n  max_llm_calls: 7\n", encoding="utf-8")
    execution = load_config(target)["execution"]
    assert execution["max_llm_calls"] == 7
    for key, value in DEFAULT_CONFIG["execution"].items():
        if key != "max_llm_calls":
            assert execution[key] == value, f"{key} 被部分写的 YAML 弄丢了"


def test_missing_file_falls_back_to_defaults(tmp_path):
    config = load_config(tmp_path / "nope.yaml")
    assert config["execution"] == DEFAULT_CONFIG["execution"]


def test_ignored_top_level_section_is_not_merged(tmp_path):
    """非字典的段（写错成列表/字符串）不该把默认结构覆盖成脏值。"""
    target = tmp_path / "agents.yaml"
    target.write_text("execution: broken\n", encoding="utf-8")
    assert load_config(target)["execution"] == DEFAULT_CONFIG["execution"]
