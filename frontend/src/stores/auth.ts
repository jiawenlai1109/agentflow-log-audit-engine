import { defineStore } from "pinia";
import { api } from "../api";

const TOKEN_KEY = "token";
const USER_KEY = "username";
const ROLE_KEY = "role";
const EXPIRES_KEY = "token_expires_at";
const ORGS_KEY = "orgs";

export type OrgBrief = { id: number; slug: string; name: string };

function readOrgs(): OrgBrief[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(ORGS_KEY) || "[]");
    // 本地这份只是"上次已知归属"，形状不对就当没有：拿它去显示可以，拿它去判定不行
    return Array.isArray(parsed) ? parsed.filter((item) => item && item.slug) : [];
  } catch {
    return [];
  }
}

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || "",
    username: localStorage.getItem(USER_KEY) || "",
    role: localStorage.getItem(ROLE_KEY) || "user",
    // 后端 token 带 exp，本地记下到期时刻只为提前跳登录页；真正的拒绝仍由服务端 401 判定
    expiresAt: Number(localStorage.getItem(EXPIRES_KEY) || 0),
    // 我在哪家企业：P3 之后"看得见谁"由它决定，所以界面要能让人自己看出来自己有没有归属
    orgs: readOrgs(),
    orgsLoaded: false,
  }),
  getters: {
    expired: (state) => state.expiresAt > 0 && state.expiresAt <= Date.now(),
    isAuthed: (state) =>
      !!state.token && !(state.expiresAt > 0 && state.expiresAt <= Date.now()),
    isAdmin: (state) => state.role === "admin",
    /** 没归属 = 共享读对他默认拒绝（看得见自己的，看不见同事的）。界面要把这件事说明白。 */
    unassigned: (state) => state.orgs.length === 0,
    orgLabel: (state) => state.orgs.map((org) => org.name || org.slug).join("、"),
  },
  actions: {
    async login(username: string, password: string) {
      const { data } = await api.login(username, password);
      this.setSession(data);
      // /me 读不到 **不等于登录失败**：token 已经到手，企业归属这一份只是显示用的副本。
      // 原来这里直接抛出去，症状是"明明登进去了还停在登录页"，而再点一次登录就撞限流。
      // 归属由 App.vue 每次进页回读，这里不必把一条附属读数升级成一次失败。
      await this.fetchMe().catch(() => undefined);
    },
    /** 刷新身份与企业归属。角色以这里为准：本地那份只是副本，服务端才是判据。 */
    async fetchMe() {
      try {
        const { data } = await api.me();
        this.orgs = data.orgs || [];
        this.role = data.role || "user";
        this.username = data.username || this.username;
        this.orgsLoaded = true;
        localStorage.setItem(ORGS_KEY, JSON.stringify(this.orgs));
        localStorage.setItem(ROLE_KEY, this.role);
        localStorage.setItem(USER_KEY, this.username);
      } catch (error) {
        this.orgsLoaded = false;
        throw error;
      }
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
      this.orgs = [];
      this.orgsLoaded = false;
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(ROLE_KEY);
      localStorage.removeItem(EXPIRES_KEY);
      localStorage.removeItem(ORGS_KEY);
    },
  },
});
