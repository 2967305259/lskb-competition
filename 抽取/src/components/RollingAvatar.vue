<template>
  <div class="rolling-avatar" :class="{ rolling: isRolling }">
    <img
      :src="currentAvatar"
      alt="rolling"
      class="roll-img"
      @error="handleAvatarError"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import type { Player } from '@/api/player'
import { getAvatarUrl, getFallbackAvatarUrl } from '@/utils/image'

const props = defineProps<{
  players: Player[]
  isRolling: boolean
}>()

const emit = defineEmits<{
  (e: 'done'): void
}>()

const currentAvatar = ref(getFallbackAvatarUrl())
let timer: ReturnType<typeof setInterval> | null = null

function getRandomAvatar(): string {
  if (props.players.length === 0) return getFallbackAvatarUrl()
  const p = props.players[Math.floor(Math.random() * props.players.length)]
  return getAvatarUrl(p.avatar)
}

function startRolling(): void {
  if (timer) return
  let speed = 40
  currentAvatar.value = getRandomAvatar()
  timer = setInterval(() => {
    currentAvatar.value = getRandomAvatar()
  }, speed)

  // 逐渐减速，2秒后停止
  const deceleration = setInterval(() => {
    speed += 40
    if (timer) {
      clearInterval(timer)
      timer = setInterval(() => {
        currentAvatar.value = getRandomAvatar()
      }, speed)
    }
    if (speed >= 400) {
      clearInterval(deceleration)
      stopRolling()
    }
  }, 300)
}

function stopRolling(): void {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  emit('done')
}

watch(
  () => props.isRolling,
  (val) => {
    if (val) {
      startRolling()
    } else {
      stopRolling()
    }
  }
)

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

function handleAvatarError(event: Event): void {
  const img = event.target as HTMLImageElement
  img.src = getFallbackAvatarUrl()
}
</script>

<style scoped>
.rolling-avatar {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  overflow: hidden;
  border: 3px solid #475569;
  transition: border-color 0.3s;
}

.rolling-avatar.rolling {
  border-color: #38bdf8;
  box-shadow: 0 0 20px rgba(56, 189, 248, 0.4);
}

.roll-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
</style>
