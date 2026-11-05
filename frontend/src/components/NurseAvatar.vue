<script setup>
defineProps({nurse:{type:Object,required:true},tiny:{type:Boolean,default:false}})
</script>
<template><span :class="['avatar',{tiny}]"><img v-if="nurse.avatar_url" :src="nurse.avatar_url" :alt="nurse.name+'头像'"/><template v-else>{{nurse.name?.slice(-1)||'护'}}</template></span></template>
<style scoped>.avatar{overflow:hidden;flex-shrink:0}.avatar img{width:100%;height:100%;object-fit:cover}</style>
