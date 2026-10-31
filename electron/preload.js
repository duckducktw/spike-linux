// Preload: one fixed channel so the page can tell the native menu bar its labels and
// which items are enabled. No Node APIs, no generic IPC.
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('spikeShell', {
  setMenu: (info) => ipcRenderer.send('spike-menu', {
    labels: info && info.labels,
    state: info && info.state,
  }),
});
