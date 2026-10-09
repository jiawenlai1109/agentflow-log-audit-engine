<template>
  <div>
    <h1 class="page-title">会话管理</h1>
    <el-button type="primary" :loading="creating" @click="create">新建会话</el-button>
    <el-table :data="sessions" style="margin-top: 16px">
      <el-table-column prop="session_id" label="session_id" min-width="200" />
      <el-table-column prop="title" label="标题" min-width="160" />
      <el-table-column prop="turn_count" label="轮次" min-width="80" />
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <el-button link type="primary" @click="continueSession(row)">继续对话</el-button>
          <el-button link type="info" @click="viewMessages(row)">历史</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="messagesVisible" :title="`会话历史（${current?.title || ''}）`" width="60%">
      <el-timeline>
        <el-timeline-item v-for="msg in messages" :key="msg.turn">
          <b>第 {{ msg.turn }} 轮：{{ msg.question }}</b>
          <div>{{ msg.answer_summary }}</div>
          <el-tag v-if="msg.run_id" size="small">{{ msg.run_id }}</el-tag>
        </el-timeline-item>
      </el-timeline>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";

const sessions = ref<any[]>([]);
const messages = ref<any[]>([]);
const messagesVisible = ref(false);
const current = ref<any>(null);
const creating = ref(false);
const router = useRouter();

async function refresh() {
  const { data } = await api.listSessions();
  sessions.value = data;
}

async function create() {
  creating.value = true;
  try {
    // 标题原先是 `会话 ${Date.now()}`：把一串 epoch 当名字存进库里，之后在会话下拉、
    // 历史页、工作台每一处都要读"会话 1759... "。名字给人看，就按人读得懂的方式给。
    const { data } = await api.createSession({ title: `未命名会话（${new Date().toLocaleString("zh-CN", { hour12: false })}）` });
    ElMessage.success("已创建，前往工作台提问");
    router.push({ path: "/", query: { session: data.session_id } });
  } catch (caught: any) {
    ElMessage.error(caught?.message || "新建会话失败");
  } finally {
    creating.value = false;
  }
}

function continueSession(row: any) {
  router.push({ path: "/", query: { session: row.session_id } });
}

async function viewMessages(row: any) {
  current.value = row;
  const { data } = await api.sessionMessages(row.session_id);
  messages.value = data;
  messagesVisible.value = true;
}

async function remove(row: any) {
  // "取消"是这条对话框的一个正常出口：点取消时 ElMessageBox 是 reject 的，
  // 原先没有接住 ⇒ 一次无害的操作变成一条未处理的 Promise 拒绝。
  const confirmed = await ElMessageBox.confirm(`删除会话“${row.title}”？该会话的历史记录会一并移除。`, "确认删除", {
    type: "warning",
  }).catch(() => false);
  if (!confirmed) return;
  try {
    await api.deleteSession(row.session_id);
    ElMessage.success("已删除");
    await refresh();
  } catch (caught: any) {
    ElMessage.error(caught?.message || "删除失败");
  }
}

onMounted(refresh);
</script>
