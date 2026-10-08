<template>
  <div>
    <el-row :gutter="12" style="margin-bottom: 16px">
      <el-col :span="6"><el-card><div class="stat">success：{{ summary.status_count?.success }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">partial：{{ summary.status_count?.partial }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">degraded：{{ summary.status_count?.degraded }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">平均耗时：{{ summary.avg_duration }}s</div></el-card></el-col>
    </el-row>
    <p class="scope-note">
      上面四个数是**整个企业**的口径（含同事跑的运行），不随下面的筛选变化；
      筛"我跑的"只筛这张表。当前列表：{{ visible.length }} 条。
    </p>

    <el-radio-group v-model="scope" style="margin-bottom: 12px">
      <el-radio-button value="all">全部（{{ runs.length }}）</el-radio-button>
      <el-radio-button value="mine">我跑的（{{ mineCount }}）</el-radio-button>
      <el-radio-button value="peers">同事跑的（{{ peerCount }}）</el-radio-button>
    </el-radio-group>

    <el-table :data="visible">
      <el-table-column prop="run_id" label="run_id" width="220" />
      <el-table-column label="归属" width="110">
        <template #default="{ row }">
          <el-tag :type="row.is_mine ? 'success' : 'info'" size="small">
            {{ row.is_mine ? "我跑的" : "同事跑的" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="question" label="问题" min-width="180" />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'success' ? 'success' : row.status === 'degraded' ? 'warning' : 'danger'">
            {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="llm_calls" label="LLM调用" width="90" />
      <el-table-column prop="duration_seconds" label="耗时(s)" width="90" />
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button link type="primary" @click="openReport(row)">查看报告</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="报告" width="70%">
      <ReportViewer :content="reportContent" />
      <p class="scope-note">
        图片地址带一枚只读媒体 token，约 {{ expiresInMinutes }} 分钟有效；
        过期之后重新点"查看报告"就会换一枚（有效期由后端给，不在前端抄一份数）。
      </p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import ReportViewer from "../components/ReportViewer.vue";

const runs = ref<any[]>([]);
const summary = ref<any>({});
const dialogVisible = ref(false);
const reportContent = ref("");
// 媒体 token 的有效期由后端随报告一起给（`media_expires_in`），前端不抄一份常数：
// 抄了之后服务端改时长，界面上那句话就成了没人背书的旧话。
const mediaExpiresIn = ref(0);
const expiresInMinutes = computed(() => Math.max(1, Math.round(mediaExpiresIn.value / 60)));
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
  mediaExpiresIn.value = data.media_expires_in || 0;
  dialogVisible.value = true;
}

onMounted(refresh);
</script>

<style>
.stat {
  text-align: center;
  font-weight: 600;
}
.scope-note {
  color: #909399;
  font-size: 12px;
  margin: 4px 0 12px;
}
</style>
