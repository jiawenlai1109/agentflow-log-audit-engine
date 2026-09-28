<template>
  <div>
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
          <el-switch v-model="asyncParse" active-text="后台解析" />
          <el-button type="primary" :disabled="!pending.length" :loading="creating" @click="create">
            建包
          </el-button>
          <el-button link @click="pending = []">清空选择</el-button>
        </div>

        <el-card v-for="bundle in bundles" :key="bundle.bundle_id" class="bundle" shadow="never">
          <div class="head">
            <b>{{ bundle.name }}</b>
            <el-tag :type="statusType(bundle.status)" size="small">{{ bundle.status }}</el-tag>
            <span class="meta">
              {{ bundle.file_count }} 个文件 · {{ bundle.table_count }} 张表 ·
              {{ bundle.document_count }} 份证据
            </span>
            <el-button link type="danger" @click="removeBundle(bundle)">删除</el-button>
          </div>
          <el-alert v-if="bundle.error" :title="bundle.error" type="warning" :closable="false" />

          <el-table :data="bundle.files" size="small" style="margin-top: 8px">
            <el-table-column prop="filename" label="文件" min-width="160" />
            <el-table-column label="结果" width="110">
              <template #default="{ row }">
                <el-tag :type="kindType(row.kind)" size="small">{{ kindLabel(row.kind) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="table_ref" label="表" width="70" />
            <el-table-column label="行数/大小" width="140">
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
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="filename" label="文件名" />
          <el-table-column prop="row_count" label="行数" width="100" />
          <el-table-column prop="size" label="大小(字节)" width="120" />
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
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";

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

function statusType(status: string) {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
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

async function refresh() {
  const [datasetsResponse, bundlesResponse] = await Promise.all([api.listDatasets(), api.listBundles()]);
  datasets.value = datasetsResponse.data;
  bundles.value = await Promise.all(bundlesResponse.data.map((item: any) => fetchBundle(item.bundle_id)));
  const stillParsing = bundles.value.some((item: any) => item.status === "parsing");
  if (stillParsing) setTimeout(refresh, 1200);
}

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
    results.value[bundle.bundle_id] =
      finished.status === "success" ? `完成：${finished.run_id}（报告页可看）` : `未成功：${finished.status}`;
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

async function removeBundle(bundle: any) {
  await api.deleteBundle(bundle.bundle_id);
  refresh();
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
  await api.deleteDataset(row.id);
  refresh();
}

onMounted(refresh);
</script>

<style scoped>
.drop {
  padding: 18px;
  text-align: center;
  color: #303133;
}
.drop .hint {
  color: #909399;
  font-size: 12px;
}
.actions {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 12px 0;
}
.bundle {
  margin-bottom: 14px;
}
.bundle .head {
  display: flex;
  gap: 10px;
  align-items: center;
}
.bundle .meta {
  color: #909399;
  font-size: 12px;
}
.joins {
  margin-top: 8px;
  color: #606266;
  font-size: 12px;
}
.ask {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 10px;
}
.risk {
  color: #e6a23c;
}
</style>
