<template>
  <div class="login-wrap">
    <el-card style="width: 360px">
      <h2 style="text-align: center">多智能体数据分析引擎</h2>
      <el-form @submit.prevent>
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" show-password />
        </el-form-item>
        <el-button type="primary" style="width: 100%" @click="doLogin">登 录</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuthStore } from "../stores/auth";

const username = ref("admin");
const password = ref("admin");
const router = useRouter();
const auth = useAuthStore();

async function doLogin() {
  try {
    await auth.login(username.value, password.value);
    router.push("/");
  } catch {
    ElMessage.error("登录失败");
  }
}
</script>

<style>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
</style>
