<template>
  <div class="home-page">
    <header class="home-header">
      <h1 class="home-title">PRTS Player Draw</h1>
      <p class="home-subtitle">明日方舟肉鸽赛事抽签工具</p>
    </header>

    <div class="category-grid">
      <div
        v-for="cat in categories"
        :key="cat.key"
        class="category-card"
        @click="goToDraw(cat.key)"
      >
        <div class="card-glow"></div>
        <div class="card-content">
          <h2 class="card-name">{{ cat.name }}</h2>
          <div class="card-count">{{ cat.count }}人</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { usePlayerStore } from '@/store/player'
import { countByRoguelike, getRoguelikeName } from '@/utils/filter'

const router = useRouter()
const store = usePlayerStore()

const categories = computed(() => {
  const counts = countByRoguelike(store.allPlayers)
  return Object.entries(counts).map(([key, count]) => ({
    key,
    name: getRoguelikeName(key),
    count
  }))
})

function goToDraw(roguelike: string): void {
  store.setRoguelike(roguelike)
  router.push(`/draw/${roguelike}`)
}

onMounted(async () => {
  if (store.allPlayers.length === 0) {
    await store.loadPlayers()
  }
})
</script>

<style scoped>
.home-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}

.home-header {
  text-align: center;
  margin-bottom: 60px;
}

.home-title {
  font-size: 48px;
  font-weight: 800;
  color: #f1f5f9;
  margin: 0;
  letter-spacing: 2px;
  text-shadow: 0 0 40px rgba(56, 189, 248, 0.3);
}

.home-subtitle {
  font-size: 16px;
  color: #64748b;
  margin-top: 12px;
  letter-spacing: 4px;
}

.category-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 24px;
  max-width: 1100px;
  width: 100%;
}

.category-card {
  position: relative;
  background: #1e293b;
  border-radius: 16px;
  border: 1px solid #334155;
  padding: 40px 32px;
  cursor: pointer;
  transition: all 0.3s ease;
  overflow: hidden;
  text-align: center;
}

.category-card:hover {
  border-color: #38bdf8;
  transform: translateY(-4px);
  box-shadow: 0 8px 32px rgba(56, 189, 248, 0.2);
}

.card-glow {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 200px;
  height: 200px;
  background: radial-gradient(circle, rgba(56, 189, 248, 0.08) 0%, transparent 70%);
  transform: translate(-50%, -50%);
  transition: opacity 0.3s;
  opacity: 0;
}

.category-card:hover .card-glow {
  opacity: 1;
}

.card-content {
  position: relative;
  z-index: 1;
}

.card-name {
  font-size: 22px;
  font-weight: 700;
  color: #e2e8f0;
  margin: 0 0 12px 0;
}

.card-count {
  font-size: 36px;
  font-weight: 800;
  color: #38bdf8;
}
</style>
