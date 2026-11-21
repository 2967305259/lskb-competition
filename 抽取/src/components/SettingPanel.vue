<template>
  <div class="setting-panel">
    <el-checkbox
      v-model="noRepeatModel"
      size="large"
      class="setting-checkbox"
    >
      本轮不重复
    </el-checkbox>
    <el-checkbox
      v-model="avoidSameTeamModel"
      size="large"
      class="setting-checkbox"
    >
      避免同队
    </el-checkbox>
    <el-checkbox
      v-model="soundModel"
      size="large"
      class="setting-checkbox"
    >
      音效
    </el-checkbox>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  noRepeat: boolean
  avoidSameTeam: boolean
  soundEnabled: boolean
}>()

const emit = defineEmits<{
  (e: 'update:noRepeat', val: boolean): void
  (e: 'update:avoidSameTeam', val: boolean): void
  (e: 'update:soundEnabled', val: boolean): void
}>()

const noRepeatModel = computed({
  get: () => props.noRepeat,
  set: (val) => emit('update:noRepeat', val)
})

const avoidSameTeamModel = computed({
  get: () => props.avoidSameTeam,
  set: (val) => emit('update:avoidSameTeam', val)
})

const soundModel = computed({
  get: () => props.soundEnabled,
  set: (val) => emit('update:soundEnabled', val)
})
</script>

<style scoped>
.setting-panel {
  display: flex;
  gap: 24px;
  justify-content: center;
  flex-wrap: wrap;
}

.setting-checkbox {
  --el-checkbox-checked-text-color: #38bdf8;
  --el-checkbox-checked-bg-color: #2563eb;
  --el-checkbox-checked-input-border-color: #2563eb;
}

:deep(.el-checkbox__label) {
  color: #94a3b8 !important;
  font-size: 14px;
}

:deep(.el-checkbox.is-checked .el-checkbox__label) {
  color: #e2e8f0 !important;
}
</style>
