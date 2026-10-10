<template>
  <div>
    <h1 class="page-title">历史与报告</h1>
    <el-row :gutter="12" class="stats">
      <el-col v-for="s in stats" :key="s.key" :xs="12" :sm="8" :md="6" :lg="4">
        <el-card shadow="never">
          <div class="stat">
            <span>{{ s.label }}</span>
            <b :class="`tone-${s.tone}`">{{ s.value }}</b>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <p class="scope-note">
      上面几格是<strong>整个企业</strong>的口径（含同事跑的运行），分母 {{ num(summary.total) }} 次运行，
      不随下面的筛选变化；筛"我跑的"只筛这张表。当前列表：{{ visible.length }} 条。
    </p>

    <el-radio-group v-model="scope" style="margin-bottom: 12px">
      <el-radio-button value="all">全部（{{ runs.length }}）</el-radio-button>
      <el-radio-button value="mine">我跑的（{{ mineCount }}）</el-radio-button>
      <el-radio-button value="peers">同事跑的（{{ peerCount }}）</el-radio-button>
    </el-radio-group>

    <el-table :data="visible">
      <el-table-column prop="run_id" label="run_id" min-width="200" />
      <el-table-column label="归属" min-width="110">
        <template #default="{ row }">
          <el-tag :type="row.is_mine ? 'success' : 'info'" size="small">
            {{ row.is_mine ? "我跑的" : "同事跑的" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="question" label="问题" min-width="180" />
      <el-table-column label="场景包" min-width="120">
        <template #default="{ row }">
          <!-- null 在这里是事实而不是"没读到"：这张列表只来自 jobs 表，CLI 直跑的产物
               压根不在列表里（默认拒绝），所以没有"其实是包但没记下来"那第三种状态。 -->
          <el-tag v-if="row.pack" size="small" type="info">{{ row.pack }}</el-tag>
          <span v-else class="scope-note">不挂包</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="110">
        <template #default="{ row }">
          <el-tag :type="tone(row.status)" size="small" :title="tooltip(row.status)">
            {{ label(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="llm_calls" label="LLM调用" min-width="90" />
      <el-table-column prop="duration_seconds" label="耗时(s)" min-width="90" />
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button link type="primary" @click="openReport(row)">查看报告</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="报告" width="70%">
      <ReportViewer :content="reportContent" />
      <p class="scope-note">
        <template v-if="mediaMinutes">
          图片地址带一枚只读媒体 token，约 {{ mediaMinutes }} 分钟有效；
          过期之后重新点"查看报告"就会换一枚（有效期由后端给，不在前端抄一份数）。
        </template>
        <template v-else>
          媒体 token 有效期<strong>未取得</strong>（后端没随报告给这个数）：不要按"大概几分钟"来预判图片什么时候打不开，
          重新点一次"查看报告"总会换一枚新的。
        </template>
      </p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import ReportViewer from "../components/ReportViewer.vue";
import { num, seconds } from "../ui/readout";
import { label, tone, tooltip } from "../ui/status";

const runs = ref<any[]>([]);
const summary = ref<any>({});
const dialogVisible = ref(false);
const reportContent = ref("");
// 媒体 token 的有效期由后端随报告一起给（`media_expires_in`），前端不抄一份常数：
// 抄了之后服务端改时长，界面上那句话就成了没人背书的旧话。
const mediaExpiresIn = ref<number | null>(null);
// 缺值就是缺值。原来是 `Math.max(1, Math.round(0 / 60))`，于是"后端没给"会被印成
// "约 1 分钟有效"——那是把一次没测量冒充成一个测量结果。
const mediaMinutes = computed(() =>
  Number.isFinite(Number(mediaExpiresIn.value)) && Number(mediaExpiresIn.value) > 0
    ? Math.round(Number(mediaExpiresIn.value) / 60)
    : null
);

const stats = computed(() => {
  const c = summary.value?.status_count || {};
  return [
    { key: "total", label: "运行总数", value: num(summary.value?.total), tone: "neutral" },
    { key: "success", label: "成功", value: num(c.success), tone: "success" },
    { key: "partial", label: "部分完成", value: num(c.partial), tone: "warning" },
    { key: "degraded", label: "降级完成", value: num(c.degraded), tone: "warning" },
    { key: "failed", label: "失败", value: num(c.failed), tone: "danger" },
    { key: "duration", label: "平均耗时", value: seconds(summary.value?.avg_duration), tone: "neutral" },
  ];
});

// 后端在 /api/runs 每行上给了 is_mine（归属判据在服务端，前端只照着显示）
const scope = ref<"all" | "mine" | "peers">("all");

const mineCount = computed(() => runs.value.filter((row) => row.is_mine).length);
const peerCount = computed(() => runs.value.length - mineCount.value);
const visible = computed(() => {
  if (scope.value === "mine") return runs.value.filter((row) => row.is_mine);
  if (scope.value === "peers") return runs.value.filter((row) => !row.is_mine);
  return runs.value;
});

async function refresh() {
  const [runsRes, summaryRes] = await Promise.all([api.listRuns(), api.evaluationSummary()]);
  runs.value = runsRes.data;
  summary.value = summaryRes.data;
}

async function openReport(row: any) {
  const { data } = await api.getReport(row.run_id);
  reportContent.value = data.content;
  mediaExpiresIn.value = data.media_expires_in ?? null;
  dialogVisible.value = true;
}

onMounted(refresh);
</script>

<style scoped>
.stats {
  margin-bottom: 12px;
}
.stat {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-weight: 600;
}
.scope-note {
  color: var(--af-text-secondary);
  font-size: var(--el-font-size-extra-small);
  margin: 4px 0 12px;
}
.tone-success {
  color: var(--el-color-success-dark-2);
}
.tone-warning {
  color: var(--af-text-warning);
}
.tone-danger {
  color: #a03b3b;
}
</style>
