<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { PieChart, BarChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([PieChart,BarChart,TooltipComponent,LegendComponent,GridComponent,CanvasRenderer])
const props=defineProps({kind:String,data:{type:[Object,Array],default:()=>({})}})
const node=ref(null);let chart,observer
function options(){
  if(props.kind==='levels'){
    const levels=props.data||{},known=['N0','N1','N2','N2-1','N2-2','N3','N4'],extra=Object.keys(levels).filter(x=>!known.includes(x)).sort()
    const data=[...known,...extra].map(name=>({name,value:levels[name]||0})).filter(x=>x.value>0)
    return {color:['#287eff','#38a5f5','#29c3bf','#55d7bd','#9364f0','#ffac47'],tooltip:{trigger:'item',formatter:'{b}：{c}人（{d}%）'},
      legend:{orient:'vertical',right:0,top:'middle',textStyle:{fontSize:11},formatter:name=>`${name}  ${data.find(x=>x.name===name)?.value||0}人`},
      series:[{type:'pie',radius:['48%','72%'],center:['30%','50%'],label:{show:false},data}]}
  }
  const rows=(props.data||[]).slice(0,8).reverse()
  return {color:['#3b96f5','#4bcf9a'],tooltip:{trigger:'axis'},legend:{top:0,textStyle:{fontSize:10}},
    grid:{left:95,right:25,top:35,bottom:25},xAxis:{type:'value',minInterval:1,splitLine:{lineStyle:{color:'#eef3f9'}}},
    yAxis:{type:'category',data:rows.map(x=>x.name),axisLabel:{width:85,overflow:'truncate',fontSize:10},axisTick:{show:false}},
    series:[{name:'登记技能',type:'bar',barWidth:10,data:rows.map(x=>x.skilled)},
      {name:'有效独立资格',type:'bar',barWidth:10,data:rows.map(x=>x.independent)}]}
}
async function render(){await nextTick();if(!node.value)return;chart ||= echarts.init(node.value);chart.setOption(options(),true)}
watch(()=>[props.kind,props.data],render,{deep:true})
onMounted(()=>{render();observer=new ResizeObserver(()=>chart?.resize());observer.observe(node.value)})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="node" class="overview-chart" role="img" :aria-label="kind==='levels'?'护理层级结构环图':'专科技能及独立资格分布图'"></div></template>
<style scoped>.overview-chart{height:220px;width:100%;min-width:0}</style>
