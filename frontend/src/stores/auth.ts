import { defineStore } from "pinia";
import { api } from "../api";

const TOKEN_KEY = "token";
const USER_KEY = "username";
const ROLE_KEY = "role";
const EXPIRES_KEY = "token_expires_at";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || "",
    username: localStorage.getItem(USER_KEY) || "",
    role: localStorage.getItem(ROLE_KEY) || "user",
    // 后端 token 带 exp，本地记下到期时刻只为提前跳登录页；真正的拒绝仍由服务端 401 判定
    expiresAt: Number(localStorage.getItem(EXPIRES_KEY) || 0),
  }),
  getters: {
    expired: (state) => state.expiresAt > 0 && state.expiresAt <= Date.now(),
    isAuthed: (state) =>
      !!state.token && !(state.expiresAt > 0 && state.expiresAt <= Date.now()),
  },
  actions: {
    async login(username: string, password: string) {
      const { data } = await api.login(username, password);
      this.setSession(data);
    },
    setSession(data: { token: string; username: string; role?: string; expires_in?: number }) {
      this.token = data.token;
      this.username = data.username;
      this.role = data.role || "user";
      this.expiresAt = data.expires_in ? Date.now() + data.expires_in * 1000 : 0;
      localStorage.setItem(TOKEN_KEY, this.token);
      localStorage.setItem(USER_KEY, this.username);
      localStorage.setItem(ROLE_KEY, this.role);
      localStorage.setItem(EXPIRES_KEY, String(this.expiresAt));
    },
    logout() {
      this.token = "";
      this.username = "";
      this.role = "user";
      this.expiresAt = 0;
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(ROLE_KEY);
      localStorage.removeItem(EXPIRES_KEY);
    },
  },
});
