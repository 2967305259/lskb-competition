import type { Player } from '@/api/player'

export const ROGUELIKE_MAP: Record<string, string> = {
  hslh: '钟表头的黑流树海',
  garden: '岁的界园志异',
  sami: '探索者的银凇止境',
  sarkaz: '萨卡兹的无终奇语'
}

/**
 * 根据 roguelike 类型筛选玩家
 * roguelike 来自 team.roguelike
 */
export function filterByRoguelike(players: Player[], type: string): Player[] {
  return players.filter(p => p.team?.roguelike === type)
}

/**
 * 统计各分类人数
 */
export function countByRoguelike(players: Player[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const key of Object.keys(ROGUELIKE_MAP)) {
    counts[key] = filterByRoguelike(players, key).length
  }
  return counts
}

/**
 * 获取分类中文名
 */
export function getRoguelikeName(key: string): string {
  return ROGUELIKE_MAP[key] || key
}
