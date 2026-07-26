# CLI-Downloader Plugin 開發說明

`Plugins/` 是專案開發版插件目錄。每個插件一個資料夾，啟動時會自動掃描並註冊；同 ID 的專案插件會覆蓋已安裝版本。

## 目錄結構

```text
Plugins/<plugin_id>/
|- plugin.json
|- plugin.py
|- requirements.txt          # optional runtime dependencies
|- requirements-dev.txt      # optional local test/lint dependencies
|- <plugin_package>/         # optional site-specific implementation package
|- tests/                     # optional plugin-owned tests and fixtures
`- manifests/
   `- site.json
```

外部插件的網站實作、HTTP client、parser、fixtures 與測試都必須放在插件目錄內。Core
只保留通用契約、載入器、生命週期與 orchestration；Core 測試不得直接 import 某個網站的
parser、adapter 或 fixture。插件若使用 sibling package，入口載入時會以插件版本目錄為
import root，讓同一套程式也能從專案 `Plugins/` 或 LocalAppData 的安裝版本執行。

插件測試可在專案根目錄獨立執行，例如 `<plugin-id>`：

```powershell
$env:PYTHONPATH = "Plugins/<plugin-id>"
python -m unittest discover -s Plugins/<plugin-id>/tests -t .
```

測試必須預設 mock 網路；需要 live-site 的檢查不得成為 Core 或插件的 deterministic release
gate。

正式安裝版本位於：

```text
%LOCALAPPDATA%/CLI-Downloader/plugins/<plugin_id>/<version>/
```

版本目錄旁可放置 `active` 純文字檔，內容為啟用版本名稱；沒有 `active` 時使用最高版本。

## plugin.json

```json
{
  "plugin_id": "example",
  "display_name": "Example",
  "version": "0.1.0",
  "description": "Example plugin description.",
  "entrypoint": "plugin.py:Plugin",
  "manifest": "site.json"
}
```

`plugin_id` 使用小寫英數字、`-` 或 `_`。`description` 用於在 Shell 外掛列表中顯示簡介。入口檔必須在插件根目錄，Manifest 必須在 `manifests/`。

可選的 `requires_python` 與 `requires_core_api` 使用版本條件（例如 `>=3.11` 或
`>=0.1.0`）。`requirements.txt` 只描述執行期依賴；Plugin Manager 安裝或更新時會把它們
放入該版本自己的 `deps/`，不會在插件 discovery/import 時執行 pip，也不會修改 Core 環境。
`requirements-dev.txt` 僅供插件作者本機測試與 lint，不會被產品安裝流程帶入。

## Manifest

Manifest 宣告網域、URL 規則、操作參數、設定、秘密欄位與輸出標籤。Runtime 會先驗證 JSON，再載入 Python。

```json
{
  "site_id": "example",
  "display_name": "Example",
  "domains": ["example.com"],
  "url_rules": [
    {
      "name": "gallery",
      "pattern": "^https://example\\.com/gallery/(?P<id>[a-z0-9]+)$",
      "input_kind": "single",
      "operation": "gallery"
    }
  ],
  "operations": {
    "gallery": {"params": {}}
  },
  "settings": {
    "quality": {
      "type": "string",
      "default": "original",
      "enum": ["original", "preview"]
    }
  },
  "output": {
    "fields": ["author_name", "artwork_id"],
    "default_folder_template": "{author_name}/{artwork_id}-{title}"
  },
  "secrets": {}
}
```

插件依賴應使用可重現的版本範圍或鎖定版本，避免未預期的上游更新；需要雜湊完整性時可在
requirements 檔使用 pip 支援的 `--hash` 行。安裝失敗會保留原本的 active 版本，並在 Runtime
diagnostics 顯示原因。

設定會註冊為 `plugins.<plugin_id>.<key>`。密鑰只透過 secret reference 傳遞，不應放進結果、紀錄或錯誤訊息。

需要網站登入時，優先在 Manifest 註冊系統管理的瀏覽器登入，而不是要求
使用者手動複製 Cookie：

```json
"browser_auth": {
  "login_url": "https://accounts.example.com/login",
  "cookie_domains": ["example.com"],
  "required_cookies": ["session_id"]
}
```

Core 會建立 `auth.session` secret、開啟隔離登入視窗，並只保存允許網域的
Cookie Jar。`required_cookies` 只判斷登入完成，不限制實際保存的 Cookie。
Plugin 可在 `configure()` 的 `secrets["auth.session"]` 取得固定 JSON Session，
並使用 `cookie_header_for_url()` 依請求 URL 安全產生 Cookie Header。

`output.fields` 只能列出插件放在 `ResolvedItem.metadata` 的純量欄位。系統會自動提供 `site_id`、`gallery_id`、`title`、`task_id`，並註冊可選的 `plugins.<plugin_id>.download.folder_template` 覆寫。插件不可回傳實際路徑；核心會驗證模板、清理檔名並把有效模板與欄位快照保存到任務。

## Plugin 類別

插件必須提供以下接口：

```python
class Plugin:
    site_id = "example"
    display_name = "Example"

    def get_manifest(self): ...
    def can_handle_url(self, url): ...
    def classify_url(self, url): ...

    async def execute(self, request): ...
    async def resolve(self, request): ...
    async def list_assets(self, item): ...
    async def choose_strategy(self, item): ...
```

- `classify_url` 回傳 core `RouteMatch`，內容必須與 Manifest 規則一致。
- `execute` 接收 core `OperationRequest`，回傳 core `OperationResult`。
- `resolve` 將已確認下載的 URL 轉為 `ResolvedItem`。
- `list_assets` 回傳完整 `AssetList`。
- `choose_strategy` 回傳 `DownloadStrategyPlan`。
- ListPage、Gallery、Widget、Action 與 Enum 一律使用 `cli_downloader.core` 的通用物件。
- 插件不可建立輸出路徑、任務或下載佇列；這些由系統管理。

插件若有設定或秘密欄位，可選擇加入同步設定入口。`secrets` 只包含此插件命名空間內的實際值，不會進入 OperationResult 或紀錄：

```python
def configure(self, settings, secrets):
    self._timeout = settings["http.timeout_seconds"]
    self._session = secrets.get("session")
```

Runtime 的 Batch 重試會重新呼叫 `resolve` 與 `choose_strategy`，並用穩定的 Gallery/Asset 識別資料比對缺少項目。建議在 `ResolvedItem.metadata` 放入 `gallery_id`，在 `Asset.metadata` 放入穩定的 `asset_id`、`image_id`、`page_id` 或 `index`。

## 網路節流

有實際網路請求的插件應在 `manifests/site.json` 宣告安全預設：

```json
"network": {
  "max_concurrency": 2,
  "min_interval_seconds": 1.0,
  "retryable_status_codes": [429],
  "max_attempts": 5,
  "max_backoff_seconds": 300.0
}
```

系統會將數值設定註冊成 `plugins.<plugin_id>.network.*` 覆寫項目，並讓同一網站的解析與取檔共用 request gate。插件不需自行重試 429；保留原始 `HTTPError` 的 status 與 headers，系統才能遵守 `Retry-After` 並保存冷卻狀態。

## 自訂取檔

一般 HTTP/HTTPS Asset 不需要額外程式碼，系統會直接下載。需要 Cookie、簽名、刷新 URL 或特殊協定時，可選擇實作：

```python
async def fetch(self, asset):
    return b"file content"
```

`fetch` 必須回傳 `bytes`。驗證、重試、檔案命名與寫入仍由系統負責。
