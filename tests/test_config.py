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


def test_absent_default_path_falls_back_but_not_invisibly(tmp_path, monkeypatch):
    """默认配置是**可选**的：精简过的部署包里没有它，应该照跑。

    但"退默认"不能等于"没人看得出来"——这里断言它一定会改变归因指纹。
    （`test_mcp.py::test_absent_default_config_means_no_external_server` 测的是同一条边界在
    MCP 那一侧的表现：没配外部 server 就是不接，而不是报错。两边判据不同，都要留。）
    """
    from agentflow.core import config as config_module
    from agentflow.core.grading import fingerprint

    normal = load_config(None)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "absent.yaml")
    fallback = load_config(None)

    assert fallback["execution"] == DEFAULT_CONFIG["execution"]
    assert fingerprint(config=fallback)["config"] != fingerprint(config=normal)["config"], (
        "退默认必须改变 config hash，否则'配置没吃到'又是一次只能靠人怀疑的事故"
    )
    assert fallback["agents"] == {} and normal["agents"], "各角色的 temperature/max_tokens 段应当整段退化"


def test_explicit_missing_path_raises_instead_of_silently_defaulting(tmp_path):
    """显式给路径 = 声明"我要用这份配置"，读不到必须报错。

    这条用例的来源是一次真实的误诊：拿解释器看不见的 `/tmp` 路径跑"关停某 skill"的
    实验，旧实现静默返回 DEFAULT_CONFIG，于是`skills.disabled` 一个字都没生效，
    而程序毫无异常——"配置没生效"被伪装成"配置生效了但行为没变"。
    """
    import pytest

    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError) as caught:
        load_config(missing)
    assert "nope.yaml" in str(caught.value), "报错要点名是哪个路径读不到"
    assert "别传" in str(caught.value), "报错要给出下一步该怎么做"


def test_run_analysis_fails_loudly_on_a_bad_config_path(tmp_path):
    """缺陷的用户侧形态：`run_analysis(config_path=打错的路径)` 不许安静跑成默认配置。"""
    import pytest

    from agentflow.pipeline import run_analysis

    with pytest.raises(FileNotFoundError):
        run_analysis(
            "总销售额是多少？",
            [str(PROJECT_ROOT / "demo" / "data" / "retail_sales.csv")],
            config_path=str(tmp_path / "typo.yaml"),
            outputs_root=tmp_path / "out",
        )
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("run_*")), (
        "配置读不到就在建产物之前停，别留下一个用默认配置跑出来的 run 目录"
    )


def test_a_config_that_disables_a_skill_is_actually_read(tmp_path):
    """关停类开关必须"传了就真生效"——这正是当初误诊实验想做的那件事。"""
    from agentflow.core.skill import load_skill_set
    from agentflow.core.tools import build_default_registry

    config_file = tmp_path / "off.yaml"
    config_file.write_text("skills:\n  disabled: [chart_selection]\n", encoding="utf-8")
    config = load_config(config_file)
    assert config["skills"]["disabled"] == ["chart_selection"]
    registry = build_default_registry(config)
    skills = load_skill_set(registry=registry, disabled=config["skills"]["disabled"])
    assert {skill.name for skill in skills.installed} == {"cross_table_triage"}


def test_ignored_top_level_section_is_not_merged(tmp_path):
    """非字典的段（写错成列表/字符串）不该把默认结构覆盖成脏值。"""
    target = tmp_path / "agents.yaml"
    target.write_text("execution: broken\n", encoding="utf-8")
    assert load_config(target)["execution"] == DEFAULT_CONFIG["execution"]
