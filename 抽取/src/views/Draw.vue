<template>
  <div class="draw-page">
    <!-- 顶部栏 -->
    <header class="draw-header">
      <button class="back-btn" @click="goHome">
        ← 返回首页
      </button>
      <div class="header-center">
        <span class="mode-label">当前模式：</span>
        <span class="mode-value">{{ modeName }}</span>
      </div>
      <div class="header-right">
        <span class="remain-label">剩余：</span>
        <span class="remain-value">{{ store.remainingCount }} / {{ store.totalCount }}</span>
      </div>
    </header>

    <!-- 主区域 -->
    <main class="draw-main">
      <!-- 倒计时覆盖层 -->
      <transition name="countdown-fade">
        <div v-if="countdown > 0" class="countdown-overlay">
          <div class="countdown-number" :key="countdown">{{ countdown }}</div>
        </div>
      </transition>

      <!-- 抽签结果展现 -->
      <div class="result-area">
        <!-- 左侧玩家 -->
        <transition name="player-reveal" mode="out-in">
          <div v-if="showResult" class="player-slot left-slot">
            <PlayerCard :player="resultA" :revealed="true" />
          </div>
          <div v-else class="player-slot left-slot">
            <PlayerCard :player="null" :revealed="false" />
          </div>
        </transition>

        <!-- VS -->
        <transition name="vs-fade">
          <div v-if="showVs" class="vs-divider">
            <span class="vs-text">VS</span>
          </div>
        </transition>

        <!-- 右侧玩家 -->
        <transition name="player-reveal" mode="out-in">
          <div v-if="showResult" class="player-slot right-slot">
            <PlayerCard :player="resultB" :revealed="true" />
          </div>
          <div v-else class="player-slot right-slot">
            <PlayerCard :player="null" :revealed="false" />
          </div>
        </transition>
      </div>

      <!-- 操作按钮 -->
      <div class="action-buttons">
        <button
          class="btn btn-primary"
          :disabled="store.isRolling || store.totalCount < 2"
          @click="startDraw"
        >
          开始抽签
        </button>
        <button
          class="btn btn-secondary"
          :disabled="store.isRolling"
          @click="redraw"
        >
          重新抽取
        </button>
        <button
          class="btn btn-secondary"
          :disabled="store.isRolling"
          @click="resetRound"
        >
          重置本轮
        </button>
        <button
          class="btn btn-secondary"
          :disabled="store.isRolling"
          @click="reloadData"
        >
          重新读取数据
        </button>
      </div>

      <!-- 设置面板 -->
      <div class="settings">
        <SettingPanel
          :no-repeat="noRepeat"
          :avoid-same-team="avoidSameTeam"
          :sound-enabled="soundEnabled"
          @update:no-repeat="noRepeat = $event"
          @update:avoid-same-team="avoidSameTeam = $event"
          @update:sound-enabled="soundEnabled = $event"
        />
      </div>

      <!-- 不够人数提示 -->
      <div v-if="store.totalCount < 2 && store.allPlayers.length > 0" class="warning">
        当前分类人数不足，无法抽签
      </div>
    </main>

    <!-- 历史记录 -->
    <div class="history-section">
      <div class="history-header">
        <h3 class="history-title">最近抽签</h3>
        <button
          v-if="store.drawHistory.length > 0"
          class="btn-clear"
          @click="clearHistory"
        >
          清除历史
        </button>
      </div>
      <DrawHistory :history="store.drawHistory" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePlayerStore } from '@/store/player'
import { getRoguelikeName } from '@/utils/filter'
import type { Player } from '@/api/player'
import { pickTwoAvoidSameTeam, pickTwoRandom } from '@/utils/random'
import PlayerCard from '@/components/PlayerCard.vue'
import SettingPanel from '@/components/SettingPanel.vue'
import DrawHistory from '@/components/DrawHistory.vue'

const route = useRoute()
const router = useRouter()
const store = usePlayerStore()

const noRepeat = ref(true)
const avoidSameTeam = ref(true)
const soundEnabled = ref(true)

const countdown = ref(0)
const showResult = ref(false)
const showVs = ref(false)
const resultA = ref<Player | null>(null)
const resultB = ref<Player | null>(null)

const modeName = computed(() => {
  return getRoguelikeName(store.currentRoguelike)
})

// 音效播放
function playSound(name: string): void {
  if (!soundEnabled.value) return
  try {
    const audio = new Audio(`/audio/${name}.mp3`)
    audio.volume = 0.5
    audio.play().catch(() => {})
  } catch {
    // 忽略音效错误
  }
}

// 返回首页
function goHome(): void {
  playSound('click')
  router.push('/')
}

// 重置本轮
function resetRound(): void {
  playSound('click')
  store.resetRound()
  showResult.value = false
  showVs.value = false
  resultA.value = null
  resultB.value = null
}

// 清除历史
function clearHistory(): void {
  playSound('click')
  store.clearHistory()
}

// 重新读取数据
async function reloadData(): Promise<void> {
  playSound('click')
  await store.loadPlayers()
  store.resetRound()
  showResult.value = false
  showVs.value = false
  resultA.value = null
  resultB.value = null
}

// 执行抽取
function executeDraw(): Player[] {
  const excludeIds = noRepeat.value ? store.drawnIds : new Set<number>()
  const players = store.currentPlayers

  if (avoidSameTeam.value) {
    return pickTwoAvoidSameTeam(players, excludeIds)
  }
  return pickTwoRandom(players, excludeIds)
}

// 开始抽签
async function startDraw(): Promise<void> {
  if (store.isRolling) return
  if (store.totalCount < 2) return

  playSound('click')

  // 检查剩余人数
  if (noRepeat.value && store.remainingCount < 2) {
    // 不够两人，重置再抽
    store.resetRound()
  }

  showResult.value = false
  showVs.value = false
  resultA.value = null
  resultB.value = null

  // 倒计时
  store.setRolling(true)
  countdown.value = 3
  await delay(600)
  countdown.value = 2
  await delay(600)
  countdown.value = 1
  await delay(600)
  countdown.value = 0

  // 滚动阶段
  playSound('rolling')
  await delay(2000)

  // 抽取结果
  const players = executeDraw()

  if (players.length < 2) {
    store.setRolling(false)
    return
  }

  resultA.value = players[0]
  resultB.value = players[1]

  playSound('success')
  store.setRolling(false)

  // 显示结果动画
  await delay(200)
  showResult.value = true
  await delay(300)
  showVs.value = true

  // 记录
  if (noRepeat.value) {
    store.addDrawnId(players[0].id)
    store.addDrawnId(players[1].id)
  }

  store.setResult(players)

  const now = new Date()
  const time = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`

  store.addHistory({
    time,
    roguelike: store.currentRoguelike,
    roguelikeName: modeName.value,
    playerA: players[0],
    playerB: players[1]
  })
}

// 重新抽取
async function redraw(): Promise<void> {
  await startDraw()
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function pad(n: number): string {
  return n.toString().padStart(2, '0')
}

// 键盘快捷键
function handleKeydown(e: KeyboardEvent): void {
  // 防止在输入框中触发
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return

  switch (e.code) {
    case 'Space':
      e.preventDefault()
      startDraw()
      break
    case 'KeyR':
      e.preventDefault()
      redraw()
      break
    case 'Escape':
      e.preventDefault()
      goHome()
      break
  }
}

onMounted(async () => {
  const roguelike = route.params.roguelike as string
  if (store.allPlayers.length === 0) {
    await store.loadPlayers()
  }
  store.setRoguelike(roguelike)
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.draw-page {
  min-height: 100vh;
  max-width: 900px;
  margin: 0 auto;
  padding: 20px 20px 40px;
}

/* History Section */
.history-section {
  margin-top: 40px;
}

.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.history-title {
  font-size: 16px;
  color: #94a3b8;
  margin: 0;
  font-weight: 600;
}

.btn-clear {
  background: transparent;
  border: 1px solid #475569;
  color: #94a3b8;
  padding: 6px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.3s;
}

.btn-clear:hover {
  border-color: #ef4444;
  color: #ef4444;
}

/* Header */
.draw-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  background: #1e293b;
  border-radius: 16px;
  border: 1px solid #334155;
  margin-bottom: 40px;
}

.back-btn {
  background: transparent;
  border: 1px solid #475569;
  color: #94a3b8;
  padding: 8px 16px;
  border-radius: 10px;
  cursor: pointer;
  font-size: 14px;
  transition: all 0.3s;
}

.back-btn:hover {
  border-color: #38bdf8;
  color: #38bdf8;
}

.header-center {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mode-label {
  color: #64748b;
  font-size: 14px;
}

.mode-value {
  color: #38bdf8;
  font-size: 18px;
  font-weight: 700;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.remain-label {
  color: #64748b;
  font-size: 14px;
}

.remain-value {
  color: #f59e0b;
  font-size: 20px;
  font-weight: 700;
}

/* Main */
.draw-main {
  position: relative;
}

/* Countdown */
.countdown-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100vw;
  height: 100vh;
  background: rgba(15, 23, 42, 0.9);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.countdown-number {
  font-size: 120px;
  font-weight: 900;
  color: #38bdf8;
  text-shadow: 0 0 60px rgba(56, 189, 248, 0.5);
  animation: count-pop 0.5s ease;
}

@keyframes count-pop {
  0% {
    transform: scale(1.5);
    opacity: 0;
  }
  100% {
    transform: scale(1);
    opacity: 1;
  }
}

.countdown-fade-enter-active {
  transition: opacity 0.3s;
}

.countdown-fade-leave-active {
  transition: opacity 0.2s;
}

.countdown-fade-enter-from,
.countdown-fade-leave-to {
  opacity: 0;
}

/* Result Area */
.result-area {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 40px;
  margin-bottom: 40px;
  min-height: 300px;
}

.player-slot {
  flex-shrink: 0;
}

.vs-divider {
  flex-shrink: 0;
}

.vs-text {
  font-size: 48px;
  font-weight: 900;
  color: #f59e0b;
  text-shadow: 0 0 20px rgba(245, 158, 11, 0.4);
}

/* Transitions */
.player-reveal-enter-active {
  transition: all 0.5s ease;
}

.player-reveal-leave-active {
  transition: all 0.3s ease;
}

.player-reveal-enter-from {
  opacity: 0;
  transform: scale(0.9);
}

.player-reveal-enter-to {
  opacity: 1;
  transform: scale(1);
}

.player-reveal-leave-to {
  opacity: 0;
  transform: scale(0.9);
}

.vs-fade-enter-active {
  transition: all 0.5s ease 0.3s;
}

.vs-fade-leave-active {
  transition: all 0.2s ease;
}

.vs-fade-enter-from {
  opacity: 0;
  transform: scale(0.8);
}

.vs-fade-leave-to {
  opacity: 0;
}

/* Buttons */
.action-buttons {
  display: flex;
  gap: 12px;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 24px;
}

.btn {
  padding: 12px 28px;
  border-radius: 12px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  border: none;
  transition: all 0.3s ease;
  letter-spacing: 1px;
}

.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn-primary {
  background: #2563eb;
  color: #fff;
  box-shadow: 0 0 20px rgba(37, 99, 235, 0.3);
}

.btn-primary:hover:not(:disabled) {
  background: #3b82f6;
  box-shadow: 0 0 30px rgba(37, 99, 235, 0.5);
  transform: translateY(-2px);
}

.btn-secondary {
  background: #1e293b;
  color: #94a3b8;
  border: 1px solid #475569;
}

.btn-secondary:hover:not(:disabled) {
  border-color: #38bdf8;
  color: #38bdf8;
}

/* Settings */
.settings {
  margin-bottom: 24px;
}

/* Warning */
.warning {
  text-align: center;
  color: #f59e0b;
  padding: 12px;
  background: rgba(245, 158, 11, 0.1);
  border-radius: 10px;
  border: 1px solid rgba(245, 158, 11, 0.2);
  margin-top: 16px;
  font-size: 14px;
}
</style>
