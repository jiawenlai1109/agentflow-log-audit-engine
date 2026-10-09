<template>
  <div>
    <h1 class="page-title">数据管理</h1>
    <el-tabs v-model="tab">
      <el-tab-pane label="Bundle（多文件 / 多类型）" name="bundles">
        <el-upload
          drag
          multiple
          :auto-upload="false"
          :show-file-list="false"
          accept=".csv,.tsv,.json,.jsonl,.txt,.log,.md,.yaml,.yml,.xml,.ini,.conf,.xlsx,.parquet"
          :on-change="onPick"
        >
          <div class="drop">
            <p>把多个文件拖进来（表：csv / tsv / json / jsonl；证据：txt / log / md / yaml…）</p>
            <p class="hint">
              大文件自动分片上传；文本与日志只进"证据"通道，不会成为数字来源。
              已选 {{ pending.length }} 个文件
            </p>
          </div>
        </el-upload>

        <div class="actions">
          <el-input v-model="bundleName" placeholder="包名称（可留空）" style="width: 200px" />
          <!-- active-text 只是旁边的一个字，不会成为开关的可访问名（实测无障碍树里它是一个没有名字的
               switch）。名字要显式给。 -->
          <el-switch v-model="asyncParse" active-text="后台解析" aria-label="后台解析" />
          <el-button type="primary" :disabled="!pending.length" :loading="creating" @click="create">
            建包
          </el-button>
          <el-button link @click="pending = []">清空选择</el-button>
        </div>

        <el-card v-for="bundle in bundles" :key="bundle.bundle_id" class="bundle" shadow="never">
          <div class="head">
            <b>{{ bundle.name }}</b>
            <el-tag :type="tone(bundle.status)" size="small" :title="tooltip(bundle.status)">
              {{ label(bundle.status) }}
            </el-tag>
            <span class="meta">
              {{ bundle.file_count }} 个文件 · {{ bundle.table_count }} 张表 ·
              {{ bundle.document_count }} 份证据
            </span>
            <el-button link type="danger" @click="removeBundle(bundle)">删除</el-button>
          </div>
          <el-alert v-if="bundle.error" :title="bundle.error" type="warning" :closable="false" />

          <el-table :data="bundle.files" size="small" style="margin-top: 8px">
            <el-table-column prop="filename" label="文件" min-width="160" />
            <el-table-column label="结果" min-width="110">
              <template #default="{ row }">
                <el-tag :type="kindType(row.kind)" size="small">{{ kindLabel(row.kind) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="table_ref" label="表" min-width="72" />
            <el-table-column label="行数/大小" min-width="140">
              <template #default="{ row }">{{ human(bundle, row) }}</template>
            </el-table-column>
            <el-table-column label="说明" min-width="220">
              <template #default="{ row }">
                <span v-if="row.reason">{{ row.reason }}<em v-if="row.hint">（{{ row.hint }}）</em></span>
                <span v-else-if="row.risk && row.risk.formula_cells" class="risk">
                  公式样单元格 {{ row.risk.formula_cells }} 处（只标记，未改写数据）
                </span>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button
                  v-if="row.kind === 'table'"
                  link
                  type="primary"
                  @click="preview(bundle, row.table_ref)"
                >
                  预览
                </el-button>
              </template>
            </el-table-column>
          </el-table>

          <div v-if="bundle.join_candidates && bundle.join_candidates.length" class="joins">
            join 候选：{{ joinText(bundle.join_candidates) }}
          </div>

          <div class="ask">
            <el-input v-model="questions[bundle.bundle_id]" placeholder="就这个包提个问题，如：总销售额是多少？" />
            <el-button :loading="running[bundle.bundle_id]" @click="ask(bundle)">提问</el-button>
            <span v-if="results[bundle.bundle_id]" class="meta">
              {{ results[bundle.bundle_id] }}
            </span>
          </div>
        </el-card>

        <el-dialog v-model="previewVisible" :title="previewTitle" width="720px">
          <el-table :data="previewRows" size="small" max-height="380">
            <el-table-column v-for="col in previewColumns" :key="col" :prop="col" :label="col" />
          </el-table>
        </el-dialog>
      </el-tab-pane>

      <el-tab-pane label="单文件 CSV（历史接口）" name="datasets">
        <el-upload :auto-upload="false" :show-file-list="false" accept=".csv" :on-change="onFileChange">
          <el-button type="primary">上传 CSV</el-button>
        </el-upload>

        <el-table :data="datasets" style="margin-top: 16px">
          <el-table-column prop="id" label="ID" min-width="64" />
          <el-table-column prop="filename" label="文件名" min-width="140" />
          <el-table-column prop="row_count" label="行数" min-width="80" />
          <el-table-column prop="size" label="大小(字节)" min-width="110" />
          <el-table-column label="列" min-width="220">
            <template #default="{ row }">{{ row.columns.join("、") }}</template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button link type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { label, tone, tooltip } from "../ui/status";

const tab = ref("bundles");
const datasets = ref<any[]>([]);
const bundles = ref<any[]>([]);
const pending = ref<File[]>([]);
const bundleName = ref("");
const asyncParse = ref(false);
const creating = ref(false);

const questions = ref<Record<string, string>>({});
const running = ref<Record<string, boolean>>({});
const results = ref<Record<string, string>>({});

const previewVisible = ref(false);
const previewTitle = ref("");
const previewRows = ref<any[]>([]);
const previewColumns = ref<string[]>([]);

const KIND_LABELS: Record<string, string> = {
  table: "成表",
  document: "证据",
  skipped: "被拒",
  pending: "待解析",
};

function kindLabel(kind: string) {
  return KIND_LABELS[kind] || kind;
}

function kindType(kind: string) {
  if (kind === "table") return "success";
  if (kind === "document") return "info";
  if (kind === "skipped") return "danger";
  return "warning";
}

function human(bundle: any, row: any) {
  const size = `${row.size} 字节`;
  if (row.kind !== "table") return size;
  const table = (bundle.tables || []).find((item: any) => item.table_ref === row.table_ref);
  return table ? `${table.row_count} 行 · ${size}` : size;
}

function joinText(candidates: any[]) {
  return candidates
    .filter((item) => item.usable)
    .slice(0, 4)
    .map((item) => `${item.left}↔${item.right} on ${item.column}（重叠 ${item.overlap}）`)
    .join("；") || "无同名列可连（需要包内列映射）";
}

// 后台解析靠这条轮询看它进没进 ready。原先是一句裸的 `setTimeout(refresh, 1200)` 且没有清理，
// 而这一条链自己续期：离开这一页之后它仍然每 1.2s 打一次接口，永不停。
let poll: ReturnType<typeof setTimeout> | null = null;

async function refresh() {
  const [datasetsResponse, bundlesResponse] = await Promise.all([api.listDatasets(), api.listBundles()]);
  datasets.value = datasetsResponse.data;
  bundles.value = await Promise.all(bundlesResponse.data.map((item: any) => fetchBundle(item.bundle_id)));
  const stillParsing = bundles.value.some((item: any) => item.status === "parsing");
  if (stillParsing) {
    poll = setTimeout(refresh, 1200);
  } else if (poll) {
    clearTimeout(poll);
    poll = null;
  }
}

onBeforeUnmount(() => {
  if (poll) clearTimeout(poll);
  poll = null;
});

async function fetchBundle(bundleId: string) {
  const { data } = await api.getBundle(bundleId);
  return data;
}

function onPick(file: any) {
  pending.value.push(file.raw as File);
}

async function create() {
  creating.value = true;
  try {
    await api.createBundle(pending.value, {
      name: bundleName.value || undefined,
      asyncParse: asyncParse.value,
    });
    ElMessage.success("已建包");
    pending.value = [];
    bundleName.value = "";
    await refresh();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || "建包失败");
  } finally {
    creating.value = false;
  }
}

async function preview(bundle: any, tableRef: string) {
  try {
    const { data } = await api.previewBundle(bundle.bundle_id, tableRef, 20);
    previewTitle.value = `${data.source_file}（${data.row_count} 行，预览前 ${data.head.length} 行）`;
    previewColumns.value = data.columns;
    previewRows.value = data.head;
    previewVisible.value = true;
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || "预览失败");
  }
}

async function ask(bundle: any) {
  const question = (questions.value[bundle.bundle_id] || "").trim();
  if (!question) {
    ElMessage.warning("先写个问题");
    return;
  }
  running.value[bundle.bundle_id] = true;
  results.value[bundle.bundle_id] = "排队中…";
  try {
    const { data: job } = await api.analyze({ question, bundle_id: bundle.bundle_id, mode: "mock" });
    results.value[bundle.bundle_id] = `任务 ${job.job_id}`;
    const finished = await waitJob(job.job_id);
    // 这一条入口走的是 mock（写死的），界面必须把"这是离线演示"说到结果那一行上。
    // 原来只在请求里带 mode:"mock"，结果行只写"完成：run_xxx（报告页可看）"——
    // 读到这一行的人没有任何线索知道这份报告不是真推理出来的。
    // （把这里改成可选模式、或干脆并到工作台那一个入口，是产品决定，见工作规划待办池。）
    results.value[bundle.bundle_id] =
      finished.status === "success"
        ? `完成：${finished.run_id}（报告页可看）· mock 演示，非真实推理`
        : `未成功：${label(finished.status)}`;
  } catch (error: any) {
    results.value[bundle.bundle_id] = error?.response?.data?.detail || "分析失败";
  } finally {
    running.value[bundle.bundle_id] = false;
  }
}

async function waitJob(jobId: string) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const { data } = await api.getJob(jobId);
    if (data.status !== "pending" && data.status !== "running") return data;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return { status: "timeout" };
}

/** 删除类操作统一先确认、失败要说出来。原先这两处是直接一个 `await` 打出去：
 *  删包连问都不问（会话倒是问了），而失败时错误只落进未处理的 Promise 拒绝里，
 *  界面上什么都不会发生——用户看到的仍是"点了没反应"。 */
async function removeWithConfirm(what: string, action: () => Promise<unknown>) {
  const confirmed = await ElMessageBox.confirm(`删除${what}？数据与已解析的表都会一起移除。`, "确认删除", {
    type: "warning",
  }).catch(() => false);
  if (!confirmed) return false;
  try {
    await action();
    ElMessage.success(`已删除${what}`);
    return true;
  } catch (caught: any) {
    ElMessage.error(caught?.message || "删除失败");
    return false;
  }
}

async function removeBundle(bundle: any) {
  if (await removeWithConfirm(`包「${bundle.name}」`, () => api.deleteBundle(bundle.bundle_id))) refresh();
}

async function onFileChange(file: any) {
  try {
    await api.uploadDataset(file.raw as File);
    ElMessage.success("上传成功");
    refresh();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || "上传失败");
  }
}

async function remove(row: any) {
  if (await removeWithConfirm(`数据集「${row.filename}」`, () => api.deleteDataset(row.id))) refresh();
}

onMounted(refresh);
</script>

<style scoped>
.drop {
  padding: 18px;
  text-align: center;
  color: var(--el-text-color-primary);
}
.drop .hint {
  /* 原先两处 #909399（实测 3.08:1，AA 要 4.5:1）。令牌见 tokens.css 顶部那张量表。 */
  color: var(--af-text-secondary);
  font-size: var(--el-font-size-extra-small);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin: 12px 0;
}
.bundle {
  margin-bottom: 14px;
}
.bundle .head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
.bundle .meta {
  color: var(--af-text-secondary);
  font-size: var(--el-font-size-extra-small);
}
.joins {
  margin-top: 8px;
  color: var(--af-text-secondary);
  font-size: var(--el-font-size-extra-small);
}
.ask {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-top: 10px;
}
.risk {
  /* #e6a23c 当正文色实测 2.19:1；换成本项目量出来达标的深一档（5.19:1）。 */
  color: var(--af-text-warning);
}
</style>
