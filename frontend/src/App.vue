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
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ auth.username || "用户" }}</span>
        <el-button link type="primary" @click="auth.logout(); router.push('/login')">退出</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { useRoute, useRouter } from "vue-router";
import { useAuthStore } from "./stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
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
