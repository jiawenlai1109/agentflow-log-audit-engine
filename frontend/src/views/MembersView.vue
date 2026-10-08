<template>
  <div>
    <el-alert
      v-if="auth.unassigned"
      type="warning"
      :closable="false"
      style="margin-bottom: 12px"
      title="你还没有企业归属"
      description="共享读对未归属的账号默认拒绝：你只看得见自己上传的数据集与自己跑的报告。要协作，请让管理员建号时（或之后）把你放进一家企业。"
    />
    <el-card v-else style="margin-bottom: 12px">
      <span>我的企业：</span>
      <el-tag v-for="org in auth.orgs" :key="org.id" style="margin-right: 6px">
        {{ org.name || org.slug }}
      </el-tag>
      <span class="hint">同企业成员之间共享数据集与报告；会话与删除仍是个人的。</span>
    </el-card>

    <h3>成员名单</h3>
    <el-table :data="members" v-loading="loadingMembers">
      <el-table-column prop="username" label="账号" min-width="160">
        <template #default="{ row }">
          {{ row.username }}
          <el-tag v-if="row.is_me" size="small" type="success">我</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="org_id" label="企业 id" width="90" />
      <el-table-column prop="org_role" label="企业内角色" width="120" />
    </el-table>
    <p class="hint">
      这份名单按你的企业过滤（后端判的，不是前端筛的）：你看得见同企业有谁，看不见别家企业有谁。
      企业内角色目前只是记录，还不参与权限判定。
    </p>

    <template v-if="auth.isAdmin">
      <el-divider />
      <h3>建号（管理员）</h3>
      <el-form :model="form" label-width="90px" style="max-width: 520px">
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="3-32 位字母、数字或 _ . -" />
        </el-form-item>
        <el-form-item label="初始口令">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 12 位" />
        </el-form-item>
        <el-form-item label="企业">
          <el-select v-model="form.org" clearable placeholder="留空 = 未归属（只看得到自己的资源）" style="width: 100%">
            <el-option
              v-for="org in orgs"
              :key="org.id"
              :label="`${org.name}（${org.slug}）`"
              :value="org.slug"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="createAccount">创建账号</el-button>
          <span class="hint">口令只进这一次请求，前端不缓存、不回显。</span>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="error"
        type="error"
        :closable="true"
        style="max-width: 520px"
        :title="error"
      />

      <h3 style="margin-top: 20px">企业名单</h3>
      <el-table :data="orgs" v-loading="loadingOrgs">
        <el-table-column prop="id" label="id" width="70" />
        <el-table-column prop="slug" label="slug" width="160" />
        <el-table-column prop="name" label="名称" />
      </el-table>
      <p class="hint">
        现在还没有"在界面上开一家企业"的入口：企业由运维建（建号时选的就是这里的 slug）。
        那是单独一条决定，不在这个页面里顺手加。
      </p>
    </template>
    <el-alert
      v-else
      type="info"
      :closable="false"
      style="margin-top: 16px"
      title="建号只给管理员：你不是管理员，所以这个页面只提供名单。"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const members = ref<any[]>([]);
const orgs = ref<any[]>([]);
const loadingMembers = ref(false);
const loadingOrgs = ref(false);
const creating = ref(false);
const error = ref("");
const form = reactive<{ username: string; password: string; org: string }>({
  username: "",
  password: "",
  org: "",
});

/** 后端把拒绝原因写在 detail 里（重名 409、形状不对 422、没权限 403）。
 *  这里原样显示，不换成"创建失败"：管理员需要知道到底是哪一条。 */
function detail(error_: any, fallback: string) {
  return error_?.response?.data?.detail || fallback;
}

async function refresh() {
  loadingMembers.value = true;
  try {
    members.value = (await api.listMembers()).data;
  } finally {
    loadingMembers.value = false;
  }
  if (auth.isAdmin) {
    loadingOrgs.value = true;
    try {
      orgs.value = (await api.listOrgs()).data;
    } finally {
      loadingOrgs.value = false;
    }
  }
}

async function createAccount() {
  creating.value = true;
  error.value = "";
  try {
    const payload: { username: string; password: string; org?: string } = {
      username: form.username,
      password: form.password,
    };
    if (form.org) payload.org = form.org;
    const { data } = await api.createAccount(payload);
    ElMessage.success(`已创建 ${data.username}（企业 id ${data.org_id}）`);
    form.password = "";
    await refresh();
  } catch (caught) {
    error.value = detail(caught, "创建失败");
  } finally {
    creating.value = false;
  }
}

onMounted(async () => {
  // 身份/角色/企业以服务端为准：本地那份只是显示用的副本
  try {
    await auth.fetchMe();
  } catch (caught) {
    error.value = detail(caught, "读不到当前身份");
  }
  refresh();
});
</script>

<style scoped>
.hint {
  color: #909399;
  font-size: 12px;
}
</style>
