"""前端样式与反馈下限：把这一轮 UI 审查里"改完就会漂回去"的几条钉住。

为什么这些下限放在后端的 pytest 里：frontend 没有 JS 测试运行器（`package.json` 只有
dev/build/preview），而这个仓库已有的习惯就是"结构性判据写在 pytest 里"（`tests/test_auth.py`
按 AST 查授权谓词只有一份、`tests/test_scripts_encoding.py` 按行序查 stdout 加固）。
同一把尺、同一个门禁，不另起一套前端测试基建。

两类用例分开：① 文本/结构守卫（扫源码）；② **行为守卫**——直接让 node 跑
`frontend/src/ui/*.ts` 这两个共享模块（本机 node v24 支持直接 import .ts）。
只有①的话，"函数名还在、行为被改了"是全绿的——本项目为这句话付过一次学费：
一条下限"存在但没接进调用点"，全绿放行了真缺陷。

每条都带一条"故意改坏必须红"的证据（`_red_*` 与变异复测两组位点），因为
**存在性查不出来等于没查**。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"

# 这些色值是量出来的（scripts/contrast_report.py，读数 .appdata/contrast_before.txt）。
# 判据：作为正文色时必须达 WCAG 2.1 AA 常规字 4.5:1。
AA_TEXT_COLORS = {
    "#606266",  # --el-text-color-regular：6.11:1
    "#303133",  # --el-text-color-primary：13.02:1
    "#a15c00",  # --af-text-warning：5.19:1
    "#5c5f66",  # info 浅底 Tag 文字：5.82:1
    "#a03b3b",  # danger 浅底 Tag 文字：5.95:1
    "#3f7a22",  # success 浅底 Tag 文字：4.84:1
}
# 曾经写在界面里、实测不达标的色值。它们不许再作为颜色出现（注释里提到是允许的，
# 所以下面先剥注释）。
REJECTED_TEXT_COLORS = {"#909399", "#e6a23c", "#c0c4cc", "#a8abb2"}

VIEW_FILES = sorted(p.name for p in (SRC / "views").glob("*.vue"))


def strip_comments(text: str) -> str:
    """剥掉 CSS/JS/HTML 注释：注释里写"原先是 #909399"是**解释**，不是把它用出去。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    # 行注释只剥 CSS/JS 那种；URL 里的 // 不会命中，因为这里只看行首或空白后的 //
    text = re.sub(r"(?m)^(\s*)//.*$", r"\1", text)
    text = re.sub(r"\s//[^\"']*$", " ", text)
    return text


def source_of(rel: str) -> str:
    return strip_comments((SRC / rel).read_text(encoding="utf-8"))


@pytest.mark.parametrize("rel", ["views/DatasetsView.vue", "views/ReportsView.vue", "views/LoginView.vue"])
def test_no_rejected_hex_color_survives_in_the_views(rel: str):
    body = source_of(rel)
    found = {m.group(0).lower() for m in re.finditer(r"#[0-9a-fA-F]{6}\b", body)}
    bad = found & REJECTED_TEXT_COLORS
    assert not bad, f"{rel} 里还有实测不达标（<4.5:1）的色值：{sorted(bad)}；令牌见 styles/tokens.css"


def test_every_literal_text_color_passes_aa():
    """任何一处字面量 `color: #xxx` 都必须在达标清单里，否则就该改用令牌。

    这条是防漂回去的：达标清单不会自己变长，写一个新色值就必须先来量一次。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.vue")) + sorted(SRC.rglob("*.css")):
        body = strip_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"color:\s*(#[0-9a-fA-F]{6})\b", body):
            if match.group(1).lower() not in AA_TEXT_COLORS:
                offenders.append(f"{path.relative_to(SRC)}: {match.group(1)}")
    assert not offenders, f"这些正文色没在达标清单里（先跑 scripts/contrast_report.py 量）：{offenders}"


def test_progress_stream_does_not_read_the_clock_at_render_time():
    """`new Date()` 在渲染期取值 = 每来一帧，历史帧的时间全变成"现在"。

    只禁"空括号那一种"：`new Date(seen)` 拿的是随事件带进来的到页时刻，那是这条链的正解。
    """
    body = source_of("components/AgentProgress.vue")
    assert "new Date()" not in body, (
        "AgentProgress 又在渲染期取当前时间了；时间必须由推流那侧随事件捕获一次（seen_at）"
    )


@pytest.mark.parametrize(
    ("rel", "authority"),
    [
        ("components/AgentProgress.vue", "ui/status"),
        ("views/DatasetsView.vue", "ui/status"),
        ("views/ReportsView.vue", "ui/status"),
        ("views/WorkbenchView.vue", "ui/readout"),
        ("views/WorkbenchView.vue", "ui/packChoice"),
    ],
)
def test_the_display_authorities_are_wired_in(rel: str, authority: str):
    """ "有个单点"≠"这一处用的是那个单点"。按 import 查接线，不查函数名是否碰巧出现。"""
    body = source_of(rel)
    assert f'from "../{authority}"' in body, (
        f"{rel} 没有 import ../{authority}：语气/读数会退回各写一份的三元表达式"
    )


@pytest.mark.parametrize("rel", ["views/WorkbenchView.vue", "views/DatasetsView.vue", "views/ReportsView.vue", "views/MembersView.vue", "views/SessionsView.vue"])
def test_each_page_has_one_heading(rel: str):
    body = source_of(rel)
    assert 'class="page-title"' in body, f"{rel} 没有页级标题：进门第一句就是表单或表格，没有「你在哪」的锚点"


def test_views_do_not_declare_unscoped_styles():
    """非 scoped 的 `<style>` 就是一份隐式全局样式。

    原先 App.vue / ReportsView.vue / LoginView.vue 三处各写了一份，`.stat`、`.scope-note`、
    `.login-wrap` 互相可见——同名规则谁赢取决于打包顺序，而不取决于谁的意图更对。
    例外只有一条：ReportViewer 要给 v-html 渲染出来的 markdown 上样式，那份内容没有
    scope 属性，scoped 规则打不到它。
    """
    allowed = {"components/ReportViewer.vue"}
    offenders = []
    for path in sorted(SRC.rglob("*.vue")):
        rel = path.relative_to(SRC).as_posix()
        if rel in allowed:
            continue
        if re.search(r"<style(?![^>]*\bscoped\b)[^>]*>", path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, f"这些组件的样式块没写 scoped（等于往全局发规则）：{offenders}"


def test_the_token_layer_is_loaded_after_the_component_library():
    """令牌照的是覆盖 EP 自己的 Tag 变量；顺序反了就是白覆盖。"""
    body = (SRC / "main.ts").read_text(encoding="utf-8")
    assert body.index("element-plus/dist/index.css") < body.index("./styles/tokens.css"), (
        "tokens.css 必须排在 element-plus 之后，否则 Tag 文字色的覆盖不生效"
    )


def test_the_page_declares_a_color_scheme():
    """没声明 color-scheme 时，系统深色下浏览器会把表单控件/滚动条画成深色，
    而界面本身还是浅色 —— 表现是"白底输入框配深色下拉箭头"。"""
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'name="color-scheme"' in html


def test_tag_overrides_are_written_at_the_library_specificity():
    """覆盖组件库的自定义属性要**至少一样具体**。

    EP 自己写的是 `.el-tag.el-tag--warning`（0,2,0）；只写 `.el-tag--warning`（0,1,0）时
    导入顺序再靠后也赢不了。这条不是推出来的：在实跑页面用 getComputedStyle 读
    `--el-tag-text-color`，覆盖加上去之后一度仍是 #e6a23c——"令牌层加载了"与"令牌生效了"是两件事。
    """
    body = (SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
    bare = re.findall(r"^\.el-tag--[a-z]+", body, flags=re.M)
    assert not bare, f"这些 Tag 覆盖只有单个类名，会静默输给 EP：{bare}"


def test_templates_do_not_print_markdown_emphasis():
    """界面上的 `**整个企业**` 不是 markdown：浏览器把它原样印出来。

    这条是实跑撞到的——历史页那句口径说明里的星号是原字符显示的（同类三处一起按族清了）。
    产物正文里的 markdown 由 ReportViewer 渲染，不走这条。
    """
    offenders = []
    for path in sorted(SRC.rglob("*.vue")):
        if "**" in strip_comments(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(SRC).as_posix())
    assert not offenders, f"这些模板里有会原样印出来的星号：{offenders}"


def test_no_slot_content_inside_el_alert():
    """`el-alert` 的默认插槽渲染进 `.el-alert__description`，会把失败原因整段顶掉。

    这是本轮自己改出来的回归，在实跑页面上量到的：错误卡上 `.el-alert__description` 里
    躺着"按这一问法重跑"那颗按钮，而 `:description="item.error"` 一个字都没显示——
    一张没有"为什么"的错误卡，比没有卡更坏。按钮要放在 alert 之后。
    """
    offenders = []
    for path in sorted(SRC.rglob("*.vue")):
        body = strip_comments(path.read_text(encoding="utf-8"))
        for block in re.finditer(r"<el-alert\b[^>/]*>(?:(?!</el-alert>).)*?</el-alert>", body, flags=re.S):
            if "el-button" in block.group(0) or "<div" in block.group(0) or "<template" in block.group(0):
                offenders.append(f"{path.relative_to(SRC)}: {block.group(0)[:60]}…")
    assert not offenders, f"这些 el-alert 用了默认插槽，会顶掉 description：{offenders}"

# --- 故意改坏必须红：这几条是上面每条下限的反向证据 -------------------------------


@pytest.mark.parametrize("color", sorted(REJECTED_TEXT_COLORS))
def test_a_rejected_color_goes_red(color: str):
    body = f".x {{ color: {color}; }}".lower()
    found = {m.group(0).lower() for m in re.finditer(r"#[0-9a-fA-F]{6}\b", body)}
    assert found & REJECTED_TEXT_COLORS, f"量具自己看不见 {color}——那上面那条下限是空的"


def test_a_render_time_clock_goes_red():
    sample = "const t = computed(() => items.map(i => new Date().toLocaleTimeString()));"
    assert "new Date()" in sample


def test_an_unscoped_style_block_goes_red():
    sample = '<template><p>x</p></template><style>.stat { color: red }</style>'
    assert re.search(r"<style(?![^>]*\bscoped\b)[^>]*>", sample)


def test_a_literal_color_outside_the_allowlist_goes_red():
    assert "#123456" not in AA_TEXT_COLORS


# --- 行为守卫：直接跑那两个共享模块 ------------------------------------------------


def _run_node(module: str, names: tuple[str, ...], exprs: dict[str, str]) -> dict[str, object]:
    """一个 node 进程算完一批表达式。`--input-type=module` + `import()` 走的是
    node v24 自带的 .ts 直接导入（这几个模块只有类型标注，没有 enum/namespace）。"""
    if shutil.which("node") is None:
        pytest.skip("这台机器没有 node：这一组是行为用例，不拿文本用例冒充它跑过了")
    path = (SRC / module).as_uri()
    lines = [f'  results["{key}"] = {expr};' for key, expr in exprs.items()]
    script = (
        "const results = {};\n"
        f"const m = await import({json.dumps(path)});\n"
        f"const {{{', '.join(names)}}} = m;\n"
        + "\n".join(lines)
        + "\nconsole.log(JSON.stringify(results));\n"
    )
    probe = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert probe.returncode == 0, probe.stderr[-400:]
    return json.loads(probe.stdout.strip())


@pytest.fixture(scope="module")
def readout_values() -> dict[str, object]:
    return _run_node(
        "ui/readout.ts",
        ("num", "seconds", "UNKNOWN"),
        {
            "null": 'num(null)',
            "undefined": "num(undefined)",
            "empty": 'num("")',
            "zero": "num(0)",
            "string_seven": 'num("7")',
            "nan": "num(NaN)",
            "unknown_word": "UNKNOWN",
            "sec_zero": "seconds(0)",
            "sec_sub": "seconds(0.4)",
            "sec_three": "seconds(3)",
            "sec_null": "seconds(null)",
        },
    )


@pytest.fixture(scope="module")
def status_values() -> dict[str, object]:
    return _run_node(
        "ui/status.ts",
        ("tone", "label", "tooltip"),
        {
            "running": 'tone("running")',
            "pending": 'tone("pending")',
            "uppercase_running": 'tone("RUNNING")',
            "succeeded": 'tone("SUCCEEDED")',
            "failed": 'tone("failed")',
            "degraded": 'tone("degraded")',
            "unknown_enum": 'tone("啥也不是")',
            "label_running": 'label("running")',
            "label_partial": 'label("partial")',
            "label_unknown": 'label("啥也不是")',
            "tooltip_carries_raw": 'tooltip("running")',
        },
    )


def test_a_missing_readout_is_never_printed_as_zero(readout_values):
    """这一条是整个 P0-C 的落点：`?? 0` 把"没读到"印成一个结论。"""
    unknown = readout_values["unknown_word"]
    for key in ("null", "undefined", "empty", "nan"):
        assert readout_values[key] == unknown, f"{key} 被印成了 {readout_values[key]!r}，应为「{unknown}」"


def test_a_real_zero_still_reads_as_zero(readout_values):
    """反向半边：不许为了"诚实"把真的 0 也藏起来——那会藏掉"队列确实空了"这件事。"""
    assert readout_values["zero"] == "0"
    assert readout_values["string_seven"] == "7"


def test_sub_second_and_absent_durations_are_two_different_sentences(readout_values):
    assert readout_values["sec_sub"] == "<1s"
    assert readout_values["sec_zero"] == "<1s", "秒级精度下 0 的真义就是「不到 1 秒」"
    assert readout_values["sec_three"] == "3s"
    assert readout_values["sec_null"] == readout_values["unknown_word"]


def test_in_progress_is_not_rendered_as_danger(status_values):
    """原状：`:type="item.status === 'SUCCEEDED' ? 'success' : 'danger'"` ⇒ RUNNING/PENDING 全红。"""
    for key in ("running", "pending", "uppercase_running"):
        assert status_values[key] != "danger", f"{key} 又回到红色了（{status_values[key]}）"
    assert status_values["running"] == "primary"
    assert status_values["succeeded"] == "success"
    assert status_values["failed"] == "danger"
    assert status_values["degraded"] == "warning"


def test_an_unknown_status_is_not_green(status_values):
    """认不出的值给 info 而不是 success：新状态出现时不许界面上先绿一片。"""
    assert status_values["unknown_enum"] == "info"


def test_the_backend_enum_stays_readable_behind_the_chinese(status_values):
    assert status_values["label_running"] == "进行中"
    assert status_values["label_partial"] == "部分完成"
    assert status_values["label_unknown"] == "啥也不是", "认不出的原值要照样显示，别吞掉"
    assert "running" in status_values["tooltip_carries_raw"]


@pytest.fixture(scope="module")
def heading_values() -> dict[str, object]:
    return _run_node(
        "ui/reportHtml.ts",
        ("demoteHeadings",),
        {
            "h1": 'demoteHeadings("<h1>标题</h1>")',
            "nested": 'demoteHeadings("<h2>a</h2><p>b</p><h3>c</h3>")',
            "h6_floor": 'demoteHeadings("<h6>最深</h6>")',
            "text_safe": 'demoteHeadings("<p>Windows 10 h1 显示器</p>")',
            "attr_safe": 'demoteHeadings("<h1 id=\\"t\\">带属性</h1>")',
        },
    )


def test_report_headings_nest_under_the_page_title(heading_values):
    assert heading_values["h1"] == "<h2>标题</h2>"
    assert heading_values["nested"] == "<h3>a</h3><p>b</p><h4>c</h4>"


def test_the_demotion_touches_tags_only_and_stops_at_h6(heading_values):
    """正文里出现的 "h1" 字样不许被改；降到底也不许造出 `<h7>`（浏览器不认）。"""
    assert heading_values["text_safe"] == "<p>Windows 10 h1 显示器</p>"
    assert heading_values["h6_floor"] == "<h6>最深</h6>"
    assert heading_values["attr_safe"] == '<h2 id="t">带属性</h2>'


def test_report_viewer_uses_the_demoting_authority():
    """同"接线"那条：函数存在 ≠ 调用点用了它。"""
    body = source_of("components/ReportViewer.vue")
    assert 'from "../ui/reportHtml"' in body


# --- 场景包那一格（ui/packChoice.ts + Workbench 的接线） ----------------------------


@pytest.fixture(scope="module")
def pack_values() -> dict[str, object]:
    return _run_node(
        "ui/packChoice.ts",
        ("NO_PACK", "packPayload", "packLabel", "packFacts", "brokenPackNote", "sessionForPack"),
        {
            "omitted_undefined": "packPayload(undefined) === undefined",
            "omitted_null": "packPayload(null) === undefined",
            "omitted_empty": "packPayload('') === undefined",
            "omitted_no_pack": "packPayload(NO_PACK) === undefined",
            "kept_name": "packPayload('sigma_triage')",
            "label_with_count": "packLabel({ name: 'login_audit', rule_count: 4 })",
            "label_zero": "packLabel({ name: 'login_audit', rule_count: 0 })",
            "label_unknown": "packLabel({ name: 'login_audit' })",
            "facts_none": "packFacts(undefined)",
            "facts_columns": "packFacts({ name: 'x', required_columns: ['time', 'src_ip'] })",
            "pack_kills_session": "sessionForPack('login_audit', 's1') === undefined",
            "no_pack_keeps_session": "sessionForPack(undefined, 's1') === 's1'",
            "empty_pack_keeps_session": "sessionForPack('', 's1') === 's1'",
            "no_session_stays_no_session": "sessionForPack(undefined, undefined) === undefined",
            "broken_none": "brokenPackNote([])",
            "broken_one": "brokenPackNote([{ dir: 'zz', reason: 'rules.yaml 少一行' }])",
            "broken_reasonless": "brokenPackNote([{}])",
        },
    )


def test_no_selection_ever_leaves_the_building_as_an_empty_pack(pack_values):
    """`pack: ""` 在**后端**是 422 不是"没选包"（`app/routers/jobs.py:93` 明写这句）。

    所以界面上"不挂包"这个选项出去时必须整个键都不发；空串、null、哨兵值三种写法都算违约。
    """
    assert pack_values["omitted_undefined"] is True
    assert pack_values["omitted_null"] is True
    assert pack_values["omitted_empty"] is True, "空串被当成'不挂包'发出去 ⇒ 后端判 422，用户看到的是提交失败"
    assert pack_values["omitted_no_pack"] is True
    assert pack_values["kept_name"] == "sigma_triage"


def test_pack_label_keeps_zero_rules_and_unknown_rules_apart(pack_values):
    """0 条规则是包自己声明的事实，"没给条数"是这次没读到——两句话不许长成一个数。"""
    assert pack_values["label_with_count"] == "login_audit（4 条规则）"
    assert "0 条规则" in str(pack_values["label_zero"])
    assert "未取得" in str(pack_values["label_unknown"])


def test_pack_facts_show_the_columns_without_ruling_on_them(pack_values):
    assert "不挂包" in str(pack_values["facts_none"])
    assert "time、src_ip" in str(pack_values["facts_columns"])


def test_broken_pack_directories_are_named_not_evaporated(pack_values):
    """后端故意把坏目录一起返回（`packs.py:37`：藏起来用户就以为"没有这个场景"）。

    界面这一侧如果只渲染可选项、把坏目录丢掉，那句诚实就死在最后一公里。
    """
    assert pack_values["broken_none"] == ""
    assert str(pack_values["broken_one"]).startswith("1 个")
    assert "zz" in str(pack_values["broken_one"]) and "rules.yaml 少一行" in str(pack_values["broken_one"])
    assert "原因未给出" in str(pack_values["broken_reasonless"])


def test_workbench_sends_the_chosen_pack(pack_values):
    """接线守卫：单点存在 ≠ 提交那行用了它。"""
    body = source_of("views/WorkbenchView.vue")
    assert "packPayload(pack.value)" in body, "Workbench 没把选择器的值过一遍单点：空串会直接进载荷"
    assert "pack: usedPack," in body, "analyze 的载荷里没有 pack 这一格：选择器是个装饰"
    assert "sessionForPack(usedPack" in body, "提交前没过互斥单点：pack + session_id 一起发是后端明令的 422"


def test_a_packed_run_never_carries_a_session(pack_values):
    """`pack` 与 `session_id` 同时出现是 422（`tests/test_packs_api.py:384` 那条用例连着理由）。

    界面上这条不是风格问题：工作台原本"没会话就顺手建一个"，配上包之后**每一次带包的提交都会被拒**，
    而后端拒得很正确——会话只绑单文件数据集，允许带包的续轮等于第二轮静默换掉场景口径。
    """
    assert pack_values["pack_kills_session"] is True
    assert pack_values["no_pack_keeps_session"] is True
    assert pack_values["empty_pack_keeps_session"] is True, "空串在载荷里等于不挂包，会话不该被它一起带走"
    assert pack_values["no_session_stays_no_session"] is True


@pytest.mark.parametrize("rel", VIEW_FILES)
def test_no_view_prejudges_missing_columns(rel: str):
    """界面不许自己算"你的数据缺哪几列"。

    后端 preflight 比的是**归一化之后**的列（`jobs.py:139` 那条注释记着它曾用原始表头
    误拒 `login_audit` 配 `login_auth.csv`），而界面手里只有原始表头——
    在拿不到正确输入的位置下结论，就是给自己造假红。
    """
    body = source_of(f"views/{rel}")
    assert "required_columns" not in body, f"{rel} 在视图层碰了 required_columns：那里没有归一化后的列"


def test_a_view_that_guesses_columns_goes_red():
    sample = '<span>{{ pack.required_columns.join("、") }}</span>'
    assert "required_columns" in sample, "这条下限自己看不见缺列预判的形状"


# --- 模板里念到的名字，脚本里必须真的声明过 ------------------------------------------
# vite build 不检查这一条：模板里写 `{{ undefinedName }}` 编译照过、页面照渲染，那一格永远是空的。
# 本轮就撞上一次——模板写 `packBrokenNote`、脚本声明的是 `packBroken`，"装载失败的包"那行
# 永远不会出现，而 build 与 49 条下限全绿。**"代码就位 ≠ 生效"在这一族里第三次。**

_SCRIPT_WORDS = {
    "true", "false", "null", "undefined", "in", "of", "typeof", "new", "return", "void",
    "Math", "Date", "Number", "String", "Boolean", "Object", "Array", "JSON", "Infinity", "NaN",
}


def _template_bindings(template: str) -> set[str]:
    """绑定表达式里的**根**标识符：`{{ }}`、v-if/v-show/v-model/v-for 的值、`:prop` 与 `@evt`。"""
    chunks = re.findall(r"\{\{(.*?)\}\}", template, flags=re.S)
    chunks += re.findall(
        r"\b(?:v-(?:if|else-if|show|model|for)|:[\w.-]+|v-bind:[\w.-]+|@[\w.-]+)=\"([^\"]*)\"", template
    )
    names: set[str] = set()
    for chunk in chunks:
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", " ", chunk)
        for match in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)", stripped):
            names.add(match.group(1))
    # v-for 的别名是模板作用域里的局部量，不是脚本声明
    for aliases in re.findall(r'v-for="\s*\(?([^)]*?)\)?\s+(?:in|of)\s+', template):
        for part in aliases.split(","):
            if part.strip():
                names.discard(part.strip())
    # 作用域插槽的解构同样是模板局部量：`#default="{ row }"`、`v-slot:default="{ a, b }"`
    for group in re.findall(r'(?:#\w[\w-]*|v-slot(?::[\w-]+)?)="\s*\{([^}]*)\}', template):
        for part in group.split(","):
            token = part.strip().split(":")[-1].strip()
            if token:
                names.discard(token)
    for single in re.findall(r'(?:#\w[\w-]*|v-slot(?::[\w-]+)?)="\s*([A-Za-z_$][\w$]*)\s*"', template):
        names.discard(single)
    return {n for n in names if n not in _SCRIPT_WORDS and not n.startswith("$")}


def _script_declarations(script: str) -> set[str]:
    declared = set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", script))
    declared |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", script))
    groups = re.findall(r"import\s*\{([^}]*)\}\s*from", script) + re.findall(r"import\s+([\w$]+)\s*from", script)
    for group in groups:
        for part in group.split(","):
            token = part.strip().removeprefix("type ").split(" as ")[-1].strip()
            if token:
                declared.add(token)
    for props in re.findall(r"defineProps<\{([^}]*)\}>", script):
        declared |= set(re.findall(r"(?m)^\s*([A-Za-z_$][\w$]*)\??\s*:", props))
    return declared


@pytest.mark.parametrize("rel", sorted(
    [f"views/{p.name}" for p in (SRC / "views").glob("*.vue")]
    + [f"components/{p.name}" for p in (SRC / "components").glob("*.vue")]
    + ["App.vue"]
))
def test_every_name_the_template_reads_is_declared_in_the_script(rel: str):
    """模板里每个绑定都得有脚本里的声明撑着：编译器和 build 都不会替你发现它不存在。"""
    text = strip_comments((SRC / rel).read_text(encoding="utf-8"))
    template = "".join(re.findall(r"<template>(.*?)</template>", text, flags=re.S))
    script = "".join(re.findall(r"<script\b[^>]*>(.*?)</script>", text, flags=re.S))
    missing = _template_bindings(template) - _script_declarations(script)
    assert not missing, f"{rel} 的模板念了脚本里没有的名字：{sorted(missing)}（那一格会安静地永远是空的）"


def test_an_undefined_template_binding_goes_red():
    sample = "<template><p>{{ packBrokenNote }}</p></template><script setup>const packBroken = 1;</script>"
    template = re.findall(r"<template>(.*?)</template>", sample, flags=re.S)[0]
    script = re.findall(r"<script\b[^>]*>(.*?)</script>", sample, flags=re.S)[0]
    assert _template_bindings(template) - _script_declarations(script) == {"packBrokenNote"}, (
        "这条下限看不见自己那一族的形状 ⇒ 上面那条是空的"
    )


# --- 会话名字（ui/sessionTitle.ts）：优先级、空白、以及那条 200 的字界 ------------------


@pytest.fixture(scope="module")
def title_values() -> dict[str, object]:
    return _run_node(
        "ui/sessionTitle.ts",
        ("TITLE_LIMIT", "sessionTitle", "titleHint"),
        {
            "limit": "TITLE_LIMIT",
            "typed_wins": "sessionTitle('  我的分诊  ') === '我的分诊'",
            "blank_uses_question": "sessionTitle('   ', { question: '过去7天失败登录最多的源IP是谁？' }) === '过去7天失败登录最多的源IP是谁？'",
            "question_cut_at_20": "sessionTitle('', { question: 'x'.repeat(40) }).length === 20",
            "nothing_gives_readable_default": "sessionTitle('', { question: '   ', at: new Date(2026, 9, 10, 8, 9, 10) }).startsWith('未命名会话（')",
            "overlong_is_cut_to_the_limit": "sessionTitle('y'.repeat(250)).length === TITLE_LIMIT",
            "hint_typed": "titleHint('甲').includes('你写的这个名字')",
            "hint_fallback": "titleHint('', { question: '生产域主机的异常告警有哪些？' }).includes('留空则用问题前')",
            "hint_no_question": "titleHint('', { question: '' }).includes('未命名会话')",
        },
    )


def test_a_typed_session_name_wins_and_a_blank_one_never_ships(title_values):
    """空白不算名字：直接发空串，后端会照原样入库，列表里就是一个看不见的会话名。"""
    assert title_values["typed_wins"] is True
    assert title_values["blank_uses_question"] is True
    assert title_values["question_cut_at_20"] is True
    assert title_values["nothing_gives_readable_default"] is True, "连问题都没有时要给人话，不是发空串也不是 epoch"


def test_the_session_name_limit_is_the_same_number_in_three_places(title_values):
    """界面 maxlength、pydantic 的 max_length、库里那一列的宽度——三个读者一个数。

    SQLite 不认 VARCHAR 长度，超长照样存；真到那一步就是"能输入但存不下"的下一版：
    同一个名字在下拉、历史页、工作台被截成三种样子。所以这条界必须在受理层就守住。
    """
    limit = int(title_values["limit"])
    assert limit == 200
    schema = (ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
    block = schema.split("class SessionCreateRequest", 1)[1].split("class ", 1)[0]
    assert f"max_length={limit}" in block, f"SessionCreateRequest 的 title 上限不是 {limit}：界面上的字数限制是假的"
    model = (ROOT / "app" / "models.py").read_text(encoding="utf-8")
    assert "title: Mapped[str | None] = mapped_column(String(200))" in model, "sessions.title 那一列不是 200 宽"


def test_session_title_authority_is_wired_in_both_entry_points(title_values):
    """建会话有两个入口（工作台顺手建、会话页显式建），单点必须两处都接上。"""
    for rel in ("views/WorkbenchView.vue", "views/SessionsView.vue"):
        body = source_of(rel)
        assert 'from "../ui/sessionTitle"' in body, f"{rel} 没 import 名字单点：优先级会在这里各写一份"
        assert "sessionTitle(" in body, f"{rel} 建会话时没调用名字单点"


def test_session_name_hint_is_told_to_the_user(title_values):
    assert title_values["hint_typed"] is True
    assert title_values["hint_fallback"] is True
    assert title_values["hint_no_question"] is True
