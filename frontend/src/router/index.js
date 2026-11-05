import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({history:createWebHistory(),routes:[
  {path:'/',redirect:'/overview'}, {path:'/overview',component:()=>import('../views/Overview.vue')},
  {path:'/overview/published/:id',component:()=>import('../views/Overview.vue')},
  {path:'/personnel',component:()=>import('../views/Personnel.vue')}, {path:'/scheduling',component:()=>import('../views/Scheduling.vue')},
  {path:'/beds',component:()=>import('../views/Beds.vue')}, {path:'/workload',component:()=>import('../views/Workload.vue')},
  {path:'/settings',component:()=>import('../views/Settings.vue')}, {path:'/:pathMatch(.*)*',redirect:'/overview'}
]})
