<template>
  <div class="player-card" :class="{ revealed: revealed }">
    <div class="card-inner">
      <div class="avatar-wrapper">
        <img
          :src="avatarUrl"
          :alt="player?.nickname || '?'"
          class="avatar"
          @error="handleAvatarError"
        />
      </div>
      <transition name="info-fade">
        <div v-if="revealed && player" class="player-info">
          <div class="player-name">{{ player.nickname }}</div>
          <div class="player-team">{{ player.team?.name || '' }}</div>
        </div>
      </transition>
      <div v-if="!revealed" class="placeholder-text">?</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Player } from '@/api/player'
import { getAvatarUrl, getFallbackAvatarUrl } from '@/utils/image'

const props = defineProps<{
  player: Player | null
  revealed: boolean
}>()

const avatarUrl = computed(() => {
  if (!props.player) return getFallbackAvatarUrl()
  return getAvatarUrl(props.player.avatar)
})

function handleAvatarError(event: Event): void {
  const img = event.target as HTMLImageElement
  img.src = getFallbackAvatarUrl()
}
</script>

<style scoped>
.player-card {
  width: 200px;
  height: 280px;
  perspective: 1000px;
}

.card-inner {
  width: 100%;
  height: 100%;
  background: #1e293b;
  border-radius: 16px;
  border: 2px solid #334155;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  transition: all 0.5s ease;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
  position: relative;
  overflow: hidden;
}

.player-card.revealed .card-inner {
  border-color: #38bdf8;
  box-shadow: 0 0 30px rgba(56, 189, 248, 0.3), 0 4px 24px rgba(0, 0, 0, 0.3);
}

.avatar-wrapper {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  overflow: hidden;
  border: 3px solid #475569;
  transition: all 0.5s ease;
  flex-shrink: 0;
}

.player-card.revealed .avatar-wrapper {
  border-color: #38bdf8;
}

.avatar {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.player-info {
  text-align: center;
  margin-top: 16px;
}

.player-name {
  font-size: 22px;
  font-weight: 700;
  color: #f1f5f9;
  letter-spacing: 1px;
}

.player-team {
  font-size: 14px;
  color: #94a3b8;
  margin-top: 4px;
}

.placeholder-text {
  font-size: 48px;
  color: #475569;
  font-weight: 700;
}

.info-fade-enter-active {
  transition: all 0.4s ease 0.3s;
}

.info-fade-enter-from {
  opacity: 0;
  transform: translateY(10px);
}
</style>
