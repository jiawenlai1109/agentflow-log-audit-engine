<template>
  <div>
    <el-upload
      :auto-upload="false"
      :show-file-list="false"
      accept=".csv"
      :on-change="onFileChange"
    >
      <el-button type="primary">上传 CSV</el-button>
    </el-upload>

    <el-table :data="datasets" style="margin-top: 16px">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="filename" label="文件名" />
      <el-table-column prop="row_count" label="行数" width="100" />
      <el-table-column prop="size" label="大小(字节)" width="120" />
      <el-table-column label="列" min-width="220">
        <template #default="{ row }">{{ row.columns.join("、") }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="{ row }">
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";

const datasets = ref<any[]>([]);

async function refresh() {
  const { data } = await api.listDatasets();
  datasets.value = data;
}

async function onFileChange(file: any) {
  try {
    await api.uploadDataset(file.raw as File);
    ElMessage.success("上传成功");
    refresh();
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || "上传失败");
  }
}

async function remove(row: any) {
  await api.deleteDataset(row.id);
  refresh();
}

onMounted(refresh);
</script>
