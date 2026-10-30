/**
 * 获取玩家头像 URL
 * avatar 为文件名或 null，null 时返回兜底头像
 */
export function getAvatarUrl(avatar: string | null): string {
  if (!avatar) return getFallbackAvatarUrl()
  return `/image/${avatar}`
}

/**
 * 获取默认头像 URL
 */
export function getDefaultAvatarUrl(): string {
  return '/image/default.png'
}

/**
 * 内联 SVG 占位头像（纯 CSS 绘制，无需外部文件）
 */
const INLINE_PLACEHOLDER_SVG = `data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
    <rect fill="#1e293b" width="200" height="200" rx="100"/>
    <circle cx="100" cy="80" r="40" fill="#475569"/>
    <ellipse cx="100" cy="180" rx="70" ry="50" fill="#475569"/>
  </svg>`
)}`

/**
 * 处理头像加载失败，替换为默认头像
 */
export function handleAvatarError(event: Event): void {
  const img = event.target as HTMLImageElement
  if (img.src === getDefaultAvatarUrl()) {
    // 默认头像也加载失败，使用内联 SVG
    img.src = INLINE_PLACEHOLDER_SVG
  } else {
    img.src = getDefaultAvatarUrl()
  }
}

/**
 * 获取最终兜底头像（内联 SVG）
 */
export function getFallbackAvatarUrl(): string {
  return INLINE_PLACEHOLDER_SVG
}
