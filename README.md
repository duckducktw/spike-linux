# SPIKE 3 for Linux

把官方 Windows 版 **LEGO Education SPIKE 3.6.1** 搬到 Linux。

官方 Windows 版是「網頁 app ＋ 一層 Windows 原生外殼」（WPF + CefSharp 瀏覽器 + 本機 Web 伺服器）。網頁本體跨平台，只有外殼是 Windows 專用。本專案：

- `host/extract.py`：從官方 MSI 裡把網頁 app 原封不動抽出來（藏在 1.3 GB 的 .NET DLL 資源裡）。
- `host/spike_host.py`：用 Python 重寫原生外殼（照反編譯的 C# 行為做），網頁 app 不改一行。
- 視窗：用系統的 Chrome / Chromium / Edge 以 `--app` 模式開（無網址列）。

## 安裝

```bash
sudo apt install msitools python3-venv   # 加上 Chrome 或 Chromium
./install.sh                              # 自動下載官方 MSI；或 ./install.sh 路徑/SPIKE.msi
sudo usermod -aG dialout $USER            # USB 連 Hub 需要，做完重新登入
```

裝完選單有「LEGO Education SPIKE」，或執行 `spike3`。`.llsp3` / `.llsp` 檔可雙擊開啟。

## 支援狀況

| 功能 | 狀態 |
|---|---|
| 首頁、課程、選 Prime / Essential | ✅ 實測 |
| Python 編輯器（Monaco）、積木區 | ✅ 實測 |
| 專案新增 / 自動儲存 / 從 My Projects 重開 / 雙擊 .llsp3 開啟 | ✅ 實測 |
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
.venv/bin/python tests/test_rpc_serial.py   # 用假的序列埠模擬 Hub，測 USB 通道全程
```

## 授權

本 repo 只含相容層程式碼。SPIKE 網頁 app 與內容屬 LEGO Group，由使用者自行從官方 MSI 抽取。
