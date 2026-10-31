// LEGO Education SPIKE 3 for Linux - Electron shell.
// The window is this bundled Chromium; the native layer (USB/BLE hub bridge, project files,
// content) is the Python host, started as a child process and stopped when the app quits.
'use strict';
const { app, BrowserWindow, shell, Menu, session, dialog } = require('electron');
const { spawn } = require('child_process');
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

  app.on('second-instance', (_ev, argv, _cwd, extra) => {
    const files = (extra && extra.files) || projectArgs(argv);
    if (files.length && base) {
      const req = http.request(base + '/activate', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
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
      host = spawn(python(), args, { stdio: ['pipe', 'pipe', 'inherit'] });
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

  function createWindow(info) {
    win = new BrowserWindow({
      width: 1440, height: 900, minWidth: 1024, minHeight: 640,
      title: 'LEGO Education SPIKE',
      icon: path.join(RES, 'icon.png'),
      backgroundColor: '#ffffff',
      autoHideMenuBar: true,
      show: false,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false },
    });
    Menu.setApplicationMenu(null);
    win.once('ready-to-show', () => win.show());
    win.on('page-title-updated', e => e.preventDefault());

    // links that leave the app (help, store, privacy) open in the user's browser
    const inApp = url => url.startsWith(info.base);
    win.webContents.setWindowOpenHandler(({ url }) => {
      if (/^(https?|mailto):/.test(url)) shell.openExternal(url);
      return { action: 'deny' };
    });
    win.webContents.on('will-navigate', (e, url) => {
      if (!inApp(url)) { e.preventDefault(); if (/^(https?|mailto):/.test(url)) shell.openExternal(url); }
    });

    // keyboard: devtools / reload / zoom (no menu bar)
    win.webContents.on('before-input-event', (e, input) => {
      if (input.type !== 'keyDown') return;
      const c = input.control || input.meta;
      if (input.key === 'F12' || (c && input.shift && input.key.toLowerCase() === 'i')) { win.webContents.toggleDevTools(); e.preventDefault(); }
      else if (c && input.key.toLowerCase() === 'r') { win.webContents.reload(); e.preventDefault(); }
      else if (c && (input.key === '=' || input.key === '+')) { win.webContents.setZoomLevel(win.webContents.getZoomLevel() + 0.5); e.preventDefault(); }
      else if (c && input.key === '-') { win.webContents.setZoomLevel(win.webContents.getZoomLevel() - 0.5); e.preventDefault(); }
      else if (c && input.key === '0') { win.webContents.setZoomLevel(0); e.preventDefault(); }
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
