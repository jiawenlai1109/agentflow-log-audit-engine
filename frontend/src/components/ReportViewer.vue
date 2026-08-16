<template>
  <div class="report" v-html="html"></div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";

const props = defineProps<{ content: string }>();
const md = new MarkdownIt({ html: false, linkify: true });

const html = computed(() => md.render(props.content || ""));
</script>

<style>
.report img {
  max-width: 100%;
}
</style>
