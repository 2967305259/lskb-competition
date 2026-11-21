<template>
  <div class="app-container">
    <router-view v-slot="{ Component }">
      <transition name="page-fade" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>
</template>

<script setup lang="ts">
</script>

<style>
/* 全局样式 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body {
  font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  min-height: 100vh;
  overflow-x: hidden;
}

body {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* 滚动条 */
::-webkit-scrollbar {
  width: 6px;
}

::-webkit-scrollbar-track {
  background: #1e293b;
}

::-webkit-scrollbar-thumb {
  background: #475569;
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: #64748b;
}

/* 页面切换动画 */
.page-fade-enter-active {
  transition: all 0.3s ease;
}

.page-fade-leave-active {
  transition: all 0.2s ease;
}

.page-fade-enter-from {
  opacity: 0;
  transform: translateY(10px);
}

.page-fade-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}
</style>

<style scoped>
.app-container {
  min-height: 100vh;
}
</style>
