import { defineStore } from "pinia";
import { api } from "../api";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: localStorage.getItem("token") || "",
    username: localStorage.getItem("username") || "",
  }),
  getters: {
    isAuthed: (state) => !!state.token,
  },
  actions: {
    async login(username: string, password: string) {
      const { data } = await api.login(username, password);
      this.token = data.token;
      this.username = data.username;
      localStorage.setItem("token", data.token);
      localStorage.setItem("username", data.username);
    },
    logout() {
      this.token = "";
      this.username = "";
      localStorage.removeItem("token");
      localStorage.removeItem("username");
    },
  },
});
