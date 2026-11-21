import { createRouter, createWebHashHistory } from 'vue-router'
import Home from '@/views/Home.vue'
import Draw from '@/views/Draw.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      name: 'Home',
      component: Home
    },
    {
      path: '/draw/:roguelike',
      name: 'Draw',
      component: Draw
    }
  ]
})

export default router
