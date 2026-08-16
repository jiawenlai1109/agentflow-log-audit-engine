<template>
  <div>
    <el-form inline>
      <el-form-item label="数据集">
        <el-select v-model="dataset_id" placeholder="选择数据集" style="width: 260px">
          <el-option v-for="d in datasets" :key="d.id" :label="`${d.filename}（${d.row_count}行）`" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="会话">
        <el-select v-model="session_id" placeholder="选择会话（默认新建）" clearable style="width: 220px">
          <el-option v-for="s in sessions" :key="s.session_id" :label="s.title" :value="s.session_id" />
        </el-select>
      </el-form-item>
      <el-form-item label="模式">
        <el-select v-model="mode" style="width: 110px">
          <el-option label="mock" value="mock" />
          <el-option label="real" value="real" />
        </el-select>
      </el-form-item>
    </el-form>

    <el-input
      v-model="question"
      type="textarea"
      :rows="3"
      placeholder="输入你的业务问题，例如：总销售额是多少？最近7天销售额走势如何？"
    />
    <div style="margin-top: 12px">
      <el-button type="primary" :loading="running" @click="submit">提交分析</el-button>
    </div>

    <el-divider />

    <div v-if="running">
      <h4>Agent 执行进度</h4>
      <AgentProgress :events="events" />
    </div>

    <div v-for="(item, index) in messages" :key="index" class="message-card">
      <el-card>
        <div class="message-q">问：{{ item.question }}</div>
        <el-tag v-if="item.run_id" size="small" style="margin: 8px 0">{{ item.run_id }}</el-tag>
        <ReportViewer v-if="item.report" :content="item.report" />
        <div v-else>{{ item.answer_summary }}</div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import AgentProgress from "../components/AgentProgress.vue";
import ReportViewer from "../components/ReportViewer.vue";

const route = useRoute();
const datasets = ref<any[]>([]);
const sessions = ref<any[]>([]);
const dataset_id = ref<number | undefined>(undefined);
const session_id = ref<string | undefined>((route.query.session as string) || undefined);
const mode = ref("mock");
const question = ref("");
const running = ref(false);
const events = ref<any[]>([]);
const messages = ref<any[]>([]);

async function loadSessions() {
  const { data } = await api.listSessions();
  sessions.value = data;
}

async function loadHistory() {
  if (!session_id.value) return;
  const { data } = await api.sessionMessages(session_id.value);
  messages.value = data.map((m: any) => ({ question: m.question, run_id: m.run_id, answer_summary: m.answer_summary, report: "" }));
  const jobIds: string[] = [];
  for (const m of data) {
    if (m.run_id) {
      const { data: report } = await api.getReport(m.run_id);
      messages.value.find((x: any) => x.run_id === m.run_id)!.report = report.content;
    }
  }
}

async function submit() {
  if (!dataset_id.value) {
    ElMessage.warning("请先选择数据集");
    return;
  }
  if (!question.value.trim()) {
    ElMessage.warning("请输入问题");
    return;
  }
  if (!session_id.value) {
    const { data } = await api.createSession({ title: question.value.slice(0, 20), dataset_id: dataset_id.value });
    session_id.value = data.session_id;
    loadSessions();
  }
  const { data } = await api.analyze({
    question: question.value,
    dataset_id: dataset_id.value,
    mode: mode.value,
    session_id: session_id.value,
  });
  const jobId = data.job_id;
  running.value = true;
  events.value = [];
  const source = new EventSource(`/api/jobs/${jobId}/events?token=${localStorage.getItem("token") || ""}`);
  source.onmessage = async (ev) => {
    const event = JSON.parse(ev.data);
    events.value.push(event);
    if (event.type === "done" || event.type === "error") {
      source.close();
      running.value = false;
      const job = await api.getJob(jobId);
      if (job.data.run_id) {
        const { data: report } = await api.getReport(job.data.run_id);
        messages.value.push({ question: question.value, run_id: job.data.run_id, answer_summary: "", report: report.content });
      }
      question.value = "";
      loadHistory();
      ElMessage.success(event.type === "done" ? `分析完成（${event.status}）` : "分析出错");
    }
  };
}

onMounted(async () => {
  const { data } = await api.listDatasets();
  datasets.value = data;
  if (data.length > 0) dataset_id.value = data[0].id;
  await loadSessions();
  await loadHistory();
});
</script>

<style scoped>
.message-card {
  margin-bottom: 12px;
}
.message-q {
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
