# SPIKE 3 for Linux

[English](README.md) | 繁體中文

把官方 Windows 版 **LEGO Education SPIKE 3.6.1** 搬到 Linux。

官方 Windows 版是「網頁 app ＋ 一層 Windows 原生外殼」（WPF + CefSharp 瀏覽器 + 本機 Web 伺服器）。網頁本體跨平台，只有外殼是 Windows 專用。本專案：

- `host/extract.py`：從官方 MSI 裡把網頁 app 原封不動抽出來（藏在 1.3 GB 的 .NET DLL 資源裡）。
- `host/spike_host.py`：用 Python 重寫原生外殼（照反編譯的 C# 行為做），網頁 app 不改一行。
- `electron/`：視窗用 Electron（內建 Chromium），不需要系統瀏覽器；Python host 由 Electron 啟動與關閉。

## 安裝

### 套件（.deb / .rpm）

到 [Releases](https://github.com/duckducktw/spike-linux/releases) 下載：

```bash
sudo apt install ./spike3-linux_*_amd64.deb      # Ubuntu 24.04+ / Debian 13+
sudo dnf install ./spike3-linux_*_x86_64.rpm     # Fedora 41+
spike3
```

第一次開啟會從 LEGO 官方下載安裝檔（約 1.4 GB）、驗證 SHA-256 後解出網頁 app。套件本身不含任何 LEGO 檔案。USB 連 Hub 已附 udev 規則，不用加 dialout 群組。

裝完選單有「LEGO Education SPIKE」，或執行 `spike3`。`.llsp3` / `.llsp` 檔可雙擊開啟。

### 從原始碼打包

```bash
sudo apt install msitools rpm python3-pip nodejs npm
packaging/build.sh          # -> dist/*.deb dist/*.rpm
```

開發時直接跑：`cd electron && npm ci && SPIKE_APP_ROOT=<解出的 app 目錄> npx electron .`

### 發新版

推一個 `v*` tag，GitHub Actions 會跑測試、打包 .deb 和 .rpm、在乾淨的 Ubuntu / Debian / Fedora 容器裡各裝一次，然後自動發 Release。

## 支援狀況

| 功能 | 狀態 |
|---|---|
| 首頁、課程、選 Prime / Essential | ✅ 實測 |
| Python 編輯器（Monaco）、積木區 | ✅ 實測 |
| 專案新增 / 自動儲存 / 從 My Projects 重開 / 雙擊 .llsp3 開啟 | ✅ 實測 |
| 選單列（檔案 / 編輯 / 檢視 / 說明） | ✅ 實測（Electron 原生選單；另存、搬移在專案內才可按） |
| 另存、匯出、刪除、搬資料夾 | 已實作，未實測 |
| USB 連 Hub | ✅ 用假序列埠實測：web app 自動偵測、連線、雙向收發 SPIKE 封包；尚未接真 Hub |
| 藍牙 BLE 連 Hub | 已實作；BlueZ 掃描實測正常，尚未連真 Hub |
| 線上內容版本檢查 | ✅（`lego.cn` 網域解析不到，已自動改走 `.com`） |
| 韌體更新、錄音 | 已實作，未實測 |
| 傳統藍牙（BTC，舊版 Hub 韌體） | ❌ 未實作 |
| 遊戲手把 | ❌ 未實作 |

## 檔案位置

- 設定 / 紀錄：`~/.config/SPIKE3/`（`log.txt`）
- 下載的課程內容：`~/.local/share/SPIKE3/`
- 專案：`~/Documents/LEGO Education SPIKE/`

## 測試

```bash
.venv/bin/python tests/test_host_security.py     # 其他網站／本機程式無法遙控 host、另存只能寫使用者選的路徑
.venv/bin/python tests/test_concurrent_save.py   # 同時存檔不掉專案
.venv/bin/python tests/test_extract_traversal.py # 被動過手腳的 MSI 不能寫出目錄外
.venv/bin/python tests/test_menubar.py           # 選單列：真瀏覽器、真點擊
.venv/bin/python tests/test_rpc_serial.py        # 用假的序列埠模擬 Hub，測 USB 通道全程
packaging/test-pkg-in-docker.sh                  # 在乾淨容器裡裝套件、開 Electron、檢查積木區（見檔頭用法）
```

## 授權

程式碼採 MIT（見 `LICENSE`）。本 repo 只含相容層程式碼。SPIKE 網頁 app 與內容屬 LEGO Group，由使用者自行從官方 MSI 抽取。
