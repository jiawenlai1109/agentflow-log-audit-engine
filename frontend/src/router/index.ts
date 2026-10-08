import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "../stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", component: () => import("../views/LoginView.vue") },
    { path: "/", component: () => import("../views/WorkbenchView.vue") },
    { path: "/datasets", component: () => import("../views/DatasetsView.vue") },
    { path: "/sessions", component: () => import("../views/SessionsView.vue") },
    { path: "/reports", component: () => import("../views/ReportsView.vue") },
    { path: "/members", component: () => import("../views/MembersView.vue") },
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  if (to.path !== "/login" && !auth.isAuthed) {
    return "/login";
  }
  return true;
});

export default router;
