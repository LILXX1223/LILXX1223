<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { Users, UserCheck, Layers, CalendarDays } from 'lucide-vue-next'
import { ElMessageBox, ElMessage } from 'element-plus'
import { useWorkspace } from '../stores/workspace'
import { api, uploadFile, shiftName, today, downloadCSV } from '../services/api'
import SkillManager from '../components/SkillManager.vue'
import PersonnelChart from '../components/PersonnelChart.vue'
import AvatarEditor from '../components/AvatarEditor.vue'
import NurseAvatar from '../components/NurseAvatar.vue'
import ExcelImport from '../components/ExcelImport.vue'
import GroupAllocation from '../components/GroupAllocation.vue'
import GroupUndo from '../components/GroupUndo.vue'
const s=useWorkspace(), query=ref(''), subgroup=ref(''), level=ref(''), base=ref(''), status=ref(''), qualification=ref(''), skillFilter=ref(''), page=ref(1), pageSize=ref(10), editOpen=ref(false), detailOpen=ref(false), form=ref({}), selected=ref(null), skillOpen=ref(false), skillCatalog=ref([]), skillError=ref('')
const skills=computed(()=>skillCatalog.value.filter(x=>x.enabled).map(x=>x.name))
const avatarChange=ref({file:null,remove:false,loading:false})
const importOpen=ref(false)
const groupOpen=ref(false)
const undoOpen=ref(false)
// A cleared current group may still have a restorable formal predecessor.
const formalState=ref(null)
let formalRequest=0
async function loadFormalState(){
  const group=String(s.group), request=++formalRequest
  formalState.value=null
  if(!group||!s.canEdit)return
  try {const result=await api('grouping/formal/?'+new URLSearchParams({group}));if(request===formalRequest&&group===String(s.group))formalState.value=result}
  catch { /* Leave the dialog available so that its existing retry can report the error. */ }
}
const undoDisabled=computed(()=>!s.data.nurses.some(n=>n.subgroup!=null)&&formalState.value?.can_restore===false)
function groupRole(n){
  if(n.subgroup==null)return '未分组'
  const leader=s.data.group.rules?.actual_leaders?.[String(n.subgroup)]
  return n.is_leader&&String(leader)===String(n.id)?'带组老师':'组员'
}
const rows=computed(()=>s.data.nurses.filter(n=>(!query.value||n.name.includes(query.value)||n.employee_id.includes(query.value))&&(!subgroup.value||String(n.subgroup??'none')===String(subgroup.value))&&(!level.value||n.level===level.value)&&(!base.value||n.base_type===base.value)&&(!status.value||n.status===status.value)&&(!qualification.value||n.qualifications.includes(qualification.value))&&(!skillFilter.value||n.skills.includes(skillFilter.value))))
const visible=computed(()=>rows.value.slice((page.value-1)*pageSize.value,page.value*pageSize.value))
function openEdit(n){avatarChange.value={file:null,remove:false,loading:false};form.value=n?JSON.parse(JSON.stringify(n)):{name:'',employee_id:'',subgroup:null,is_leader:false,level:'N1',years:0,title:'护师',gender:'女',phone:'',base_type:'rotation',qualifications:['A','N'],skills:[],certifications:[],workdays:[0,1,2,3,4],can_manage_beds:true,status:'active',effective_from:today(),notes:'',teaching_collaboration:'',assessment_level:null,special_restrictions:''};form.value.skill_levels=Object.fromEntries((n?.skill_details||[]).map(item=>[String(item.id),item.level]));form.value.group=Number(s.group);editOpen.value=true}
async function save(){
  if(!form.value.name?.trim()||!form.value.employee_id?.trim()||!form.value.effective_from)return ElMessage.warning('请填写姓名、工号和生效日期。')
  if(avatarChange.value.loading)return ElMessage.warning('请等待头像裁剪预览完成。')
  if(form.value.certifications.some(c=>!c.expires_on))return ElMessage.warning('请为每项独立资质填写到期日期。')
  const data=JSON.parse(JSON.stringify(form.value))
  const selectedSkills=new Set([...data.skills,...data.certifications.map(c=>c.name)])
  data.skill_levels=Object.fromEntries(Object.entries(data.skill_levels||{}).filter(([id])=>selectedSkills.has(skillCatalog.value.find(item=>String(item.id)===id)?.name)))
  if(data.status!=='active'){try{await ElMessageBox.confirm('停排或离职将影响后续排班与分床校验，确认保存？','确认人员状态')}catch{return}}
  if(data.group!==Number(s.group)||!editOpen.value)return
  const pending=avatarChange.value
  const v=await s.run(async()=>{
    const saved=await api('nurses/'+(data.id?data.id+'/':''),data.id?'PATCH':'POST',data)
    form.value.id=saved.id;form.value.revision=saved.revision
    try{
      if(pending.file){const upload=new FormData();upload.append('group',String(data.group));upload.append('revision',String(saved.revision));upload.append('file',pending.file);await uploadFile(`nurses/${saved.id}/avatar/`,upload)}
      else if(pending.remove&&saved.avatar_url)await api(`nurses/${saved.id}/avatar/`,'DELETE',{group:data.group,revision:saved.revision})
    }catch(error){await s.load(true);throw new Error('护士档案已保存，但头像未保存：'+error.message+'；可重新编辑头像。')}
    return saved
  },'护士档案已保存');if(v)editOpen.value=false
}
function detail(n){selected.value=n}
async function removeNurse(n){
  const group=Number(s.group), revision=n.revision
  try{await ElMessageBox.confirm(`确认删除护士 ${n.name}（工号 ${n.employee_id}）？`,'确认删除护士',{type:'warning',confirmButtonText:'确认删除',cancelButtonText:'取消'})}catch{return}
  if(group!==Number(s.group))return
  await s.run(()=>api(`nurses/${n.id}/`,'DELETE',{group,revision}),'护士档案已删除，历史发布记录保留')
}
async function loadSkills(){const group=s.group;try{const result=await api('skills/?'+new URLSearchParams({group}));if(group===s.group){skillCatalog.value=result.items;skillError.value=''}}catch(e){skillError.value=e.message;skillCatalog.value=[]}}
function certification(name,enabled){form.value.certifications=form.value.certifications.filter(c=>c.name!==name);if(enabled)form.value.certifications.push({name,expires_on:addYear()})}
function addYear(){const d=new Date();return `${d.getFullYear()+1}-12-31`}
function exportRows(){downloadCSV('护士档案.csv',[['姓名','工号','小组','层级','基础类型','资格','状态'],...rows.value.map(n=>[n.name,n.employee_id,n.subgroup,n.level,n.base_type==='day'?'长期白班':'固定轮转',n.qualifications.map(shiftName).join('、'),n.status])])}
watch(()=>s.group,()=>{editOpen.value=false;detailOpen.value=false;selected.value=s.data.group.id===Number(s.group)?s.data.nurses[0]||null:null;page.value=1;loadSkills()})
watch(()=>s.data.nurses,items=>{selected.value=items.find(n=>n.id===selected.value?.id)||items[0]||null},{immediate:true})
watch(()=>[s.group,s.data.nurses],loadFormalState,{immediate:true})
watch(()=>rows.value.length,()=>{page.value=Math.min(page.value,Math.max(1,Math.ceil(rows.value.length/pageSize.value)))})
onMounted(loadSkills)
</script>
<template><div class="page-heading"><div><h1>人员信息</h1><p>维护本组护士档案、分组归属、排班属性、专科技能及资质信息。</p></div><div class="actions"><el-button @click="exportRows">导出</el-button><el-button v-if="s.canEdit" @click="importOpen=true">Excel智能导入</el-button><el-button v-if="s.canEdit" @click="groupOpen=true">AI辅助小组分配</el-button><el-button v-if="s.canEdit" type="danger" plain :disabled="undoDisabled" @click="undoOpen=true">撤销当前分组</el-button><el-button v-if="s.canEdit" type="primary" @click="openEdit()">＋ 新增护士</el-button></div></div>
<div class="personnel-metrics">
  <div v-for="[label,value,unit,icon,extra] in [
    ['本组护士总数',s.data.nurses.length,'人',Users,'轮转 '+s.data.nurses.filter(n=>n.base_type==='rotation').length+' 人 / 长期白班 '+s.data.nurses.filter(n=>n.base_type==='day').length+' 人'],
    ['所属小组数',new Set(s.data.nurses.map(n=>n.subgroup).filter(x=>x!=null)).size,'个',Layers,'按已有人员实际分组统计'],
    ['带组老师数',s.data.nurses.filter(n=>groupRole(n)==='带组老师').length,'人',UserCheck,'当前已指定']]" :key="label" class="kpi"><span class="symbol"><component :is="icon" :size="24"/></span><div><label>{{label}}</label><strong>{{value}}</strong><small>{{unit}}</small><p class="muted small">{{extra}}</p></div></div>
  <section class="metric-chart"><h3>年资结构</h3><PersonnelChart kind="experience-ring" :nurses="s.data.nurses"/></section>
  <section class="metric-chart"><div class="card-title"><h3>专科技能覆盖率</h3><el-button link type="primary" @click="skillOpen=true">管理技能</el-button></div><PersonnelChart kind="radar" :nurses="s.data.nurses" :skills="skillCatalog"/><small v-if="skillError" class="muted">技能数据暂不可用：{{skillError}}</small><small v-else class="muted">按在岗护士及启用技能计算</small></section>
</div>
<div class="personnel-layout"><div class="personnel-left">
<section class="card"><h2>护士人员列表（{{s.data.group.name}}）</h2><div class="filters">
  <el-input v-model="query" placeholder="姓名或工号" clearable @input="page=1"/>
  <el-select v-model="subgroup" placeholder="所属小组" clearable @change="page=1"><el-option v-for="n in 4" :key="n" :value="n" :label="'第'+n+'小组'"/><el-option value="none" label="待分组"/></el-select>
  <el-select v-model="level" placeholder="层级" clearable @change="page=1"><el-option v-for="n in ['N0','N1','N2','N3','N4']" :key="n" :value="n"/></el-select>
  <el-select v-model="base" placeholder="基础排班类型" clearable @change="page=1"><el-option value="rotation" label="固定轮转"/><el-option value="day" label="长期固定白班"/></el-select>
  <el-select v-model="qualification" placeholder="排班资格" clearable @change="page=1"><el-option v-for="q in ['A','DAY','N']" :key="q" :value="q" :label="shiftName(q)"/></el-select>
  <el-select v-model="skillFilter" placeholder="专科技能" clearable @change="page=1"><el-option v-for="sk in skills" :key="sk" :value="sk"/></el-select>
  <el-select v-model="status" placeholder="在岗状态" clearable @change="page=1"><el-option value="active" label="在岗"/><el-option value="paused" label="停排"/><el-option value="left" label="离职"/></el-select>
</div>
<el-table :data="visible" row-key="id" highlight-current-row @row-click="detail">
  <el-table-column type="selection" width="42" fixed="left"/>
  <el-table-column label="护士" width="110" fixed="left"><template #default="{row}"><el-button link type="primary" @click.stop="detail(row)"><NurseAvatar :nurse="row" tiny/>{{row.name}}</el-button></template></el-table-column>
  <el-table-column prop="employee_id" label="工号" width="100"/><el-table-column prop="level" label="护理层级" width="90"/>
  <el-table-column label="所属小组" width="95"><template #default="{row}">{{row.subgroup?'第'+row.subgroup+'小组':'待分组'}}</template></el-table-column>
  <el-table-column label="小组角色" width="95"><template #default="{row}"><el-tag v-if="groupRole(row)==='带组老师'" type="info">带组老师</el-tag><span v-else>{{groupRole(row)}}</span></template></el-table-column>
  <el-table-column label="基础排班类型" width="140"><template #default="{row}"><el-tag :type="row.base_type==='day'?'warning':'primary'">{{row.base_type==='day'?'长期固定白班':'固定轮转'}}</el-tag></template></el-table-column>
  <el-table-column label="排班资格" width="190"><template #default="{row}"><span class="qualification-tags"><span v-for="q in row.qualifications" :key="q" :class="['shift-badge','shift-'+q]">{{shiftName(q)}}</span></span></template></el-table-column>
  <el-table-column label="主要专科技能" min-width="160"><template #default="{row}"><el-tag v-for="sk in row.skills" :key="sk" size="small" type="info">{{sk}}</el-tag><span v-if="!row.skills.length">—</span></template></el-table-column>
  <el-table-column label="ICU年资" width="82"><template #default="{row}">{{row.years}} 年</template></el-table-column>
  <el-table-column label="病情评估能级" width="115"><template #default="{row}">{{row.assessment_level?row.assessment_level+'档':'未记录'}}</template></el-table-column>
  <el-table-column prop="teaching_collaboration" label="带教与协作能力" width="150" show-overflow-tooltip/>
  <el-table-column label="状态" width="80"><template #default="{row}"><el-tag :type="row.status==='active'?'success':'warning'">{{({active:'在岗',paused:'停排',left:'离职'})[row.status]}}</el-tag></template></el-table-column>
  <el-table-column label="操作" width="150" fixed="right"><template #default="{row}"><el-button link type="primary" @click.stop="detail(row)">查看</el-button><el-button v-if="s.canEdit" link type="primary" @click.stop="openEdit(row)">编辑</el-button><el-button v-if="s.canEdit" link type="danger" @click.stop="removeNurse(row)">删除</el-button></template></el-table-column>
</el-table><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10,20,50]" :total="rows.length" layout="total, sizes, prev, pager, next"/></section>
<div class="personnel-charts"><section v-for="[kind,title] in [['base','基础排班类型构成'],['level','护理层级结构分布'],['experience','ICU工作年限分布']]" :key="kind" class="card"><h3>{{title}}</h3><PersonnelChart :kind="kind" :nurses="s.data.nurses"/></section></div>
</div><aside class="card personnel-detail"><div class="card-title"><h2>护士详情</h2><el-button v-if="selected&&s.canEdit" link type="primary" @click="openEdit(selected)">编辑信息</el-button></div>
  <template v-if="selected"><div class="detail-identity"><NurseAvatar :nurse="selected"/><div><b>{{selected.name}}</b> <span class="muted small">{{selected.employee_id}}</span><p class="small">{{selected.gender}} · {{selected.level}} · {{selected.title}} · ICU {{selected.years}}年</p></div></div>
    <h3>基本信息</h3><dl class="detail-list"><dt>工号</dt><dd>{{selected.employee_id}}</dd><dt>联系方式</dt><dd>{{selected.phone||'—'}}</dd></dl>
    <h3>分组归属</h3><dl class="detail-list"><dt>所属大组</dt><dd>{{s.data.group.name}}</dd><dt>所属小组</dt><dd>{{selected.subgroup?'第'+selected.subgroup+'小组':'待分组'}}</dd><dt>带组资格</dt><dd>{{selected.is_leader?'有':'无'}}</dd><dt>当前角色</dt><dd>{{groupRole(selected)}}</dd></dl>
    <h3>排班信息</h3><dl class="detail-list"><dt>基础类型</dt><dd>{{selected.base_type==='day'?'长期固定白班':'固定轮转'}}</dd><dt>排班资格</dt><dd>{{selected.qualifications.map(shiftName).join('、')||'—'}}</dd><dt>特殊限制</dt><dd>{{selected.special_restrictions||'无'}}</dd></dl>
    <h3>专科技能</h3><div><el-tag v-for="sk in selected.skills" :key="sk" size="small" type="info">{{sk}}</el-tag><span v-if="!selected.skills.length" class="muted small">暂无</span></div><p v-for="item in selected.skill_details" :key="item.id" class="muted small">{{item.name}} · {{item.level||'掌握程度未记录'}} <span v-if="item.independent">· 独立资质 {{item.expires_on||'有效期待核对'}}</span><span v-if="!item.enabled"> · 已停用</span></p>
    <h3>其他信息</h3><dl class="detail-list"><dt>在岗状态</dt><dd>{{({active:'在岗',paused:'停排',left:'离职'})[selected.status]}}</dd><dt>带教与协作</dt><dd>{{selected.teaching_collaboration||'未记录'}}</dd><dt>病情评估</dt><dd>{{selected.assessment_level?selected.assessment_level+'档':'未记录'}}</dd><dt>备注</dt><dd>{{selected.notes||'—'}}</dd></dl>
  </template><el-empty v-else description="暂无护士资料"/></aside></div>
<SkillManager v-model="skillOpen" @changed="loadSkills();s.load(true)"/>
<ExcelImport v-model="importOpen" :skills="skillCatalog" @changed="loadSkills();s.load(true)"/>
<GroupAllocation v-model="groupOpen" @changed="s.load(true)"/>
<GroupUndo v-model="undoOpen" @changed="s.load(true)"/>
<el-dialog v-model="editOpen" :title="form.id?'编辑护士信息':'新增护士'" width="780px" :close-on-click-modal="false"><el-form label-position="top"><div class="form-section"><h3>1　基本信息</h3><AvatarEditor :key="form.id||'new'" :url="form.avatar_url" @change="avatarChange=$event"/><div class="form-grid"><el-form-item label="姓名 *"><el-input v-model="form.name" maxlength="50"/></el-form-item><el-form-item label="工号 *"><el-input v-model="form.employee_id" maxlength="40"/></el-form-item><el-form-item label="护理层级"><el-select v-model="form.level"><el-option v-for="v in ['N0','N1','N2','N3','N4']" :key="v" :value="v"/></el-select></el-form-item><el-form-item label="ICU工作年限"><el-input-number v-model="form.years" :min="0" :max="60"/></el-form-item><el-form-item label="职称"><el-input v-model="form.title"/></el-form-item><el-form-item label="性别"><el-radio-group v-model="form.gender"><el-radio value="女">女</el-radio><el-radio value="男">男</el-radio></el-radio-group></el-form-item><el-form-item label="联系方式"><el-input v-model="form.phone"/></el-form-item></div></div><div class="form-section"><h3>2　分组归属</h3><div class="form-grid"><el-form-item label="所属大组"><el-input :model-value="s.data.group.name" disabled/></el-form-item><el-form-item label="所属小组"><el-select v-model="form.subgroup"><el-option :value="null" label="待分组"/><el-option v-for="n in 4" :key="n" :value="n" :label="'第'+n+'小组'"/></el-select></el-form-item></div><el-checkbox v-model="form.is_leader">具备带组老师身份</el-checkbox></div><div class="form-section"><h3>3　排班信息</h3><el-form-item label="基础排班类型"><el-radio-group v-model="form.base_type"><el-radio value="rotation">固定轮转</el-radio><el-radio value="day">长期固定白班</el-radio></el-radio-group></el-form-item><el-form-item label="排班资格（独立于基础类型）"><el-checkbox-group v-model="form.qualifications"><el-checkbox v-for="code in ['A','DAY','N','TRAIN']" :key="code" :value="code">{{shiftName(code)}}</el-checkbox></el-checkbox-group></el-form-item><el-form-item v-if="form.base_type==='day'" label="长期白班工作日"><el-checkbox-group v-model="form.workdays"><el-checkbox v-for="(day,i) in ['周一','周二','周三','周四','周五','周六','周日']" :key="day" :value="i">{{day}}</el-checkbox></el-checkbox-group></el-form-item><el-form-item label="生效日期"><el-date-picker v-model="form.effective_from" value-format="YYYY-MM-DD"/></el-form-item><el-checkbox v-model="form.can_manage_beds">岗位允许承担床位责任</el-checkbox></div><div class="form-section"><h3>4　专科技能与独立资质</h3><el-checkbox-group v-model="form.skills"><el-checkbox v-for="sk in skills" :key="sk" :value="sk">{{sk}}</el-checkbox></el-checkbox-group><p class="muted small">技能标签不等于独立护理资质。下列资格须由管理者核对有效期；已停用项目不参与有效匹配。</p><div v-for="item in skillCatalog.filter(x=>form.skills.includes(x.name)||form.certifications.some(c=>c.name===x.name))" :key="item.id" class="legend-row"><span>{{item.name}} <el-tag v-if="!item.enabled" type="info" size="small">已停用</el-tag></span><el-input v-model="form.skill_levels[String(item.id)]" placeholder="技能掌握程度（按资料填写）" style="width:220px" maxlength="100"/></div><div v-for="sk in skills" :key="sk" class="legend-row"><el-checkbox :model-value="form.certifications.some(c=>c.name===sk)" @change="v=>certification(sk,v)">{{sk}} 独立资质</el-checkbox><el-date-picker v-if="form.certifications.some(c=>c.name===sk)" v-model="form.certifications.find(c=>c.name===sk).expires_on" value-format="YYYY-MM-DD" size="small" placeholder="到期日期" style="width:160px"/></div></div><div class="form-section"><h3>5　其他信息</h3><el-radio-group v-model="form.status"><el-radio value="active">在岗</el-radio><el-radio value="paused">停排</el-radio><el-radio value="left">离职</el-radio></el-radio-group><el-form-item label="特殊排班限制"><el-input v-model="form.special_restrictions" type="textarea" maxlength="2000" placeholder="填写须由管理者核对的排班限制"/></el-form-item><el-form-item label="带教与协作能力"><el-input v-model="form.teaching_collaboration" type="textarea" maxlength="1000" placeholder="填写真实带教及协作资料"/></el-form-item><el-form-item label="病情评估能力能级"><el-select v-model="form.assessment_level" clearable placeholder="未记录"><el-option v-for="n in 5" :key="n" :value="n" :label="n+'档'"/></el-select></el-form-item><el-input v-model="form.notes" type="textarea" placeholder="备注" maxlength="500" show-word-limit class="section-gap"/></div><el-alert title="保存会影响后续草稿校验；已发布历史版本保持原样。" type="info" :closable="false"/></el-form><template #footer><el-button @click="editOpen=false">取消</el-button><el-button type="primary" :loading="s.busy" @click="save">保存档案</el-button></template></el-dialog>
<el-drawer v-model="detailOpen" title="护士详情" size="440px"><template v-if="selected"><h2><span class="avatar">{{selected.name.slice(-1)}}</span> {{selected.name}} <el-tag>{{selected.level}}</el-tag></h2><dl class="detail-list"><dt>工号</dt><dd>{{selected.employee_id}}</dd><dt>所属分组</dt><dd>{{s.data.group.name}} / 第{{selected.subgroup}}小组</dd><dt>岗位角色</dt><dd>{{selected.is_leader?'带组老师':'组员'}}</dd><dt>ICU年资</dt><dd>{{selected.years}}年</dd><dt>职称</dt><dd>{{selected.title}}</dd><dt>基础排班</dt><dd>{{selected.base_type==='day'?'长期固定白班':'固定轮转'}}</dd><dt>排班资格</dt><dd>{{selected.qualifications.map(shiftName).join('、')}}</dd><dt>专科技能</dt><dd>{{selected.skills.join('、')||'暂无'}}</dd><dt>独立护理资质</dt><dd><p v-for="c in selected.certifications" :key="c.name">{{c.name}} · {{c.expires_on}} 到期</p></dd><dt>备注</dt><dd>{{selected.notes||'—'}}</dd></dl><el-button v-if="s.canEdit" type="primary" @click="detailOpen=false;openEdit(selected)">编辑信息</el-button></template></el-drawer>
</template>
<style scoped>
.personnel-metrics{display:grid;grid-template-columns:repeat(3,minmax(135px,1fr)) minmax(220px,1.5fr) minmax(210px,1.3fr);gap:12px;margin-bottom:16px}
.personnel-metrics .kpi,.metric-chart{min-width:0;height:184px;align-items:center}.personnel-metrics .kpi p{margin:8px 0 0;line-height:1.35}.metric-chart{background:#fff;border:1px solid #deebf8;border-radius:9px;padding:12px}.metric-chart h3{margin:0;font-size:14px}.metric-chart :deep(.personnel-chart){height:138px}.metric-chart .card-title{margin:0}.metric-chart small{display:block;margin-top:-2px;font-size:10px}
.personnel-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px}.personnel-left{min-width:0}.personnel-left :deep(.el-table td.el-table__cell){padding:6px 0}.personnel-left :deep(.el-table th.el-table__cell){padding:10px 0}.qualification-tags{display:inline-flex;white-space:nowrap}.qualification-tags .shift-badge{min-width:36px;padding:4px 5px}.personnel-detail{min-width:0}.personnel-detail h3{margin:14px -6px 8px;padding:7px;background:#edf6ff;border-radius:4px;font-size:14px}.personnel-detail .detail-list{grid-template-columns:90px 1fr;gap:8px;margin:8px 0}.detail-identity{display:flex;gap:10px;align-items:center}.detail-identity p{margin:4px 0}.personnel-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}.personnel-charts .card{margin:0;padding:12px}.personnel-charts h3{font-size:14px;margin:0}
@media(max-width:1450px){.personnel-metrics{grid-template-columns:repeat(3,minmax(130px,1fr)) minmax(190px,1.3fr) minmax(190px,1.3fr)}.personnel-layout{grid-template-columns:minmax(0,1fr) 270px}.personnel-metrics .kpi{padding:10px;gap:6px}.personnel-metrics .symbol{width:32px;height:32px}}
</style>
