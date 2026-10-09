<template>
  <div class="login-wrap">
    <el-card class="login-card" shadow="never">
      <h1 class="title">多智能体数据分析引擎</h1>
      <!-- 原先是 `@submit.prevent` + 一个普通按钮：按回车什么都不发生（表单里没有 submit 按钮，
           隐式提交就不成立），而"用户名/密码"这个形状里回车是最自然的动作。 -->
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="username" aria-label="用户名" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="password"
            type="password"
            aria-label="密码"
            placeholder="密码"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button class="login-submit" type="primary" native-type="submit" :loading="busy">登 录</el-button>
      </el-form>
      <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="inline-alert" />
      <p v-if="devPrefilled" class="hint">已为本地开发预填 admin/admin；构建产物里没有这一行。</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuthStore } from "../stores/auth";

const username = ref("");
const password = ref("");
const busy = ref(false);
const error = ref("");
const router = useRouter();
const auth = useAuthStore();

// 预填只在 dev 服务器成立：这个平台对外是真环境，把一个管理员账号口令写死在登录页上，
// 表现就是"任何人都能用管理员身份试第一步"。开发时的方便保留，构建产物里不带。
const devPrefilled = import.meta.env.DEV;
if (devPrefilled) {
  username.value = "admin";
  password.value = "admin";
}

async function doLogin() {
  if (busy.value) return;
  error.value = "";
  busy.value = true;
  try {
    await auth.login(username.value, password.value);
    router.push("/");
  } catch (caught: any) {
    // 后端把拒绝原因写在 detail 里（口令不对 / 被限流要等多久），拦截器已经把它转成 message。
    // 一律压成"登录失败"等于把可行动原因扔掉，还顺手把"你在重试限流里"显示成"密码错了"。
    const reason = caught?.message || "登录失败（服务不可达）";
    error.value = reason;
    ElMessage.error(reason);
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: var(--el-fill-color-light);
}
.login-card {
  width: min(360px, 100%);
}
.title {
  margin: 0 0 16px;
  text-align: center;
  font-size: 20px;
  font-weight: 700;
}
.login-submit {
  width: 100%;
}
.inline-alert {
  margin-top: 12px;
}
.hint {
  margin: 8px 0 0;
  font-size: var(--el-font-size-extra-small);
  color: var(--af-text-secondary);
}
</style>
