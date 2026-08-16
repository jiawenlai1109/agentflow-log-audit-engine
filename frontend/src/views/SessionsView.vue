<template>
  <div>
    <el-button type="primary" @click="create">新建会话</el-button>
    <el-table :data="sessions" style="margin-top: 16px">
      <el-table-column prop="session_id" label="session_id" width="220" />
      <el-table-column prop="title" label="标题" />
      <el-table-column prop="turn_count" label="轮次" width="80" />
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
const router = useRouter();

async function refresh() {
  const { data } = await api.listSessions();
  sessions.value = data;
}

async function create() {
  const { data } = await api.createSession({ title: `会话 ${Date.now()}` });
  ElMessage.success("已创建，前往工作台提问");
  router.push({ path: "/", query: { session: data.session_id } });
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
  await ElMessageBox.confirm("确定删除该会话？", "提示");
  await api.deleteSession(row.session_id);
  refresh();
}

onMounted(refresh);
</script>
