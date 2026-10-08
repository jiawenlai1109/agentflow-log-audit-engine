<template>
  <router-view v-if="route.path === '/login'" />
  <el-container v-else>
    <el-aside width="200px">
      <div class="logo">数据分析引擎</div>
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/">分析工作台</el-menu-item>
        <el-menu-item index="/datasets">数据管理</el-menu-item>
        <el-menu-item index="/sessions">会话管理</el-menu-item>
        <el-menu-item index="/reports">历史与报告</el-menu-item>
        <el-menu-item index="/members">成员与企业</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ auth.username || "用户" }}</span>
        <el-tag v-if="auth.isAdmin" size="small" type="danger">管理员</el-tag>
        <el-tag v-if="!auth.unassigned" size="small">{{ auth.orgLabel }}</el-tag>
        <el-tooltip
          v-else
          content="没有企业归属：只看得见自己的数据集与报告（共享读默认拒绝）。要协作请让管理员在建号时指定企业。"
        >
          <el-tag size="small" type="warning">未归属企业</el-tag>
        </el-tooltip>
        <el-button link type="primary" @click="auth.logout(); router.push('/login')">退出</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { UNAUTHORIZED_EVENT } from "./api/client";
import { useAuthStore } from "./stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

// 服务端判 401（token 过期/被吊销）时统一收口：本地清干净再跳登录页
function onUnauthorized() {
  if (!auth.token) return;
  auth.logout();
  ElMessage.warning("登录已过期，请重新登录");
  if (route.path !== "/login") router.push("/login");
}

onMounted(() => {
  window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  // 每次开页都向服务端回读一次身份与企业归属：本地的 role/orgs 只是显示用的副本，
  // 企业成员关系与管理角色是会被管理员改的，界面不该拿着上次登录时的快照继续显示。
  // 失败不去动 token：401 由上面的拦截器统一收，其它错误（断网）保持可读的旧值。
  if (auth.isAuthed) auth.fetchMe().catch(() => undefined);
});
onBeforeUnmount(() => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized));
</script>

<style>
.logo {
  padding: 16px;
  font-weight: 700;
  text-align: center;
}
.header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  border-bottom: 1px solid #eee;
}
</style>
