import axios from "axios";

/** 服务端判定身份失效时广播；由 App.vue 统一登出并跳登录页。
 *  这里不 import store：client ← api ← store 会构成循环依赖。 */
export const UNAUTHORIZED_EVENT = "agentflow:unauthorized";

const client = axios.create({ baseURL: "" });

client.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && !error.config?.url?.includes("/api/auth/login")) {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    }
    return Promise.reject(error);
  }
);

export default client;
