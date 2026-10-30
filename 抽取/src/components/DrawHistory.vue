<template>
  <div class="draw-history" v-if="history.length > 0">
    <div class="history-list">
      <transition-group name="history-item" tag="div" class="history-items">
        <div
          v-for="(record, index) in history"
          :key="record.time + index"
          class="history-record"
        >
          <div class="record-time">{{ record.time }}</div>
          <div class="record-mode">{{ record.roguelikeName }}</div>
          <div class="record-matchup">
            <span class="record-player">{{ record.playerA.nickname }}</span>
            <span class="record-vs">VS</span>
            <span class="record-player">{{ record.playerB.nickname }}</span>
          </div>
        </div>
      </transition-group>
    </div>
  </div>
  <div v-else class="draw-history empty">
    <p class="empty-text">暂无抽签记录</p>
  </div>
</template>

<script setup lang="ts">
import type { DrawRecord } from '@/store/player'

defineProps<{
  history: DrawRecord[]
}>()
</script>

<style scoped>
.draw-history {
  margin-top: 40px;
  padding: 24px;
  background: #1e293b;
  border-radius: 16px;
  border: 1px solid #334155;
}

.draw-history.empty {
  text-align: center;
  padding: 32px;
}

.empty-text {
  color: #64748b;
  font-size: 14px;
}

.history-title {
  font-size: 16px;
  color: #94a3b8;
  margin: 0 0 16px 0;
  font-weight: 600;
}

.history-items {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.history-record {
  background: #0f172a;
  border-radius: 12px;
  padding: 12px 16px;
  border: 1px solid #334155;
  min-width: 200px;
  transition: all 0.3s ease;
}

.history-record:hover {
  border-color: #38bdf8;
}

.record-time {
  font-size: 12px;
  color: #64748b;
  margin-bottom: 4px;
}

.record-mode {
  font-size: 12px;
  color: #38bdf8;
  margin-bottom: 6px;
}

.record-matchup {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.record-player {
  color: #e2e8f0;
  font-weight: 600;
}

.record-vs {
  color: #f59e0b;
  font-weight: 700;
  font-size: 12px;
}

.history-item-enter-active {
  transition: all 0.4s ease;
}

.history-item-enter-from {
  opacity: 0;
  transform: translateY(-10px);
}
</style>
