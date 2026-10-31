// LEGO Education SPIKE 3 for Linux - Electron shell.
// The window is this bundled Chromium; the native layer (USB/BLE hub bridge, project files,
// content) is the Python host, started as a child process and stopped when the app quits.
'use strict';
const { app, BrowserWindow, shell, Menu, session, dialog, ipcMain } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const http = require('http');

app.setName('LEGO Education SPIKE');  // shown in dialogs; package/dir name is spike3-linux
app.setDesktopName('lego-spike3.desktop');   // taskbar icon/title come from this desktop file
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling');

const RES = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
const HOST_DIR = path.join(RES, 'host');

function projectArgs(argv) {
  return argv.filter(a => /\.(llsp3|llsp)$/i.test(a)).map(a => path.resolve(a));
}

if (!app.requestSingleInstanceLock({ files: projectArgs(process.argv) })) {
  app.quit();
} else {
  let win = null;
  let host = null;
  let base = null;
  // per-run secret shared with the Python host; sent as an HttpOnly cookie on every request
  const TOKEN = crypto.randomBytes(32).toString('base64url');

  app.on('second-instance', (_ev, argv, _cwd, extra) => {
    const files = (extra && extra.files) || projectArgs(argv);
    if (files.length && base) {
      const req = http.request(base + '/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Spike-Token': TOKEN },
      });
      req.on('error', () => {});
      req.end(JSON.stringify(files));
    }
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  function python() {
    for (const p of [process.env.SPIKE_PYTHON, '/usr/bin/python3', '/usr/local/bin/python3']) {
      if (p && fs.existsSync(p)) return p;
    }
    return 'python3';
  }

  function startHost() {
    return new Promise((resolve, reject) => {
      const args = [path.join(HOST_DIR, 'spike3.py'), '--embedded', ...projectArgs(process.argv)];
      host = spawn(python(), args, { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, SPIKE_SESSION_TOKEN: TOKEN } });
      let buf = '';
      let ready = false;
      host.stdout.on('data', d => {
        buf += d.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 1);
          if (!ready && line.startsWith('READY ')) { ready = true; resolve(JSON.parse(line.slice(6))); }
          else process.stdout.write(line + '\n');
        }
      });
      host.on('exit', code => {
        if (!ready) reject(new Error(`host exited (${code}) before it was ready`));
        else if (!app.isQuitting) { dialog.showErrorBox('LEGO Education SPIKE', `背景服務意外結束（${code}）。請重新開啟。`); app.quit(); }
      });
      host.on('error', reject);
    });
  }

  // ---- native menu bar (File / Edit / View / Help), like the Windows app's WPF menu (UIMenuBuilder).
  // Labels come from the host's own translations; Save as / Move to / Share follow the web app's
  // /menu/showProjectItems + /menu/hideProjectItems, relayed by the page through the preload bridge.
  let menuLabels = {};
  let menuState = { initial: true, project: false };
  const L = (key, fallback) => (menuLabels[key] || fallback).replace('{{name}}', 'SPIKE').replace(/\.{3,}|…+/g, '…');

  function hostMenu(item) {   // same path as the Windows host: host broadcasts /menu/itemClicked to the web app
    if (!base) return;
    const req = http.request(`${base}/linux/menu?item=${encodeURIComponent(item)}`,
      { method: 'POST', headers: { 'X-Spike-Token': TOKEN } });
    req.on('error', () => {});
    req.end();
  }

  function buildMenu() {
    const p = menuState.project;
    const tpl = [
      { label: L('menu.file.title', 'File'), submenu: [
        { label: L('menu.file.new_project', 'New Project'), accelerator: 'CmdOrCtrl+Alt+N', click: () => hostMenu('newProject') },
        { label: L('menu.file.open', 'Open…'), accelerator: 'CmdOrCtrl+O', click: () => hostMenu('openProject') },
        { type: 'separator' },
        { label: L('menu.file.save_as', 'Save as…'), accelerator: 'CmdOrCtrl+Shift+S', enabled: p, click: () => hostMenu('saveAs') },
        { label: L('menu.file.move_to', 'Move to…'), enabled: p, click: () => hostMenu('moveTo') },
        { label: L('menu.file.share', 'Share…'), enabled: p, click: () => hostMenu('share') },
        { type: 'separator' },
        { label: L('menu.mac_app.quit', 'Quit'), accelerator: 'CmdOrCtrl+Q', role: 'quit' },
      ] },
      { label: L('menu.edit.title', 'Edit'), submenu: [
        { label: L('menu.edit.undo', 'Undo'), role: 'undo' },
        { label: L('menu.edit.redo', 'Redo'), role: 'redo' },
        { type: 'separator' },
        { label: L('menu.edit.cut', 'Cut'), role: 'cut' },
        { label: L('menu.edit.copy', 'Copy'), role: 'copy' },
        { label: L('menu.edit.paste', 'Paste'), role: 'paste' },
        { type: 'separator' },
        { label: L('menu.edit.select_all', 'Select All'), role: 'selectAll' },
      ] },
      { label: L('menu.view.title', 'View'), submenu: [
        { label: L('menu.view.reload', 'Refresh'), role: 'reload' },
        { type: 'separator' },
        { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'resetZoom' },
        { type: 'separator' },
        { label: L('menu.view.fullscreen', 'Enter Full Screen'), role: 'togglefullscreen' },
      ] },
      { label: L('menu.help.title', 'Help'), submenu: [
        { label: L('menu.help.item', '{{name}} App help…'), click: () => hostMenu('help') },
        { label: L('menu.settings', 'Settings'), click: () => hostMenu('settings') },
        { type: 'separator' },
        { label: L('menu.debug.toggledevtools', 'Toggle Developer Tools'), role: 'toggleDevTools' },
      ] },
    ];
    const menu = Menu.buildFromTemplate(tpl);
    Menu.setApplicationMenu(menu);
    if (process.env.SPIKE_TEST_MENU) {   // tests: dump the live menu (labels + enabled) to stdout
      const dump = menu.items.map(m => ({ label: m.label, items: m.submenu.items.filter(i => i.type !== 'separator')
        .map(i => ({ label: i.label, enabled: i.enabled, role: i.role || null })) }));
      process.stdout.write('MENU ' + JSON.stringify({ state: menuState, menu: dump }) + '\n');
    }
  }

  function createWindow(info) {
    win = new BrowserWindow({
      width: 1440, height: 900, minWidth: 1024, minHeight: 640,
      title: 'LEGO Education SPIKE',
      icon: path.join(RES, 'icon.png'),
      backgroundColor: '#ffffff',
      autoHideMenuBar: false,
      show: false,
      webPreferences: {
        contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false,
        preload: path.join(__dirname, 'preload.js'),
      },
    });
    buildMenu();
    win.once('ready-to-show', () => win.show());
    win.on('page-title-updated', e => e.preventDefault());

    // links that leave the app (help, store, privacy) open in the user's browser
    const origin = new URL(info.base).origin;
    const inApp = url => { try { return new URL(url).origin === origin; } catch (_) { return false; } };
    win.webContents.setWindowOpenHandler(({ url }) => {
      if (/^(https?|mailto):/.test(url)) shell.openExternal(url);
      return { action: 'deny' };
    });
    win.webContents.on('will-navigate', (e, url) => {
      if (!inApp(url)) { e.preventDefault(); if (/^(https?|mailto):/.test(url)) shell.openExternal(url); }
    });

    // F12 for devtools (the menu covers the rest of the accelerators)
    win.webContents.on('before-input-event', (e, input) => {
      if (input.type === 'keyDown' && input.key === 'F12') { win.webContents.toggleDevTools(); e.preventDefault(); }
    });

    // menu labels/state reported by our own page (whitelisted shape; ignored from any other frame)
    ipcMain.removeAllListeners('spike-menu');
    ipcMain.on('spike-menu', (ev, info) => {
      if (!win || ev.sender !== win.webContents || !inApp(ev.senderFrame.url) || !info || typeof info !== 'object') return;
      if (info.labels && typeof info.labels === 'object') {
        menuLabels = Object.fromEntries(Object.entries(info.labels)
          .filter(([k, v]) => /^menu\.[\w.]+$/.test(k) && typeof v === 'string' && v.length < 200));
      }
      if (info.state && typeof info.state === 'object') {
        menuState = { initial: !!info.state.initial, project: !!info.state.project };
      }
      buildMenu();
    });

    win.loadURL(info.url);
  }

  app.whenReady().then(async () => {
    // microphone for sound recording blocks; Web Serial/Bluetooth are not used (the host bridges hubs)
    session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(['media', 'clipboard-read', 'clipboard-sanitized-write', 'fullscreen'].includes(perm)));
    let info;
    try {
      info = await startHost();
    } catch (e) {
      dialog.showErrorBox('LEGO Education SPIKE', `無法啟動：${e.message}\n\n詳細紀錄：~/.config/SPIKE3/log.txt`);
      app.exit(1);
      return;
    }
    base = info.base;
    await session.defaultSession.cookies.set({
      url: info.base, name: 'spike_token', value: TOKEN, httpOnly: true, sameSite: 'strict', path: '/',
    });
    createWindow(info);
  });

  app.on('before-quit', () => { app.isQuitting = true; });
  app.on('window-all-closed', () => app.quit());
  app.on('will-quit', () => {
    if (host && host.exitCode === null) {
      host.stdin.end();                      // host exits when stdin closes
      setTimeout(() => { try { host.kill('SIGTERM'); } catch (_) {} }, 3000).unref();
    }
  });
}
