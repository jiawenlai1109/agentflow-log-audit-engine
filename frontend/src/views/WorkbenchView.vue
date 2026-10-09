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
      <el-form-item label="上游">
        <!-- 只报预检结论，不给下拉切换：换了型号就是换了计费与产出质量，那要单独一片做归因 -->
        <el-tag :type="llmTagType" size="small" :title="llmDetail">{{ llmSummary }}</el-tag>
      </el-form-item>
    </el-form>
    <el-alert
      v-if="llmWarns"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      :title="`real 模式将使用 ${llmInfo?.configured?.model || '未知型号'}：${llmDetail}`"
    />

    <el-alert
      v-if="platformWarn"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      :title="platformWarn"
    />
    <div class="platform-strip">
      <span>{{ platformSummary }}</span>
      <span v-if="platform?.dispatch">｜形态：{{ platform.dispatch.shape }}（本进程认领循环 {{ platform.dispatch.claim_loops }}）</span>
    </div>

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
        <el-alert
          v-if="item.error"
          type="error"
          :title="'分析失败'"
          :description="item.error"
          show-icon
          :closable="false"
          style="margin: 8px 0"
        />
        <el-alert
          v-if="item.mode === 'mock' && !item.error"
          type="info"
          title="mock 模式为离线演示结果，复杂/精确问题请使用 real 模式"
          :closable="false"
          style="margin: 8px 0"
        />
        <el-tag v-if="item.run_id" size="small" style="margin: 8px 0">{{ item.run_id }}</el-tag>
        <ReportViewer v-if="item.report" :content="item.report" />
        <div v-else>{{ item.answer_summary }}</div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { api, newIdempotencyKey } from "../api";
import { streamJobEvents } from "../api/sse";
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

// 上游型号的预检结论（只读）。判据来自 scripts/preflight_llm.py 打的那次真实调用，
// 页面自己不发起调用——一个 GET 请求就烧钱是本项目不接受的形状（缺陷 #14 同一类）。
const VERDICT_TEXT: Record<string, string> = {
  usable: "可用",
  usable_if_thinking_disabled: "需关思考",
  do_not_disable_thinking: "别关思考",
  unusable: "不可用",
  unprobed: "未测",
};
const llmInfo = ref<any | null>(null);
const llmVerdict = computed(() => VERDICT_TEXT[llmInfo.value?.configured?.verdict] || "未测");
const llmTagType = computed(() => {
  const verdict = llmInfo.value?.configured?.verdict;
  if (verdict === "usable") return "success";
  if (verdict === "unusable") return "danger";
  if (verdict === "unprobed") return "info";
  return "warning";
});
const llmSummary = computed(
  () => `${llmInfo.value?.configured?.model || "未取得型号"} · ${llmVerdict.value}`,
);
const llmDetail = computed(() => {
  const info = llmInfo.value;
  if (!info) return "型号可用性接口不可用（后端未启动或未登录）";
  const note = info.configured?.note || "";
  const stamp = info.fresh ? `预检于 ${info.checked_at}` : "预检结论已过期或不属于这台端点";
  return `${info.base_host}｜${stamp}｜${note || info.hint || "无备注"}`;
});
// real 模式下"没测过"也要说出来：它不阻断提交（那是改行为，得单独决定），但别让它无声
const llmWarns = computed(
  () => mode.value === "real" && ["unusable", "unprobed"].includes(llmInfo.value?.configured?.verdict),
);

async function loadLlmStatus() {
  try {
    const { data } = await api.llmModels();
    llmInfo.value = data;
  } catch {
    llmInfo.value = null;
  }
}

// 平台读数：队列深度（库里的事实）、上游闸门（本进程的量）、执行形态（本进程吃不吃作业）。
// 三个数各来自服务端唯一的出处，界面不自己算——尤其"哪些状态算终态""这个进程几个循环"这类，
// 抄一份就地分叉（P5-2/P5-3 各为这句话付过一次学费）。
const platform = ref<any | null>(null);

const GATE_SOURCE_TEXT: Record<string, string> = {
  env: "运维定档",
  measured: "实测定档",
  unmeasured_fallback: "占位值·没人量过",
  explicit: "本次显式配置",
};

async function loadPlatform() {
  try {
    const { data } = await api.queue();
    platform.value = data;
  } catch {
    platform.value = null; // 拿不到就说"没读到"，不拿旧值冒充现状
  }
}

const platformSummary = computed(() => {
  const p = platform.value;
  if (!p) return "平台读数未取得（服务不可达或未登录）";
  const q = p.queue || {};
  const g = p.gate || {};
  return `排队 ${q.queued ?? "?"} ｜ 在跑 ${q.running ?? "?"} ｜ 没人认领的旧行 ${q.stale_pending ?? 0} ｜ 上游 ${g.inflight ?? "?"}/${g.limit ?? "?"} 路（${GATE_SOURCE_TEXT[g.limit_source] || g.limit_source || "来源未知"}）`;
});

// 分进程形态下最该说的一句话：这个进程不认领作业，而 worker 没起时作业会一直排队——
// 接口全绿、每个请求都 200，唯一能看出来的是这条读数。不显示出来，值班的人只能猜。
const platformWarn = computed(() => {
  const dispatch = platform.value?.dispatch;
  if (!dispatch) return "";
  if (!dispatch.dispatch_in_web && (platform.value?.queue?.running ?? 0) === 0) {
    return `本进程不认领作业（${dispatch.shape}）：${dispatch.note}`;
  }
  if ((platform.value?.queue?.stale_pending ?? 0) > 0) {
    return `有 ${platform.value.queue.stale_pending} 行没人能认领也没人负责（不是"还在排队"）`;
  }
  if (platform.value?.gate?.limit_source === "unmeasured_fallback") {
    return `上游闸门用的是占位值 ${platform.value.gate.limit} 路，没量过这台站的并发形状（重测：scripts/preflight_llm.py --concurrency）`;
  }
  return "";
});

// 切换视图或重复提交时取消上一条进度流，避免旧流回调写进新结果
let streamController: AbortController | null = null;

async function loadSessions() {
  const { data } = await api.listSessions();
  sessions.value = data;
}

async function loadHistory() {
  if (!session_id.value) return;
  const { data } = await api.sessionMessages(session_id.value);
  messages.value = data.map((m: any) => ({ question: m.question, run_id: m.run_id, answer_summary: m.answer_summary, report: "" }));
  for (const m of data) {
    if (m.run_id) {
      const { data: report } = await api.getReport(m.run_id);
      messages.value.find((x: any) => x.run_id === m.run_id)!.report = report.content;
    }
  }
}

async function showResult(entry: any, jobId: string) {
  const { data: job } = await api.getJob(jobId);
  if (job.run_id) {
    const { data: report } = await api.getReport(job.run_id);
    messages.value.push({ ...entry, run_id: job.run_id, answer_summary: "", report: report.content });
    return job.status;
  }
  messages.value.push({ ...entry, error: job.error || "分析失败（无详细信息）" });
  return job.status;
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
  const asked = question.value;
  const usedMode = mode.value;
  streamController?.abort();
  streamController = new AbortController();
  running.value = true;
  events.value = [];
  // 一次用户意图 = 一个幂等键。生成在这里而不是 api 层，是因为"同一次提交"的边界只有界面知道：
  // 网络抖一下由 api 重试，还是这个按钮被连点两次，对后端来说形状一样，对你是不是同一个作业不一样。
  const submissionKey = newIdempotencyKey();
  try {
    if (!session_id.value) {
      const { data } = await api.createSession({ title: asked.slice(0, 20), dataset_id: dataset_id.value });
      session_id.value = data.session_id;
      loadSessions();
    }
    const { data } = await api.analyze(
      {
        question: asked,
        dataset_id: dataset_id.value,
        mode: usedMode,
        session_id: session_id.value,
      },
      { idempotencyKey: submissionKey, signal: streamController.signal }
    );
    const jobId = data.job_id;
    if (data.idempotency_replayed) {
      // 撞回了自己上一次的作业：这不是错误，但必须说出来——不然用户以为这是一次新分析
      ElMessage.info("这次提交和上一次是同一个请求，沿用的已经是排队中的那条作业");
    }
    await streamJobEvents(
      jobId,
      (event) => {
        events.value.push(event);
        // 进度流第一帧就带着队列/闸门/形态，比再发一次 GET 更新：这一条流活着的时候读数就是活的
        if (event?.type === "queue") platform.value = { queue: event, gate: event.gate, dispatch: event.dispatch };
      },
      streamController?.signal,
      {
        onReconnect: ({ attempt, waitMs }) => {
          if (attempt === 1) {
            ElMessage.warning(`进度流中断，正在第 ${attempt} 次接回（约 ${Math.round(waitMs / 100) / 10}s）…`);
          } else {
            console.warn(`进度流第 ${attempt} 次重连，等待 ${waitMs}ms`);
          }
        },
      },
    );
    const status = await showResult({ question: asked, mode: usedMode }, jobId);
    question.value = "";
    await loadHistory();
    if (status === "pending" || status === "running") {
      ElMessage.warning("进度流已中断，任务仍在后台执行，请到历史与报告页查看结果");
    } else if (status === "success") {
      ElMessage.success(`分析完成（${status}）`);
    } else {
      ElMessage.error(`分析结束（${status}）`);
    }
  } catch (err: any) {
    if (err?.name !== "CanceledError" && err?.name !== "AbortError") {
      const status = err?.response?.status;
      const reason = err?.message || "提交失败";
      // 429 是一种**结果**，不是崩溃：走到这里说明客户端已按服务端的 Retry-After 重试过还是被拒。
      // 混进"提交失败"里，值班看到的是一处故障，而实际是配额或入口限流在起作用。
      const label = status === 429 ? `被拦下（配额或入口限流，已按建议重试过）：${reason}` : reason;
      messages.value.push({ question: asked, mode: usedMode, error: label });
      if (status === 429) ElMessage.warning(label);
      else ElMessage.error(label);
      loadPlatform(); // 被拦下时顺便回读一次平台读数：是排队满了还是闸门满了，界面要能说得出
    }
  } finally {
    running.value = false;
    streamController = null;
  }
}

onMounted(async () => {
  const { data } = await api.listDatasets();
  datasets.value = data;
  if (data.length > 0) dataset_id.value = data[0].id;
  await loadLlmStatus();
  await loadPlatform();
  await loadSessions();
  await loadHistory();
});

onBeforeUnmount(() => streamController?.abort());
</script>

<style scoped>
.platform-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: -4px 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.message-card {
  margin-bottom: 12px;
}
.message-q {
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
