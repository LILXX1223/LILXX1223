<script setup>
import { onMounted, ref } from 'vue'

// Hints are optional and deliberately conservative; unavailable values do not
// turn animation off on an otherwise capable device.
const lowPower = ref(false)
onMounted(() => {
  const cores = navigator.hardwareConcurrency
  const memory = navigator.deviceMemory
  lowPower.value = (Number.isFinite(cores) && cores <= 2) ||
    (Number.isFinite(memory) && memory <= 2) || navigator.connection?.saveData === true
})

const particles = [
  ['9%', '20%', '0s', '5px'], ['19%', '67%', '-2s', '6px'],
  ['30%', '36%', '-6s', '4px'], ['41%', '78%', '-4s', '6px'],
  ['53%', '18%', '-7s', '4px'], ['67%', '56%', '-1s', '5px'],
  ['76%', '25%', '-8s', '6px'], ['86%', '72%', '-5s', '4px'],
  ['94%', '38%', '-3s', '5px'],
]
</script>

<template>
  <div class="login-atmosphere" :class="{'is-low-power': lowPower}" aria-hidden="true">
    <div class="login-atmosphere__halo login-atmosphere__halo--left"></div>
    <div class="login-atmosphere__halo login-atmosphere__halo--right"></div>
    <svg class="login-atmosphere__waves" viewBox="0 0 1500 420" preserveAspectRatio="none" focusable="false">
      <defs><linearGradient id="login-wave-gradient" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#76bdf1" stop-opacity=".25"/><stop offset=".5" stop-color="#fff" stop-opacity=".7"/><stop offset="1" stop-color="#72b9ef" stop-opacity=".2"/></linearGradient></defs>
      <g class="login-atmosphere__wave login-atmosphere__wave--one">
        <path d="M-80 240 C200 25 420 370 760 180 S1220 100 1580 170" stroke="url(#login-wave-gradient)" stroke-width="3" fill="none"/>
        <path d="M-80 266 C210 46 440 389 780 200 S1200 112 1580 190" stroke="#a7dbf7" stroke-opacity=".17" stroke-width="25" fill="none"/>
        <path d="M-80 289 C220 80 440 410 780 230 S1220 128 1580 212" stroke="#fff" stroke-opacity=".5" stroke-width="2" fill="none"/>
      </g>
      <g class="login-atmosphere__wave login-atmosphere__wave--two">
        <path d="M-80 330 C250 148 550 420 880 282 S1280 159 1580 270" stroke="#fff" stroke-opacity=".5" stroke-width="2" fill="none"/>
        <path d="M-80 342 C270 160 560 432 890 302 S1280 169 1580 285" stroke="#8ac8ee" stroke-opacity=".15" stroke-width="18" fill="none"/>
      </g>
    </svg>
    <svg class="login-atmosphere__ecg" viewBox="0 0 360 80" preserveAspectRatio="xMidYMid meet" focusable="false">
      <defs>
        <linearGradient id="login-ecg-gradient" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#69aadc" stop-opacity="0" />
          <stop offset="0.22" stop-color="#69aadc" stop-opacity="0.5" />
          <stop offset="0.75" stop-color="#7dbce7" stop-opacity="0.46" />
          <stop offset="1" stop-color="#7dbce7" stop-opacity="0" />
        </linearGradient>
      </defs>
      <path class="login-atmosphere__ecg-line" pathLength="1"
        d="M0 40 H104 L118 40 L129 27 L141 52 L160 5 L180 76 L197 41 L216 41 L225 33 L236 48 L248 40 H360" />
    </svg>
    <i v-for="(particle, index) in particles" :key="index" class="login-atmosphere__particle"
      :style="{'--x': particle[0], '--y': particle[1], '--delay': particle[2], '--size': particle[3]}" />
  </div>
</template>

<style>
/* All overrides are restricted to the login page; the workbench keeps its theme. */
body:has(.login-page){min-width:0}
.login-page{
  position:relative;
  isolation:isolate;
  min-height:100vh;
  min-height:100svh;
  overflow:hidden;
  background:linear-gradient(118deg,#d6ebff 0%,#edf8ff 48%,#e6f4ff 75%,#d6ecff 100%);
}
.login-page>.login-brand,.login-page>.login-card{position:relative;z-index:1;min-width:0}
.login-page .login-brand{color:#1a70aa}
.login-page .login-brand h1{color:#153c63}
.login-page .login-brand p{color:#557a98}
.login-page .login-pills{flex-wrap:wrap}
.login-page .login-pills span{color:#386a91;background:rgba(255,255,255,.54);border-color:#c5dfef}
.login-page .login-card{
  isolation:isolate;
  background:rgba(255,255,255,.94);
  border:1px solid rgba(255,255,255,.95);
  box-shadow:0 18px 52px rgba(44,100,144,.13);
}
.login-page .login-card::before{
  content:'';position:absolute;inset:-1px;z-index:-1;border-radius:inherit;pointer-events:none;
  box-shadow:0 22px 68px rgba(57,123,174,.16);
  animation:login-card-breathe 10s ease-in-out infinite;
}
.login-page .login-card h2{color:#163d63}
.login-page .login-card .muted{color:#5e7489}
.login-page .login-card .el-input__inner::placeholder{color:#6b8094;opacity:1}
.login-atmosphere{position:absolute;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.login-atmosphere__halo{position:absolute;width:48vw;height:48vw;max-width:860px;max-height:860px;border-radius:50%;will-change:transform,opacity}
.login-atmosphere__halo--left{top:-25%;left:-11%;background:radial-gradient(circle,rgba(138,203,239,.28) 0%,rgba(174,221,245,.13) 40%,transparent 70%);animation:login-halo-left 10s ease-in-out infinite}
.login-atmosphere__halo--right{right:-14%;bottom:-35%;background:radial-gradient(circle,rgba(152,213,234,.25) 0%,rgba(197,232,248,.12) 42%,transparent 70%);animation:login-halo-right 10s ease-in-out infinite}
.login-atmosphere__waves{position:absolute;left:-8%;bottom:-12%;width:116%;height:45%;overflow:visible}
.login-atmosphere__wave{transform-origin:center;will-change:transform}
.login-atmosphere__wave--one{animation:login-wave-one 10s ease-in-out infinite}
.login-atmosphere__wave--two{animation:login-wave-two 10s ease-in-out infinite}
.login-atmosphere__ecg{position:absolute;left:43%;top:42%;width:23%;height:12%;opacity:.65}
.login-atmosphere__ecg-line{fill:none;stroke:url(#login-ecg-gradient);stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vector-effect:non-scaling-stroke;stroke-dasharray:1;stroke-dashoffset:1;animation:login-ecg-draw 10s linear infinite}
.login-atmosphere__particle{position:absolute;left:var(--x);top:var(--y);width:var(--size);height:var(--size);border-radius:50%;background:#87c6e9;opacity:.26;animation:login-particle-drift 10s ease-in-out var(--delay) infinite;will-change:transform,opacity}
@keyframes login-halo-left{0%,100%{transform:translate3d(0,0,0) scale(1);opacity:.8}50%{transform:translate3d(3%,3%,0) scale(1.04);opacity:1}}
@keyframes login-halo-right{0%,100%{transform:translate3d(0,0,0) scale(1);opacity:.8}50%{transform:translate3d(-3%,-3%,0) scale(1.03);opacity:1}}
@keyframes login-wave-one{0%,100%{transform:translate3d(-1%,0,0)}50%{transform:translate3d(2%,-3%,0)}}
@keyframes login-wave-two{0%,100%{transform:translate3d(2%,2%,0)}50%{transform:translate3d(-2%,-2%,0)}}
@keyframes login-particle-drift{0%,100%{transform:translate3d(0,0,0);opacity:.2}50%{transform:translate3d(6px,-10px,0);opacity:.45}}
@keyframes login-ecg-draw{0%,100%{stroke-dashoffset:1;opacity:0}10%{opacity:.7}65%{stroke-dashoffset:0;opacity:.7}87%{stroke-dashoffset:0;opacity:0}}
@keyframes login-card-breathe{0%,100%{opacity:.24}50%{opacity:.76}}
@media(max-width:820px){
  .login-page{grid-template-columns:minmax(0,1fr);align-content:center;gap:28px;padding:36px 6vw}
  .login-page .login-brand h1{font-size:clamp(25px,5.5vw,36px);margin:16px 0 10px}
  .login-page .login-brand p{margin-bottom:0}
  .login-page .login-pills{margin-top:18px;gap:8px}
  .login-page .login-card{width:min(100%,460px);padding:28px}
  .login-atmosphere__halo{width:85vw;height:85vw}
}
@media(max-width:400px){.login-page{padding:24px 18px}.login-page .login-card{padding:24px}.login-page .login-pills span{padding:6px 9px}}
@media(prefers-reduced-motion:reduce),(update:slow){
  .login-atmosphere__halo,.login-atmosphere__wave,.login-atmosphere__particle,.login-atmosphere__ecg-line,.login-page .login-card::before{animation:none!important;will-change:auto}
  .login-atmosphere__ecg-line{stroke-dashoffset:0;opacity:.45}
}
.login-atmosphere.is-low-power .login-atmosphere__halo,
.login-atmosphere.is-low-power .login-atmosphere__wave,
.login-atmosphere.is-low-power .login-atmosphere__particle,
.login-atmosphere.is-low-power .login-atmosphere__ecg-line,
.login-page:has(.login-atmosphere.is-low-power) .login-card::before{animation:none;will-change:auto}
.login-atmosphere.is-low-power .login-atmosphere__ecg-line{stroke-dashoffset:0;opacity:.45}
</style>
