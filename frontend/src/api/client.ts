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
    // 后端的拒绝原文优先于 axios 那句 "Request failed with status code 429"。
    // 配额、重名、包名写错这类拒绝都带一句人话（哪个上限、当前几个、怎么改），
    // 翻成状态码等于把"可行动原因"扔回服务器日志里，界面只留一句没用的红字。
    // 只认字符串型的 detail：422 那份是结构化数组，硬塞进 message 会变成 [object Object]。
    const detail = error?.response?.data?.detail;
    if (typeof detail === "string" && detail.trim()) {
      error.message = detail;
    }
    return Promise.reject(error);
  }
);

export default client;
