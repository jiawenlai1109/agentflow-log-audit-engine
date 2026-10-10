<template>
  <div>
    <h1 class="page-title">分析工作台</h1>
    <el-form inline class="submit-form">
      <el-form-item label="数据集">
        <el-select v-model="dataset_id" placeholder="选择数据集" class="f-dataset">
          <el-option v-for="d in datasets" :key="d.id" :label="`${d.filename}（${d.row_count}行）`" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="场景包">
        <el-select v-model="pack" class="f-pack">
          <el-option label="不挂包（自由分析）" :value="NO_PACK" />
          <el-option v-for="p in packs" :key="p.name" :label="packLabel(p)" :value="p.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="会话">
        <el-select
          v-model="session_id"
          :placeholder="pack !== NO_PACK ? '带包时不挂会话' : '选择会话（默认新建）'"
          clearable
          class="f-session"
          :disabled="pack !== NO_PACK"
        >
          <el-option v-for="s in sessions" :key="s.session_id" :label="s.title" :value="s.session_id" />
        </el-select>
      </el-form-item>
      <el-form-item label="模式">
        <el-select v-model="mode" class="f-mode">
          <el-option label="mock" value="mock" />
          <el-option label="real" value="real" />
        </el-select>
      </el-form-item>
      <el-form-item label="上游">
        <!-- 只报预检结论，不给下拉切换：换了型号就是换了计费与产出质量，那要单独一片做归因 -->
        <el-tag :type="llmTagType" size="small" :title="llmDetail">{{ llmSummary }}</el-tag>
      </el-form-item>
    </el-form>
    <div class="pack-facts" role="note">
      <span>{{ packFactsText }}</span>
      <span v-if="packBroken">｜{{ packBroken }}</span>
      <!-- 包与会话在后端是互斥的（`tests/test_packs_api.py:384` 记着理由），所以这句话
           不是提示而是解释"会话那一格为什么灰了"——不说，用户以为自己选错了什么。 -->
      <span v-if="packSessionNote">｜{{ packSessionNote }}</span>
    </div>
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
    <div class="platform-strip" role="status">
      <span>{{ platformSummary }}</span>
      <span v-if="platform?.dispatch">｜形态：{{ platform.dispatch.shape }}（本进程认领循环 {{ num(platform.dispatch.claim_loops) }}）</span>
      <span>｜读数于 {{ readoutAt }}</span>
      <el-button link type="primary" :loading="refreshing" @click="reloadPlatform">回读一次</el-button>
    </div>

    <el-input
      v-model="question"
      type="textarea"
      :rows="3"
      aria-label="业务问题"
      placeholder="输入你的业务问题，例如：总销售额是多少？最近7天销售额走势如何？"
    />
    <div class="submit-row">
      <el-button type="primary" :loading="running" @click="submit">提交分析</el-button>
      <!-- 中止的是"这条进度流"，不是作业：后端没有取消接口（那是另一片），
           按钮必须说清楚它停的是哪一半，否则用户以为作业也没了。 -->
      <el-button v-if="running" @click="stopWatching">停止接进度</el-button>
    </div>

    <el-divider />

    <div v-if="running">
      <h2 class="section-title">Agent 执行进度</h2>
      <AgentProgress :events="events" />
    </div>

    <div v-for="(item, index) in messages" :key="index" class="message-card">
      <el-card>
        <div class="message-q">
          问：{{ item.question }}
          <el-tag v-if="item.pack" size="small" type="info" class="meta">带包 {{ item.pack }}</el-tag>
          <span v-else-if="item.packKnown" class="meta">不挂包</span>
          <!-- 历史条目里两个都没有：`/api/sessions/{id}/messages` 不返回包名，
               所以那一轮到底带没带包这一页不知道。不知道就两个都不印，别补一个"不挂包"。 -->
        </div>
        <el-alert
          v-if="item.error"
          type="error"
          :title="'分析失败'"
          :description="item.error"
          show-icon
          :closable="false"
          class="inline-alert"
        />
        <!-- 重跑按钮不许放进 el-alert 的默认插槽：那个插槽渲染进 `.el-alert__description`，
             会把失败原因整段顶掉（实跑页面上量到的就是这个形状——红色卡片上一句"为什么"都没有）。 -->
        <div v-if="item.error" class="inline-alert">
          <el-button size="small" @click="rerun(item)">按这一问法重跑</el-button>
        </div>
        <el-alert
          v-if="item.mode === 'mock' && !item.error"
          type="info"
          title="mock 模式为离线演示结果，复杂/精确问题请使用 real 模式"
          :closable="false"
          class="inline-alert"
        />
        <el-tag v-if="item.run_id" size="small" class="inline-alert">{{ item.run_id }}</el-tag>
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
import { num } from "../ui/readout";
import { NO_PACK, brokenPackNote, packFacts, packLabel, packPayload, sessionForPack } from "../ui/packChoice";

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

// 场景包：名单来自 `GET /api/packs`，界面不自己拼一份（`_available_pack_names()` 在引擎里算，
// Web 层不另立权威——`app/routers/jobs.py:80` 那句理由同样适用于前端）。
// `packsLoaded` 这一格是必须的：接口挂了的时候下拉里只剩"不挂包"，那读起来就是"这台系统没有场景包"，
// 而实际上是"这次没读到"。两者必须长得不一样。
const packs = ref<any[]>([]);
const packBroken = ref("");
const packLoaded = ref(false);
const pack = ref<string>(NO_PACK);
const selectedPack = computed(() => packs.value.find((p: any) => p.name === pack.value));
const packFactsText = computed(() =>
  packLoaded.value ? packFacts(selectedPack.value) : "场景包名单未取得（接口不可用或未登录）",
);
const packSessionNote = computed(() =>
  pack.value !== NO_PACK ? "带包时这一轮不挂会话：结果照样进本页与历史页，但不进会话树" : "",
);

async function loadPacks() {
  try {
    const { data } = await api.listPacks();
    packs.value = data.packs || [];
    packBroken.value = brokenPackNote(data.broken);
    packLoaded.value = true;
  } catch {
    packs.value = [];
    packBroken.value = "";
    packLoaded.value = false; // 读不到就是读不到，别让它显示成"一个包都没有"
  }
}

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
// 读数是什么时候取的，必须和读数一起显示：一个不动的数字与一个"当前是 0"是两件事。
// 原先这里只有数，页面挂十分钟不动，值班看到的仍是"排队 0"。
const readoutAt = ref("尚未回读");
const refreshing = ref(false);

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
    readoutAt.value = clock(new Date());
  } catch {
    platform.value = null; // 拿不到就说"没读到"，不拿旧值冒充现状
    readoutAt.value = "回读失败";
  }
}

async function reloadPlatform() {
  refreshing.value = true;
  try {
    await loadPlatform();
  } finally {
    refreshing.value = false;
  }
}

/** 只在显示处格式化，存的是 Date：把 toLocaleTimeString() 存在响应式变量里，
 *  就又回到"印一个会过期的现在"——那正是进度流时间戳那一处刚修掉的毛病。 */
function clock(at: Date): string {
  return at.toLocaleTimeString("zh-CN", { hour12: false });
}

const platformSummary = computed(() => {
  const p = platform.value;
  if (!p) return "平台读数未取得（服务不可达或未登录）";
  const q = p.queue || {};
  const g = p.gate || {};
  // 四格同口径：谁没测到就都说"未取得"。原先这行是 `queued ?? "?"` 配 `stale_pending ?? 0`，
  // 于是"库里读不到"在最后一格里显示成"没有没人认领的旧行"——那是个结论，不是没结论。
  return `排队 ${num(q.queued)} ｜ 在跑 ${num(q.running)} ｜ 没人认领的旧行 ${num(q.stale_pending)} ｜ 上游 ${num(g.inflight)}/${num(g.limit)} 路（${GATE_SOURCE_TEXT[g.limit_source] || g.limit_source || "来源未知"}）`;
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
  // 在选择器被用户改动之前就把这一次的值定下来：结果卡上写的包名必须是**那次提交**带的包，
  // 不是我等我看结果期间顺手换了另一个包。
  const usedPack = packPayload(pack.value);
  streamController?.abort();
  streamController = new AbortController();
  running.value = true;
  events.value = [];
  // 一次用户意图 = 一个幂等键。生成在这里而不是 api 层，是因为"同一次提交"的边界只有界面知道：
  // 网络抖一下由 api 重试，还是这个按钮被连点两次，对后端来说形状一样，对你是不是同一个作业不一样。
  const submissionKey = newIdempotencyKey();
  try {
    // 带包时不再自动建会话：原先这条链是"没会话就顺手建一个，然后把 session_id 发出去"，
    // 配上包就成了"pack + session_id 一起发"——那是后端明令 422 的组合。
    // 这一轮的结果照样进本页与历史页，只是不进会话树。
    if (!session_id.value && !usedPack) {
      const { data } = await api.createSession({ title: asked.slice(0, 20), dataset_id: dataset_id.value });
      session_id.value = data.session_id;
      loadSessions();
    }
    const usedSession = sessionForPack(usedPack, session_id.value);
    const { data } = await api.analyze(
      {
        question: asked,
        dataset_id: dataset_id.value,
        mode: usedMode,
        session_id: usedSession,
        // 不挂包时这个键整个不发（undefined 会被 axios 丢掉）——发空串会被后端判成 422
        pack: usedPack,
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
        // 到页时刻在这里捕获一次，随事件带着走。渲染期取 new Date() 的话，
        // 每来一帧整个列表的时间都会变成"现在"（断线续读补发的旧帧尤其明显）。
        events.value.push({ ...event, seen_at: Date.now() });
        // 进度流第一帧就带着队列/闸门/形态，比再发一次 GET 更新：这一条流活着的时候读数就是活的
        if (event?.type === "queue") {
          platform.value = { queue: event, gate: event.gate, dispatch: event.dispatch };
          // 首帧没有游标（它是这条流自己造的，不是库里的事件行）：那就说"首帧"，别显示"第 ? 帧"。
          readoutAt.value = event.seq ? `流内第 ${event.seq} 帧` : `进度流首帧 ${clock(new Date())}`;
        }
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
    const status = await showResult({ question: asked, mode: usedMode, pack: usedPack ?? null, packKnown: true }, jobId);
    // 只有真跑完才清问题输入。原先是无条件清：失败那一支把用户刚打的问法一起抹掉，
    // 而错误卡上又没有任何重跑入口——等于"失败了，我把你的现场也收走了"。
    if (status === "success") question.value = "";
    await loadHistory();
    // 跑完之后必须再回读一次：这条带子上的"在跑 1"是从进度流里来的，作业已经结束了，
    // 留着旧数就是"界面显示着一个已经不存在的世界面"。
    await loadPlatform();
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
      // 后端在 422 的 detail 里说的是"缺哪几列 / 可用的包有哪些 / 需要去哪看约定"
      // （`jobs.py:98`、`jobs.py:160`）。原先只取 `err.message`，那是 axios 的
      // "Request failed with status code 422"——等于把最有用的那句话丢了，
      // 而这条页面上的错误卡本来就该是"当场知道该改什么"（validate_pack 写在派发前就是为了这句）。
      const reason = err?.response?.data?.detail || err?.message || "提交失败";
      // 429 是一种**结果**，不是崩溃：走到这里说明客户端已按服务端的 Retry-After 重试过还是被拒。
      // 混进"提交失败"里，值班看到的是一处故障，而实际是配额或入口限流在起作用。
      const label = status === 429 ? `被拦下（配额或入口限流，已按建议重试过）：${reason}` : reason;
      messages.value.push({ question: asked, mode: usedMode, pack: usedPack ?? null, packKnown: true, error: label });
      if (status === 429) ElMessage.warning(label);
      else ElMessage.error(label);
      loadPlatform(); // 被拦下时顺便回读一次平台读数：是排队满了还是闸门满了，界面要能说得出
    }
  } finally {
    running.value = false;
    streamController = null;
  }
}

/** 手动停止接进度。作业不受影响——后端没有取消接口（那是另一片要单独决定的事），
 *  所以这句话必须说准：停的是"这一页还在盯着看"，不是"这件事被中止了"。 */
function stopWatching() {
  streamController?.abort();
  ElMessage.info("已停止接进度。作业仍在队列里跑，可到「历史与报告」页看结果");
}

/** 重跑 = 一次新的用户意图，所以走一条新的幂等键（P5-1 的口径：同一个键才是同一件事）。 */
function rerun(item: any) {
  question.value = item.question;
  if (item.mode) mode.value = item.mode;
  // 只有这一条记录**知道**自己带的什么包才还原它：历史条目里 `"pack" in item` 不成立，
  // 那时把选择器改成"不挂包"就是拿一个unknown当结论。
  if ("pack" in item) pack.value = item.pack || NO_PACK;
  submit();
}

onMounted(async () => {
  const { data } = await api.listDatasets();
  datasets.value = data;
  if (data.length > 0) dataset_id.value = data[0].id;
  await loadPacks();
  await loadLlmStatus();
  await loadPlatform();
  await loadSessions();
  await loadHistory();
});

onBeforeUnmount(() => streamController?.abort());
</script>

<style scoped>
/* 提交区这几个下拉原先是 `style="width: 260px"` 这类固定宽度：窄屏上它们不换行、
 * 也压不下长文件名，于是横向溢出。改成 min/max 之间自适应。
 * 深度选择器只用于 el-form-item 的直接控件容器，不越过一层。 */
.submit-form :deep(.el-form-item) {
  margin-right: 16px;
  margin-bottom: 8px;
}
.f-dataset,
.f-pack,
.f-session {
  width: min(260px, 100%);
  min-width: 10rem;
}
.f-mode {
  width: 7rem;
}
.submit-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.inline-alert {
  margin: 8px 0;
}
.platform-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: -4px 0 12px;
  font-size: var(--el-font-size-extra-small);
  /* 原先这条带的字色没写，继承正文色；而同类"说明文字"在别的 view 里是 #909399（3.08:1）。
   * 现在统一走令牌：同一件事（次级说明）全站一个色，且这个色是量过达标的。 */
  color: var(--af-text-secondary);
}
.message-card {
  margin-bottom: 12px;
}
.message-q {
  font-weight: 600;
  margin-bottom: 8px;
}
.pack-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: -4px 0 12px;
  font-size: var(--el-font-size-extra-small);
  color: var(--af-text-secondary);
}
.meta {
  font-size: var(--el-font-size-extra-small);
  color: var(--af-text-secondary);
  font-weight: 400;
}
</style>
