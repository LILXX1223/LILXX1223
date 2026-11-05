<script setup>
import { ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from 'echarts/core'
import { PieChart, BarChart, RadarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, RadarComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([PieChart,BarChart,RadarChart,TooltipComponent,LegendComponent,RadarComponent,GridComponent,CanvasRenderer])
const props=defineProps({kind:{type:String,required:true},nurses:{type:Array,default:()=>[]},skills:{type:Array,default:()=>[]}})
const node=ref(null);let chart,observer
const colors=['#328dff','#55d393','#ffa229','#ff766a','#9b5cff','#55c9d5']
function experience(){return [['≤3年',props.nurses.filter(n=>n.years<=3).length],['4–8年',props.nurses.filter(n=>n.years>=4&&n.years<=8).length],['9–15年',props.nurses.filter(n=>n.years>=9&&n.years<=15).length],['>15年',props.nurses.filter(n=>n.years>15).length]]}
function options(){
  const pie=data=>({color:colors,tooltip:{trigger:'item',formatter:'{b}：{c}人（{d}%）'},legend:{orient:'vertical',right:0,top:'middle',textStyle:{fontSize:11,color:'#355578'},formatter:name=>`${name}  ${data.find(x=>x.name===name)?.value||0}人`},series:[{type:'pie',radius:['52%','72%'],center:['29%','51%'],label:{show:false},data}]})
  if(props.kind==='experience-ring')return pie(experience().map(([name,value])=>({name,value})))
  if(props.kind==='base')return pie([['固定轮转',props.nurses.filter(n=>n.base_type==='rotation').length],['长期固定白班',props.nurses.filter(n=>n.base_type==='day').length]].map(([name,value])=>({name,value})))
  if(props.kind==='radar'){
    const skills=props.skills.filter(x=>x.enabled)
    if(!skills.length)return {title:{text:'暂无启用技能',left:'center',top:'middle',textStyle:{fontSize:12,color:'#94a6bb'}}}
    return {color:['#338dff'],tooltip:{trigger:'item',formatter:p=>p.value.map((v,i)=>`${skills[i].name}：${Number(v).toFixed(1)}%`).join('<br/>')},radar:{indicator:skills.map(x=>({name:x.name,max:100})),center:['38%','53%'],radius:'65%',axisName:{color:'#5b7592',fontSize:10},splitArea:{areaStyle:{color:['#f7fbff','#eef6ff']}},splitLine:{lineStyle:{color:'#dceafd'}}},series:[{type:'radar',data:[{value:skills.map(x=>Number(x.coverage)),areaStyle:{opacity:.24},symbolSize:4}]}]}
  }
  const values=props.kind==='level'?['N0','N1','N2','N3','N4'].map(name=>[name,props.nurses.filter(n=>n.level===name).length]):experience()
  return {color:colors,tooltip:{trigger:'axis',formatter:p=>`${p[0].name}：${p[0].value}人`},grid:{left:30,right:12,top:25,bottom:25},xAxis:{type:'category',data:values.map(x=>x[0]),axisTick:{show:false},axisLine:{lineStyle:{color:'#dbe8f7'}},axisLabel:{fontSize:10,color:'#55718f'}},yAxis:{type:'value',minInterval:1,axisLabel:{fontSize:10},splitLine:{lineStyle:{color:'#edf3fa'}}},series:[{type:'bar',barWidth:30,data:values.map((x,i)=>({value:x[1],itemStyle:{color:colors[i%colors.length],borderRadius:[3,3,0,0]}})),label:{show:true,position:'top',fontSize:10}}]}
}
async function render(){await nextTick();if(!node.value)return;chart ||= echarts.init(node.value);chart.setOption(options(),true)}
watch(()=>[props.kind,props.nurses,props.skills],render,{deep:true})
onMounted(()=>{render();observer=new ResizeObserver(()=>chart?.resize());observer.observe(node.value)})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose();chart=null})
</script>
<template><div ref="node" class="personnel-chart" role="img" :aria-label="({radar:'专科技能覆盖率雷达图','experience-ring':'ICU年资结构环图',base:'基础排班类型构成图',level:'护理层级分布图',experience:'ICU工作年限分布图'})[kind]"></div></template>
<style scoped>.personnel-chart{height:160px;width:100%;min-width:0}</style>
