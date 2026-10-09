import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
// 令牌层必须在组件库之后：这里覆盖的是 EP 自己的 Tag 文字色变量，顺序反了就白覆盖。
import "./styles/tokens.css";
import App from "./App.vue";
import router from "./router";

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(ElementPlus, { locale: zhCn });
app.mount("#app");
