<template>
  <div class="report" v-html="html"></div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import { demoteHeadings } from "../ui/reportHtml";

const props = defineProps<{ content: string }>();
const md = new MarkdownIt({ html: false, linkify: true });

const html = computed(() => demoteHeadings(md.render(props.content || "")));
</script>

<style>
.report img {
  max-width: 100%;
}
/* 报告里的明细表列数多（分诊队列/证据行常有七八列）：窄屏上让它在自己容器里横滚，
 * 而不是把对话框或正文撑出一条页面级横向滚动条。 */
.report table {
  display: block;
  overflow-x: auto;
  max-width: 100%;
}
</style>
