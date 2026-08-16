<template>
  <el-timeline style="max-width: 560px">
    <el-timeline-item
      v-for="(item, index) in timeline"
      :key="index"
      :type="item.type === 'error' ? 'danger' : item.type === 'done' ? 'success' : 'primary'"
      :timestamp="item.time"
    >
      {{ item.text }}
      <el-tag v-if="item.status" size="small" :type="item.status === 'SUCCEEDED' ? 'success' : 'danger'">
        {{ item.status }}
      </el-tag>
    </el-timeline-item>
  </el-timeline>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ events: any[] }>();

const phaseNames: Record<string, string> = {
  explore: "数据探查",
  plan: "任务规划",
  execute: "代码执行",
  report: "报告生成",
  review: "报告评审",
};

const timeline = computed(() =>
  props.events.map((event) => {
    if (event.type === "phase") {
      return { text: `阶段：${phaseNames[event.phase] || event.phase}`, time: new Date().toLocaleTimeString() };
    }
    if (event.type === "task") {
      return {
        text: `任务 ${event.task_id}`,
        status: event.status,
        time: new Date().toLocaleTimeString(),
      };
    }
    if (event.type === "done") {
      return { text: `完成（${event.status}）`, time: new Date().toLocaleTimeString() };
    }
    if (event.type === "error") {
      return { text: `错误：${event.error}`, time: new Date().toLocaleTimeString() };
    }
    return { text: `${event.sender || ""} → ${event.receiver || ""}（${event.kind || ""}）`, time: new Date().toLocaleTimeString() };
  })
);
</script>
