# CLI-Downloader Plugin 开发说明

`Plugins/` 是项目开发版插件目录。每个插件一个文件夹，启动时会自动扫描并注册；同 ID 的项目插件会覆盖已安装版本。

## 目录结构

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

外部插件的网站实现、HTTP client、parser、fixtures 与测试都必须放在插件目录内。Core
只保留通用契约、加载器、生命周期与 orchestration；Core 测试不得直接 import 某个网站的
parser、adapter 或 fixture。插件若使用 sibling package，入口加载时会以插件版本目录为
import root，让同一套程序也能从项目 `Plugins/` 或 LocalAppData 的安装版本运行。

插件测试可在项目根目录独立执行，例如 `<plugin-id>`：

```powershell
$env:PYTHONPATH = "Plugins/<plugin-id>"
python -m unittest discover -s Plugins/<plugin-id>/tests -t .
```

测试必须默认 mock 网络；需要 live-site 的检查不得成为 Core 或插件的 deterministic release
gate。

正式安装版本位于：

```text
%LOCALAPPDATA%/CLI-Downloader/plugins/<plugin_id>/<version>/
```

版本目录旁可放置 `active` 纯文本档，内容为启用版本名称；没有 `active` 时使用最高版本。

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

`plugin_id` 使用小写英数字、`-` 或 `_`。`description` 用于在 Shell 插件列表中显示简介。入口文件必须在插件根目录，Manifest 必须在 `manifests/`。

可选的 `requires_python` 与 `requires_core_api` 使用版本条件（例如 `>=3.11` 或
`>=0.1.0`）。`requirements.txt` 只描述运行期依赖；Plugin Manager 安装或更新时会把它们
放入该版本自己的 `deps/`，不会在插件 discovery/import 时执行 pip，也不会修改 Core 环境。
`requirements-dev.txt` 仅供插件作者本机测试与 lint，不会被产品安装流程带入。

## Manifest

Manifest 声明域名、URL 规则、操作参数、设置、秘密字段与输出标签。Runtime 会先验证 JSON，再加载 Python。

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

插件依赖应使用可重现的版本范围或锁定版本，避免未预期的上游更新；需要哈希完整性时可在
requirements 档使用 pip 支持的 `--hash` 行。安装失败会保留原本的 active 版本，并在 Runtime
diagnostics 显示原因。

设置会注册为 `plugins.<plugin_id>.<key>`。密钥只通过 secret reference 传递，不应放进结果、日志或错误消息。

需要网站登录时，优先在 Manifest 注册系统管理的浏览器登录，而不是要求
用户手动复制 Cookie：

```json
"browser_auth": {
  "login_url": "https://accounts.example.com/login",
  "cookie_domains": ["example.com"],
  "required_cookies": ["session_id"]
}
```

Core 会建立 `auth.session` secret、开启隔离登录窗口，并只保存允许域名的
Cookie Jar。`required_cookies` 只判断登录完成，不限制实际保存的 Cookie。
Plugin 可在 `configure()` 的 `secrets["auth.session"]` 取得固定 JSON Session，
并使用 `cookie_header_for_url()` 依请求 URL 安全产生 Cookie Header。

`output.fields` 只能列出插件放在 `ResolvedItem.metadata` 的标量字段。系统会自动提供 `site_id`、`gallery_id`、`title`、`task_id`，并注册可选的 `plugins.<plugin_id>.download.folder_template` 覆盖。插件不可返回实际路径；核心会验证模板、清理文件名并把有效模板与字段快照保存到任务。

## Plugin 类别

插件必须提供以下接口：

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

- `classify_url` 返回 core `RouteMatch`，内容必须与 Manifest 规则一致。
- `execute` 接收 core `OperationRequest`，返回 core `OperationResult`。
- `resolve` 将已确认下载的 URL 转为 `ResolvedItem`。
- `list_assets` 返回完整 `AssetList`。
- `choose_strategy` 返回 `DownloadStrategyPlan`。
- ListPage、Gallery、Widget、Action 与 Enum 一律使用 `cli_downloader.core` 的通用对象。
- 插件不可建立输出路径、任务或下载队列；这些由系统管理。

插件若有设置或秘密字段，可选择加入同步设置入口。`secrets` 只包含此插件命名空间内的实际值，不会进入 OperationResult 或日志：

```python
def configure(self, settings, secrets):
    self._timeout = settings["http.timeout_seconds"]
    self._session = secrets.get("session")
```

Runtime 的 Batch 重试会重新调用 `resolve` 与 `choose_strategy`，并用稳定的 Gallery/Asset 识别数据比对缺少项目。建议在 `ResolvedItem.metadata` 放入 `gallery_id`，在 `Asset.metadata` 放入稳定的 `asset_id`、`image_id`、`page_id` 或 `index`。

## 网络节流

有实际网络请求的插件应在 `manifests/site.json` 声明安全默认：

```json
"network": {
  "max_concurrency": 2,
  "min_interval_seconds": 1.0,
  "retryable_status_codes": [429],
  "max_attempts": 5,
  "max_backoff_seconds": 300.0
}
```

系统会将数值设置注册成 `plugins.<plugin_id>.network.*` 覆盖项目，并让同一网站的解析与取档共享 request gate。插件不需自行重试 429；保留原始 `HTTPError` 的 status 与 headers，系统才能遵守 `Retry-After` 并保存冷却状态。

## 自定义取档

一般 HTTP/HTTPS Asset 不需要额外代码，系统会直接下载。需要 Cookie、签名、刷新 URL 或特殊协议时，可选择实现：

```python
async def fetch(self, asset):
    return b"file content"
```

`fetch` 必须返回 `bytes`。验证、重试、文件命名与写入仍由系统负责。
