import type { Player } from '@/api/player'

/**
 * 从玩家列表中随机抽取 n 个不重复的玩家
 */
export function pickRandom(players: Player[], count: number): Player[] {
  if (players.length < count) return []
  const shuffled = [...players].sort(() => Math.random() - 0.5)
  return shuffled.slice(0, count)
}

/**
 * 优先抽取不同队伍的两位玩家
 * 如果没有不同队伍可选，则允许同队
 */
export function pickTwoAvoidSameTeam(
  allPlayers: Player[],
  excludeIds: Set<number>
): Player[] {
  const available = allPlayers.filter(p => !excludeIds.has(p.id))
  if (available.length < 2) return []

  // 按队伍分组（team 是对象，取 team.name）
  const teamMap = new Map<string, Player[]>()
  for (const p of available) {
    const teamName = p.team?.name || '__no_team__'
    const list = teamMap.get(teamName) || []
    list.push(p)
    teamMap.set(teamName, list)
  }

  const teams = Array.from(teamMap.keys())

  if (teams.length >= 2) {
    // 随机选两个不同队伍
    const shuffledTeams = [...teams].sort(() => Math.random() - 0.5)
    const teamA = shuffledTeams[0]
    const teamB = shuffledTeams[1]

    const playerA = teamMap.get(teamA)!.sort(() => Math.random() - 0.5)[0]
    const playerB = teamMap.get(teamB)!.sort(() => Math.random() - 0.5)[0]

    return [playerA, playerB]
  } else {
    // 只有一个队伍，从中随机选两个
    return pickRandom(available, 2)
  }
}

/**
 * 普通随机抽取两位玩家（不保证不同队）
 */
export function pickTwoRandom(
  allPlayers: Player[],
  excludeIds: Set<number>
): Player[] {
  const available = allPlayers.filter(p => !excludeIds.has(p.id))
  return pickRandom(available, 2)
}
