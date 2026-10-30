import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Player } from '@/api/player'
import { fetchPlayers } from '@/api/player'
import { filterByRoguelike } from '@/utils/filter'

export interface DrawRecord {
  time: string
  roguelike: string
  roguelikeName: string
  playerA: Player
  playerB: Player
}

export const usePlayerStore = defineStore('player', () => {
  // 所有玩家数据
  const allPlayers = ref<Player[]>([])

  // 当前分类
  const currentRoguelike = ref<string>('')

  // 已抽取的玩家 ID（防重复模式）
  const drawnIds = ref<Set<number>>(new Set())

  // 抽签历史
  const drawHistory = ref<DrawRecord[]>([])

  // 是否正在滚动动画
  const isRolling = ref(false)

  // 最终结果
  const result = ref<Player[]>([])

  // 加载数据
  async function loadPlayers(): Promise<void> {
    try {
      allPlayers.value = await fetchPlayers()
    } catch (e) {
      console.error('Failed to load players:', e)
    }
  }

  // 当前分类的玩家
  const currentPlayers = computed(() => {
    if (!currentRoguelike.value) return []
    return filterByRoguelike(allPlayers.value, currentRoguelike.value)
  })

  // 当前分类剩余可抽取玩家
  const remainingPlayers = computed(() => {
    return currentPlayers.value.filter(p => !drawnIds.value.has(p.id))
  })

  // 剩余人数
  const remainingCount = computed(() => remainingPlayers.value.length)

  // 总人数
  const totalCount = computed(() => currentPlayers.value.length)

  // 设置当前分类
  function setRoguelike(type: string): void {
    currentRoguelike.value = type
    drawnIds.value = new Set()
    result.value = []
    // 切换分类时不清理历史（保持最近10条全局记录）
  }

  // 添加已抽取 ID
  function addDrawnId(id: number): void {
    drawnIds.value = new Set([...drawnIds.value, id])
  }

  // 重置本轮（清空已抽取记录）
  function resetRound(): void {
    drawnIds.value = new Set()
    result.value = []
  }

  // 添加抽签历史
  function addHistory(record: DrawRecord): void {
    drawHistory.value = [record, ...drawHistory.value].slice(0, 10)
  }

  // 清除历史记录
  function clearHistory(): void {
    drawHistory.value = []
  }

  // 设置抽取结果
  function setResult(players: Player[]): void {
    result.value = players
  }

  // 设置滚动状态
  function setRolling(rolling: boolean): void {
    isRolling.value = rolling
  }

  return {
    allPlayers,
    currentRoguelike,
    drawnIds,
    drawHistory,
    isRolling,
    result,
    currentPlayers,
    remainingPlayers,
    remainingCount,
    totalCount,
    loadPlayers,
    setRoguelike,
    addDrawnId,
    resetRound,
    addHistory,
    clearHistory,
    setResult,
    setRolling
  }
})
