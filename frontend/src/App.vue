<template>
  <router-view />
</template>

<script setup>
// 使用 Vue Router 来管理页面
</script>

<style>
/* 全局样式重置 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: 'JetBrains Mono', 'Space Grotesk', 'Noto Sans SC', monospace;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #000000;
  background-color: #ffffff;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #333333;
}

/* Kontras: abu #999 di atas putih hanya 2.85:1; #6b6b6b = 5.3:1 (AA).
   Hanya warna, tetap global (juga berlaku di non-viewer). */
#app .app-header .step-num {
  color: #6b6b6b;
}

/* Header ponsel: tiga baris rapi (brand + bahasa / step + status / pilihan layout). */
@media (max-width: 768px) {
  .is-viewer #app .app-header {
    height: auto;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "brand lang"
      "step status"
      "switch switch";
    align-items: center;
    gap: 8px 12px;
    padding: 8px 12px;
  }
  .is-viewer #app .app-header .header-left {
    grid-area: brand;
    min-width: 0;
  }
  .is-viewer #app .app-header .brand {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 15px;
  }
  .is-viewer #app .app-header .header-right {
    display: contents;
  }
  .is-viewer #app .app-header .header-right > .language-switcher {
    grid-area: lang;
    justify-self: end;
  }
  .is-viewer #app .app-header .step-divider {
    display: none;
  }
  .is-viewer #app .app-header .workflow-step {
    grid-area: step;
    flex-wrap: wrap;
    gap: 2px 8px;
    font-size: 13px;
    min-width: 0;
  }
  .is-viewer #app .app-header .status-indicator {
    grid-area: status;
    justify-self: end;
    text-align: right;
    white-space: normal;
  }
  .is-viewer #app .app-header .header-center {
    grid-area: switch;
    position: static;
    transform: none;
  }
  .is-viewer #app .app-header .view-switcher {
    width: 100%;
  }
  .is-viewer #app .app-header .switch-btn {
    flex: 1 1 0;
    padding: 8px 4px;
    min-height: 44px;
  }
}

/* Desktop viewer: pilihan layout ikut alur flex (bukan absolut) supaya tak menabrak pemilih bahasa
   yang lebih lebar ("Bahasa Indonesia"). */
@media (min-width: 769px) {
  .is-viewer #app .app-header .header-center {
    position: static;
    transform: none;
  }
}

/* 全局按钮样式 */
button {
  font-family: inherit;
}
</style>
