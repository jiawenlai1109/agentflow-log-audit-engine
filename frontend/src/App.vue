<template>
  <router-view v-if="route.path === '/login'" />
  <el-container v-else>
    <!-- 窄屏把侧栏收成图标列：原先写死 200px，375px 视口下正文只剩一半多一点宽。
         折叠必须有图标，否则折叠态的菜单项只剩一个看不见文字的按钮——
         那等于用无障碍换屏幕宽度。图标包是 package.json 里早已声明的那份
         （@element-plus/icons-vue，此前一次都没被 import），不新增依赖。 -->
    <el-aside :width="narrow ? '64px' : '200px'" class="side">
      <div class="logo">{{ narrow ? "AF" : "数据分析引擎" }}</div>
      <el-menu :default-active="route.path" :collapse="narrow" router aria-label="主导航">
        <!-- 折叠态下 EP 只渲染图标，`#title` 那份文字进不了无障碍树（实测：菜单项在
             辅助技术里变成 5 个没有名字的空按钮）。`aria-label` 给在 li 上，展开/折叠两种
             形态都有名字，且与看得见的文字一致（WCAG 2.5.3：语音操作说的是屏幕上那句）。 -->
        <el-menu-item index="/" aria-label="分析工作台">
          <el-icon><Histogram /></el-icon>
          <template #title>分析工作台</template>
        </el-menu-item>
        <el-menu-item index="/datasets" aria-label="数据管理">
          <el-icon><Files /></el-icon>
          <template #title>数据管理</template>
        </el-menu-item>
        <el-menu-item index="/sessions" aria-label="会话管理">
          <el-icon><ChatLineRound /></el-icon>
          <template #title>会话管理</template>
        </el-menu-item>
        <el-menu-item index="/reports" aria-label="历史与报告">
          <el-icon><Tickets /></el-icon>
          <template #title>历史与报告</template>
        </el-menu-item>
        <el-menu-item index="/members" aria-label="成员与企业">
          <el-icon><User /></el-icon>
          <template #title>成员与企业</template>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ auth.username || "用户" }}</span>
        <el-tag v-if="auth.isAdmin" size="small" type="danger">管理员</el-tag>
        <el-tag v-if="!auth.unassigned" size="small">{{ auth.orgLabel }}</el-tag>
        <el-tooltip v-else :content="unassignedNote">
          <!-- 没有归属与"没读到归属"是两件事：后者是这次 /me 没成功，说成前者就是
               把一次没测量印成一个结论（与队列深度那格同族）。 -->
          <el-tag size="small" :type="auth.orgsLoaded ? 'warning' : 'info'">
            {{ auth.orgsLoaded ? "未归属企业" : "归属未读到" }}
          </el-tag>
        </el-tooltip>
        <el-button link type="primary" aria-label="退出登录" @click="auth.logout(); router.push('/login')">
          退出
        </el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { ChatLineRound, Files, Histogram, Tickets, User } from "@element-plus/icons-vue";
import { UNAUTHORIZED_EVENT } from "./api/client";
import { useAuthStore } from "./stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const unassignedNote =
  "没有企业归属：只看得见自己的数据集与报告（共享读默认拒绝）。要协作请让管理员在建号时指定企业。" +
  "如果这一格显示的是“归属未读到”，说明这次 /me 没成功，回其它页面时会再读一次。";

// 断点只在这一处定义（CSS 里用不了自定义属性，所以不假装有一份"令牌"）。
const NARROW = "(max-width: 768px)";
const query = window.matchMedia(NARROW);
// 首帧就按当前宽度定布局：等 onMounted 再改，窄屏上会先闪一下 200px 的侧栏（一次布局位移）。
const narrow = ref(query.matches);

function syncNarrow() {
  narrow.value = query.matches;
}

// 服务端判 401（token 过期/被吊销）时统一收口：本地清干净再跳登录页
function onUnauthorized() {
  if (!auth.token) return;
  auth.logout();
  ElMessage.warning("登录已过期，请重新登录");
  if (route.path !== "/login") router.push("/login");
}

onMounted(() => {
  syncNarrow();
  query.addEventListener("change", syncNarrow);
  window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  // 每次开页都向服务端回读一次身份与企业归属：本地的 role/orgs 只是显示用的副本，
  // 企业成员关系与管理角色是会被管理员改的，界面不该拿着上次登录时的快照继续显示。
  // 失败不去动 token：401 由上面的拦截器统一收，其它错误（断网）保持可读的旧值。
  if (auth.isAuthed) auth.fetchMe().catch(() => undefined);
});
onBeforeUnmount(() => {
  query.removeEventListener("change", syncNarrow);
  window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
});
</script>

<style scoped>
.side {
  border-right: 1px solid var(--af-border);
  transition: width 0.2s;
}
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
  border-bottom: 1px solid var(--af-border);
}
/* 折叠时 EP 会给 el-menu 一条 0 宽的过渡边框，这里把内边距也一并对齐 */
.side :deep(.el-menu) {
  border-right: none;
}
</style>
