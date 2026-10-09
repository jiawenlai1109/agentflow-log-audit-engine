<template>
  <div>
    <!-- 只追加不整段重播：role=log 的默认语义就是"新内容播报、旧内容不重复念"，
         用 role=status 会把每一帧变成一次全屏播报，读屏里听不见上下文。 -->
    <el-timeline class="stream" role="log" aria-live="polite" aria-label="Agent 执行进度">
      <el-timeline-item
        v-for="item in timeline"
        :key="item.key"
        :type="item.tone"
        :timestamp="item.at"
      >
        {{ item.text }}
        <el-tag v-if="item.status" size="small" :type="item.tone" :title="item.statusTitle">
          {{ item.statusLabel }}
        </el-tag>
      </el-timeline-item>
    </el-timeline>
    <p v-if="!timeline.length" class="hint">还没有收到事件帧。</p>
    <p class="hint">
      序号是服务端事件协议里的游标（权威顺序）；时间是<b>本页收到这一帧</b>的时刻——
      进度流断线接回时，补发过来的旧帧会带上接回那一刻的时间，别拿它当发生时间。
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { label, tone, tooltip } from "../ui/status";

const props = defineProps<{ events: any[] }>();

const phaseNames: Record<string, string> = {
  explore: "数据探查",
  plan: "任务规划",
  execute: "代码执行",
  report: "报告生成",
  review: "报告评审",
};

/** 到页时刻只在**收到那一帧**时算一次（由 WorkbenchView 挂在事件上带进来）。
 *  写在渲染期就是 `new Date()`：列表每加一帧，上面所有帧的时间都会变成"现在"，
 *  而进度流现在会断线续读（P5-2），补回来的旧帧会整排显示当前时刻。 */
function clockOf(event: any): string {
  const seen = Number(event?.seen_at);
  if (!Number.isFinite(seen)) return "—";
  return new Date(seen).toLocaleTimeString("zh-CN", { hour12: false });
}

const timeline = computed(() =>
  props.events.map((event, index) => {
    const seq = Number.isFinite(Number(event?.seq)) ? Number(event.seq) : null;
    const base = {
      // 有游标就用游标当身份（服务端权威、单调）；降级路径（事件没落库）没有游标，退回本页序号。
      key: seq ?? `local-${index}`,
      at: `${seq === null ? "" : `#${seq} · `}${clockOf(event)}`,
      tone: "primary",
      text: "",
    };
    if (event.type === "phase") {
      return { ...base, text: `阶段：${phaseNames[event.phase] || event.phase}` };
    }
    if (event.type === "task") {
      return {
        ...base,
        tone: tone(event.status),
        text: `任务 ${event.task_id}`,
        status: event.status,
        statusLabel: label(event.status),
        statusTitle: tooltip(event.status),
      };
    }
    if (event.type === "done") {
      return { ...base, tone: tone(event.status), text: `完成：${label(event.status)}` };
    }
    if (event.type === "error") {
      return { ...base, tone: "danger", text: `错误：${event.error}` };
    }
    if (event.type === "queue") {
      return { ...base, tone: "info", text: "队列与上游读数已更新" };
    }
    if (event.type === "job_status") {
      return { ...base, tone: tone(event.status), text: `作业终态：${label(event.status)}` };
    }
    if (event.type === "events_not_persisted") {
      // 这条要说人话：它不是"进度"，是"这条流的存储降级了"，值班看到的必须是后者
      return { ...base, tone: "warning", text: "事件未落库，本条流退化为过程内缓冲" };
    }
    return {
      ...base,
      tone: "info",
      text: `${event.sender || "?"} → ${event.receiver || "?"}（${event.kind || "?"}）`,
    };
  })
);
</script>

<style scoped>
.stream {
  max-width: 56rem;
}
.hint {
  margin: 4px 0 0;
  color: var(--af-text-secondary);
  font-size: var(--el-font-size-extra-small);
  max-width: 56rem;
}
</style>
