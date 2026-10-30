export interface TeamInfo {
  id: number
  name: string
  roguelike: string
  operator?: string
  squad?: string
  team_photo?: string
  final_score?: number
}

export interface Player {
  id: number
  nickname: string
  team: TeamInfo | null
  avatar: string | null
  username?: string
  role?: string
}

/**
 * 从 URL 中提取文件名
 */
function extractFilename(url: string): string {
  const parts = url.split('/')
  return parts[parts.length - 1] || url
}

/**
 * 从 public/data/players.json 读取玩家数据
 * 兼容新旧两种格式，自动跳过 team 为 null 的玩家
 */
export async function fetchPlayers(): Promise<Player[]> {
  const response = await fetch('/data/players.json')
  if (!response.ok) {
    throw new Error(`Failed to load players.json: ${response.status}`)
  }
  const raw = await response.json()

  // 新格式: { players: [...] }
  let players: any[]
  if (Array.isArray(raw)) {
    players = raw
  } else if (raw.players && Array.isArray(raw.players)) {
    players = raw.players
  } else {
    return []
  }

  // 转换为统一格式并跳过 team 为 null 的玩家
  return players
    .filter((p: any) => p.team != null)
    .map((p: any) => ({
      id: p.id,
      nickname: p.nickname || p.name || '',
      team: typeof p.team === 'string'
        ? { id: 0, name: p.team, roguelike: p.roguelike || '' }
        : {
            id: p.team?.id || 0,
            name: p.team?.name || '',
            roguelike: p.team?.roguelike || '',
            operator: p.team?.operator,
            squad: p.team?.squad,
            team_photo: p.team?.team_photo,
            final_score: p.team?.final_score
          },
      avatar: p.avatar ? extractFilename(p.avatar) : null,
      username: p.username,
      role: p.role
    }))
}
