<template>
  <div>
    <el-row :gutter="12" style="margin-bottom: 16px">
      <el-col :span="6"><el-card><div class="stat">success：{{ summary.status_count?.success }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">partial：{{ summary.status_count?.partial }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">degraded：{{ summary.status_count?.degraded }}</div></el-card></el-col>
      <el-col :span="6"><el-card><div class="stat">平均耗时：{{ summary.avg_duration }}s</div></el-card></el-col>
    </el-row>

    <el-table :data="runs">
      <el-table-column prop="run_id" label="run_id" width="220" />
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
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api";
import ReportViewer from "../components/ReportViewer.vue";

const runs = ref<any[]>([]);
const summary = ref<any>({});
const dialogVisible = ref(false);
const reportContent = ref("");

async function refresh() {
  const [runsRes, summaryRes] = await Promise.all([api.listRuns(), api.evaluationSummary()]);
  runs.value = runsRes.data;
  summary.value = summaryRes.data;
}

async function openReport(row: any) {
  const { data } = await api.getReport(row.run_id);
  reportContent.value = data.content;
  dialogVisible.value = true;
}

onMounted(refresh);
</script>

<style>
.stat {
  text-align: center;
  font-weight: 600;
}
</style>
