<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props=defineProps({serverTime:String})
const current=ref(Date.now()), offset=ref(0)
let timer
const formatter=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'})
const text=computed(()=>{
  const parts=Object.fromEntries(formatter.formatToParts(new Date(current.value+offset.value)).map(part=>[part.type,part.value]))
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
})
watch(()=>props.serverTime,value=>{const timestamp=Date.parse(value);if(Number.isFinite(timestamp)){offset.value=timestamp-Date.now();current.value=Date.now()}},{immediate:true})
onMounted(()=>{timer=setInterval(()=>{current.value=Date.now()},1000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template><time class="clock" aria-label="北京时间">{{text}}</time></template>
<style scoped>.clock{font-variant-numeric:tabular-nums;white-space:nowrap}</style>
