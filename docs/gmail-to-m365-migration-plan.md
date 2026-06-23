# Gmail API → Microsoft 365 (Microsoft Graph) 改版計畫

## 1. 概述 (Overview)

本計畫將 OWNDAYS EOD Report Processor 的「讀信層」從 **Gmail API** 遷移至 **Microsoft 365 / Microsoft Graph**。整體架構與設計哲學維持不變：

**維持不變的部分：**
- 仍是 **單一中央信箱 (single central mailbox)** 模式 — job 只認證到一個信箱，四家店的寄件者 (`sender_email`) 僅作為過濾條件。
- 仍是 **procedural pipeline**（無 class、無 retry 邏輯）。`未讀信件 = retry queue` 模型不變 — 僅在 JSON 寫入 + SFTP 上傳成功後才標記已讀。
- `STORE_MAP`（sender → store code）邏輯完全不動。
- 下游模組 `claude_parser.py`、`json_writer.py`、`ftp_uploader.py` **不需修改**（前提：保留回傳 dict 結構，見 §3、§4）。
- Config 仍透過 `config.py` 的 `dotenv_values()` 讀取（不使用 `os.environ`）。

**改變的部分（只動讀信模組）：**
- `src/gmail_reader.py` → 改寫為新模組（建議命名 `src/graph_reader.py`，見 §4）。
- 認證從 **delegated OAuth + 互動式瀏覽器首登 + cached refresh token**，改為 **app-only client-credentials flow（無瀏覽器、無使用者、無 refresh token）**。
- 移除 Google SDK 依賴，改用 `msal` + `requests`。
- 移除 Gmail 特有的 **urlsafe base64 → standard base64** 轉換（Graph 的 `contentBytes` 已是 standard base64）。
- 移除遞迴 multipart body 解析，改用單一 `Prefer: outlook.body-content-type="text"` header。

**核心收益：** 排程不再依賴會失效的 refresh token，也不需任何首登瀏覽器同意；body 解析與 base64 轉換大幅簡化。

---

## 2. 認證模型決策 (Auth Model Decision)

### 推薦流程：App-only / OAuth 2.0 Client Credentials Grant（憑證式）

Gmail 的「首次互動同意，之後永久用 cached refresh token」模式 **無法乾淨對應到 M365，且不應重現**。delegated refresh token 會因密碼變更、Conditional Access / MFA 政策變更、token lifetime 政策、以及 **90 天閒置** 而被撤銷，導致排程靜默失效並需人工重新互動登入。

**決策：採用 app-only client-credentials flow，並以 certificate 作為憑證（production）。** 這是 Microsoft 對「無使用者登入的背景服務 / daemon」的官方標準模式 — 每次執行都完全非互動，access token 約 60–90 分鐘效期，MSAL 自動在記憶體快取並靜默重取，沒有任何「會過期的 refresh token」。

> 備援方案：若租戶政策禁止對 mail 授予 Application permissions，才退而使用 device code flow（delegated + cached refresh token）。**ROPC（帳密直送）一律禁用**（與 MFA 不相容、儲存明文密碼，Microsoft 明確不建議）。

### Entra App Registration 步驟（一次性，由租戶管理員執行）

在 Microsoft Entra admin center：

1. **Microsoft Entra ID → App registrations → New registration**，命名如 `OWNDAYS-EOD-Mail-Reader`。
2. **Supported account types：** "Accounts in this organizational directory only"（單租戶）。
3. **Redirect URI 留空**（daemon 無 redirect）。
4. 註冊後從 Overview 複製 **Application (client) ID** 與 **Directory (tenant) ID**。
5. **API permissions** → 移除預設的 delegated `User.Read` → **Add a permission → Microsoft Graph → Application permissions** → 加入 **`Mail.ReadWrite`**（App role GUID `e2a3a72e-5f79-4c64-b1b1-878b674786c9`）。
6. **Grant admin consent for {tenant}** → Yes，確認每項權限顯示 "Granted for {tenant}"（daemon 無使用者可互動同意，必須管理員預先授權）。
7. **Certificates & secrets** → 上傳 **certificate**（`.cer`/`.crt` 公鑰），private key 留在 job 主機（嚴格 ACL）或 vault。

### Graph Application Permission（精確權限）

| 操作 | Graph 呼叫 | 所需 Application permission |
|---|---|---|
| 列出未讀 / 日期範圍信件 | `GET /users/{id}/messages` | `Mail.ReadWrite` |
| 取得信件 body + 附件 | `GET /users/{id}/messages/{id}`、`/attachments` | `Mail.ReadWrite` |
| 標記已讀 / 加 category | `PATCH /users/{id}/messages/{id}` | **`Mail.ReadWrite`（唯一可寫選項）** |

**只需授予 `Mail.ReadWrite`（Application）一項即可。** 重點：`Mail.ReadBasic.All` / `Mail.Read` 無法寫入（無法設 `isRead`），且 `Mail.ReadBasic.All` 還會排除 body 與附件 — 都不足以滿足本 job。**不要** 授予 `Mail.Send`（本 job 從不寄信）。

### 安全關鍵：將存取範圍限縮到「單一信箱」

> **預設情況下，被授予 Application `Mail.ReadWrite` 的 app 可讀寫租戶內每一個信箱。** 這相對 Gmail 單帳號 token 是重大權限擴張，必須限縮。

**推薦：Exchange Online 的 RBAC for Applications（現行模型）。** Application Access Policy 已是 **legacy 且已預告將棄用**，新建置應採用 RBAC for Applications。在 `Connect-ExchangeOnline`（需 Organization Management / Exchange Administrator）執行：

```powershell
# 1. 對應 app 的 Entra service principal（用 Enterprise applications 的 ID，非 App registrations）
New-ServicePrincipal -AppId <client-id> -ObjectId <enterprise-app-object-id> -DisplayName "OWNDAYS EOD Mail Reader"

# 2. 範圍 = 中央信箱
New-ManagementScope -Name "EOD Central Mailbox" `
  -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'eod@yourtenant.onmicrosoft.com'"

# 3. 指派 scoped 角色（標記已讀需要 ReadWrite）
New-ManagementRoleAssignment -App <enterprise-app-object-id> `
  -Role "Application Mail.ReadWrite" -CustomResourceScope "EOD Central Mailbox"

# 4. 驗證（此 cmdlet 繞過權限快取）
Test-ServicePrincipalAuthorization -Identity <enterprise-app-object-id> -Resource eod@yourtenant.onmicrosoft.com | Format-Table
```

> **致命陷阱（必讀）：** Entra consent 與 Exchange RBAC 是 **UNION（相加且獨立）**。若你在 Entra 留下未限縮的 org-wide `Mail.ReadWrite` 授權，**org-wide 授權會勝出，RBAC 限縮形同無效**。Microsoft 的設計意圖是：採用 RBAC 時，Entra 端仍需「宣告並 admin-consent」該權限（讓 `.default` token 內含此 app role），但 **不要** 在 Entra 另外留下未限縮的同名授權；真正的 mailbox 邊界由 Exchange RBAC 強制執行。務必用 `Test-ServicePrincipalAuthorization` 驗證實際效果。
>
> **快取注意：** RBAC 變更需 **30 分鐘至 2 小時** 才生效；驗證請用上述 test cmdlet（繞過快取）。
>
> ✅ **本專案已實測（2026-06）：** 建立 scoped RBAC 後移除 Entra org-wide consent，token 的 `roles` claim 變成 `None`，但 app 仍能讀取 in-scope 中央信箱（smoke test 全通）。證實 RBAC for Applications 單獨即可授權 Graph mail 存取、且限縮生效，無需保留 Entra app-role consent。腳本見 `scripts/scope-mailbox-rbac.ps1`。

> Legacy 替代（不建議用於新建置）：`New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId <含該信箱的 mail-enabled security group> -AccessRight RestrictAccess`。它直接限縮 Entra 授權（與 RBAC 的 union 行為不同），目前仍可用但已預告棄用。

### 釐清：`owndaysburwood@gmail.com` 是「寄件者」，不是要讀取的信箱（非阻擋項）

`STORE_MAP` 的 key 是**寄件者地址 (sender)**，不是 app 登入讀取的信箱。流程為：四家店各自把 EOD 報表**寄到中央信箱** → app 只登入該中央信箱 → 以 `From` 比對 `STORE_MAP` 取得 store code。

因此 `owndaysburwood@gmail.com` 只代表「Burwood 店用 gmail 帳號寄出報表」。**app 從不需要登入這個 gmail 信箱。** 遷到 M365 後：

- 中央信箱照樣收到這封信，寄件者網域是 gmail 或 owndays 完全無關。
- Graph filter `from/emailAddress/address eq 'owndaysburwood@gmail.com'` 一樣能比對。
- **不需 forward、不需遷移此 gmail 帳號、無任何阻擋。** 四個寄件者地址（含 gmail）全部僅為過濾值。

### 真正的硬性需求：中央信箱必須是 Exchange Online 信箱

唯一與信箱有關的真實前提是：**那個「中央信箱」必須是（或遷成）租戶內的 M365 / Exchange Online 信箱，且四家店的信都已落入它**——app-only Graph (`GET /users/{mailbox}/messages`) 只能存取本租戶 Exchange Online 信箱。寄件者用什麼網域不影響。👉 需與用戶確認中央信箱身分（見 §8）。

### Certificate vs Client Secret

- **Certificate（production 推薦）：** private key 不離開主機，proof-of-possession 簽章使冒用難度遠高於靜態密碼。可較長效期並重疊輪替。
- **Client secret（僅 dev）：** 字串型、**最長 24 個月且必定過期**，到期後 job 會靜默死掉直到輪替。
- **Federated / Managed Identity（最安全、無儲存密鑰）：** 僅在 job 跑在 Azure compute 時可用；本 job 透過 `run_eod.bat`/cron 在 on-prem 執行，通常不適用。
- **本案結論：** on-prem cron 主機使用 **certificate**，private key 存於 repo 外、嚴格檔案權限，path/thumbprint 由 `.env` 參照。設行事曆提醒於到期前輪替。

---

## 3. API 對應表 (API Mapping Table)

Base URL：`https://graph.microsoft.com/v1.0`。app-only token 無 `/me`，所有路徑用 `/users/{mailbox}/...`，`{mailbox}` = 中央信箱 UPN（如 `eod@owndays.com.au`）。所有請求帶 `Authorization: Bearer {token}`。

| 現行 Gmail（`gmail_reader.py`） | Microsoft Graph 替代 |
|---|---|
| `get_gmail_service()`（delegated OAuth、瀏覽器首登、cached refresh token） | 建立 MSAL `ConfidentialClientApplication`，`acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])` 取得 bearer token；回傳 token（或帶 auth header 的 `requests.Session`）。Application permission `Mail.ReadWrite` + admin consent，**無互動**。 |
| `fetch_unread_emails(service)`：`is:unread has:attachment` | `GET /users/{mailbox}/messages?$filter=isRead eq false and hasAttachments eq true&$top=50&$select=id,subject,from,receivedDateTime,sentDateTime,hasAttachments,body`（不加 `$orderby` 以避開 InefficientFilter）。可限縮資料夾：`/users/{mailbox}/mailFolders/inbox/messages`。 |
| `fetch_emails_in_date_range(service, after, before, senders)`：`after:YYYY/MM/DD before:YYYY/MM/DD has:attachment (from:a OR ...)`（after 含、before 不含） | `$filter=receivedDateTime ge {after}T00:00:00Z and receivedDateTime lt {before}T00:00:00Z and hasAttachments eq true and (from/emailAddress/address eq 'a' or from/emailAddress/address eq 'b' ...)`。`ge` 對應 after 含、`lt` 對應 before 不含 — 語意精準保留。日期格式從 `YYYY/MM/DD` 改 ISO 8601 UTC。 |
| `_fetch_emails` 的 `nextPageToken` 分頁 | **`@odata.nextLink`**（絕對 URL，已內含所有 query 參數）。迴圈 `while url:`，逐頁取 `response["value"]`，`url = response.get("@odata.nextLink")`，**原樣使用、勿再附加參數**。若有送 `Prefer` header，每次 nextLink 都要重送。 |
| `_extract_text_body` 遞迴 multipart 解析（偏好 text/plain） | **完全移除**。在 message GET 加 header `Prefer: outlook.body-content-type="text"`，回應 `body.content` 即為純文字（`body.contentType == "text"`）。單一 header 取代整段遞迴邏輯。 |
| `From` / `email.utils.parseaddr`、`Subject`、`Date` | `from.emailAddress.address`（→ `sender_email`）、`from.emailAddress.name`（→ `sender_name`，**name/address 已預先拆分，不需 `parseaddr`**）、`subject`、`sentDateTime` 或 `receivedDateTime`（ISO 8601 UTC，取前 10 字元為 `send_date`）。 |
| `_extract_pdf_attachments` + **urlsafe→standard base64 轉換** | `GET /users/{mailbox}/messages/{id}/attachments?$select=id,name,contentType,size,isInline,contentBytes`。篩 `@odata.type == "#microsoft.graph.fileAttachment"` 且 `contentType == "application/pdf"`（或 name 結尾 `.pdf`）。**`contentBytes` 已是 standard RFC 4648 base64，直接傳給 AI API，轉換步驟完全刪除。** 附件是 navigation property，需獨立呼叫此端點（plain message GET 不含附件）。 |
| `mark_as_read`（移除 UNREAD + 加 EOD_Processed label，需查/快取 label id） | 單一 `PATCH /users/{mailbox}/messages/{id}`，body `{"isRead": true, "categories": ["EOD_Processed"]}`。**一次 round-trip，不需 label-id 查詢/快取**。category 不需預先建立。 |

**附件大小說明（修正研究中的誇大）：** ~3 MB 是 **建立/上傳** 附件的門檻，**並非** READ 時 `contentBytes` 被抽掉的門檻。READ 時 `GET .../attachments` 不論大小都會回傳 `contentBytes`（base64），大檔僅是效能考量。EOD banking PDF 通常遠小於 3 MB，走簡單路徑即可。若遇大檔，備援為先取 metadata（`$select` 排除 `contentBytes`）再用 `GET .../attachments/{attId}/$value` 取 raw bytes 後自行 `base64.b64encode`。

---

## 4. 程式碼改動清單 (Code Change Checklist)

### 4.1 `src/gmail_reader.py` → 新模組 `src/graph_reader.py`

**建議重新命名模組為 `graph_reader.py`**（語意正確；舊檔可刪除）。保留 **相同的 public function 名稱與回傳 dict 結構**，使 `main.py` 改動最小。

回傳 dict 結構維持不變（下游模組才不用改）：
```python
{
    "message_id":   str,   # ← msg["id"]（Graph opaque id）
    "sender_name":  str,   # ← msg["from"]["emailAddress"]["name"]
    "sender_email": str,   # ← msg["from"]["emailAddress"]["address"]
    "subject":      str,   # ← msg["subject"]
    "send_date":    str,   # ← msg["sentDateTime"][:10]，YYYY-MM-DD（UTC，見 §6）
    "body":         str,   # ← msg["body"]["content"]（Prefer text header）
    "attachments":  [{"filename": str, "data_base64": str}],  # contentBytes 直用
}
```

公開函式對應（**簽名保留，僅把 `service` 換成 `token`**；mailbox 由模組內讀 `config.GRAPH_MAILBOX`）：

| 舊 | 新 |
|---|---|
| `get_gmail_service() -> service` | `get_graph_token() -> str`（回傳 bearer token；MSAL app 與 token 用 module-level 快取） |
| `fetch_unread_emails(service)` | `fetch_unread_emails(token)` |
| `fetch_emails_in_date_range(service, after, before, senders)` | `fetch_emails_in_date_range(token, after, before, senders)` |
| `_fetch_emails(service, query)` | `_fetch_emails(token, odata_filter)`（內部呼叫 `_list_messages` + per-message `_fetch_pdf_attachments`，組裝相同 dict） |
| `mark_as_read(service, message_id)` | `mark_as_read(token, message_id)` |

**可刪除的程式碼/import：**
- `import base64`、`import email.utils`、`import os`（不再需要）。
- `_decode_body`、`_extract_text_body`（multipart 遞迴）整段刪除。
- `_get_eod_processed_label_id` 與 module-level `_eod_processed_label_id` 快取刪除（category 不需 id 查詢）。
- 所有 `base64.urlsafe_b64decode` / `base64.b64encode` 轉換刪除。
- Google import（`google.auth.*`、`google_auth_oauthlib`、`googleapiclient`）全部刪除。

**新增 helper（procedural、同步）：**
- `_graph_get(token, url)`：`requests.get` + `raise_for_status`，回傳 `.json()`。
- `_list_messages(token, mailbox, odata_filter)`：組 URL + `$select` + `$top=50` + `Prefer: outlook.body-content-type="text"`，迴圈跟 `@odata.nextLink`。
- `_fetch_pdf_attachments(token, mailbox, message_id)`：取附件、篩 PDF、回傳 `[{"filename","data_base64"}]`。
- 建議補上 **429/Retry-After 與 5xx 的最小重試**（見 §6）— 因 Gmail SDK 內建退避，改 raw `requests` 後需自行處理。

### 4.2 `src/config.py`

**移除：**
```python
GMAIL_CREDENTIALS_FILE = str(_PROJECT_ROOT / _env.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))
GMAIL_TOKEN_FILE = str(_PROJECT_ROOT / _env.get("GMAIL_TOKEN_FILE", "gmail_token.json"))
```

**新增：**
```python
# Microsoft Graph (app-only / client-credentials)
GRAPH_TENANT_ID       = _env.get("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID       = _env.get("GRAPH_CLIENT_ID")
GRAPH_MAILBOX         = _env.get("GRAPH_MAILBOX")          # 中央信箱 UPN/SMTP
# 二擇一憑證（production 用 cert，dev 用 secret）
GRAPH_CERT_THUMBPRINT = _env.get("GRAPH_CERT_THUMBPRINT")
GRAPH_CERT_KEY_FILE   = str(_PROJECT_ROOT / _env["GRAPH_CERT_KEY_FILE"]) if _env.get("GRAPH_CERT_KEY_FILE") else None
GRAPH_CLIENT_SECRET   = _env.get("GRAPH_CLIENT_SECRET")    # dev only
```

**驗證區塊：** 將 `GRAPH_TENANT_ID`、`GRAPH_CLIENT_ID`、`GRAPH_MAILBOX` 加入 `_required`，並加一條檢查：`GRAPH_CLIENT_SECRET` 或（`GRAPH_CERT_THUMBPRINT` + `GRAPH_CERT_KEY_FILE`）至少一組存在，否則 `raise RuntimeError`。

### 4.3 `.env.example`

移除 `GMAIL_CREDENTIALS_FILE`、`GMAIL_TOKEN_FILE`。新增（見 §5 完整版）。`STORE_MAP` 不變。

### 4.4 `requirements.txt`

**移除：**
```
google-auth
google-auth-oauthlib
google-auth-httplib2
google-api-python-client
```
**新增：**
```
msal>=1.37.0
requests>=2.32.0
```
> 採 **`msal` + `requests`（同步）**，**不採** 官方 `msgraph-sdk`（async-by-default，會逼整條 pipeline 包 `asyncio.run`，對本 procedural 程式是侵入式大改）。`anthropic`、`openai`、`python-dotenv`、`paramiko` 不變。MSAL 自 1.23 起 `acquire_token_for_client` 內建記憶體快取。

### 4.5 `src/main.py`

- 改 import：`import gmail_reader` → `import graph_reader`。
- 改 3 處呼叫點：`service = gmail_reader.get_gmail_service()` → `token = graph_reader.get_graph_token()`；其餘把 `service` 換成 `token`（變數改名為 `token` 為 cosmetic，呼叫結構不變）。
- retry 模型不變：未讀信件仍是 retry queue；`mark_as_read`（現為 PATCH `isRead=true` + category）僅在 JSON 寫入 + SFTP 上傳成功後執行。
- 因 dict 結構保留，`claude_parser` / `json_writer` / `ftp_uploader` 無需改動（前提：parser 不依賴特定的 plain-text 換行格式 — 見 §8 待確認）。

### 4.6 其他

- `credentials.json`、`gmail_token.json` 不再需要（可從專案與 `.gitignore` 說明移除）。憑證 private key 檔（cert）改放 repo 外，path 由 `.env` 參照並 gitignore。

---

## 5. 設定與密鑰 (Config & Secrets)

### 新 `.env` keys

```dotenv
# Microsoft Graph (app-only / client-credentials)
GRAPH_TENANT_ID=00000000-0000-0000-0000-000000000000
GRAPH_CLIENT_ID=00000000-0000-0000-0000-000000000000
GRAPH_MAILBOX=eod@owndays.com.au

# Production：certificate（private key 檔存於 repo 外，嚴格 ACL）
GRAPH_CERT_THUMBPRINT=
GRAPH_CERT_KEY_FILE=

# Dev only：client secret（最長 24 個月、必定過期）
GRAPH_CLIENT_SECRET=
```

移除 `GMAIL_CREDENTIALS_FILE`、`GMAIL_TOKEN_FILE`。`STORE_MAP`（sender→store code）不變。

### MSAL 認證程式骨架（取代 `get_gmail_service`）

```python
import msal

_msal_app = None

def get_graph_token() -> str:
    global _msal_app
    if _msal_app is None:
        if config.GRAPH_CERT_THUMBPRINT and config.GRAPH_CERT_KEY_FILE:
            cred = {"thumbprint": config.GRAPH_CERT_THUMBPRINT,
                    "private_key": open(config.GRAPH_CERT_KEY_FILE).read()}
        else:
            cred = config.GRAPH_CLIENT_SECRET  # dev only
        _msal_app = msal.ConfidentialClientApplication(
            client_id=config.GRAPH_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{config.GRAPH_TENANT_ID}",
            client_credential=cred,
        )
    result = _msal_app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"])  # 唯一合法 scope
    if "access_token" not in result:
        raise RuntimeError(
            f"Graph token acquisition failed: "
            f"{result.get('error')}: {result.get('error_description')}")
    return result["access_token"]
```

### Token 快取 vs `gmail_token.json` 的差異

- **無 `gmail_token.json`、無 `InstalledAppFlow`、無 `run_local_server`、無瀏覽器** — 這些概念全部消失。
- **無持久化檔案需求：** MSAL 自 1.23 起 `acquire_token_for_client` 自動在 **記憶體** 快取 app token，cache miss 才打 Entra。批次 job 每次 run 重取一次 token 即可（token TTL ~1 小時）。
- **無 refresh token：** 不存在「90 天閒置失效 / CA 政策變更撤銷」問題。唯一會過期的是 **憑證本身**（cert 或 secret），需以行事曆提醒輪替。
- 唯一的本機密鑰是 cert private key 檔（或 dev 的 secret 字串於 `.env`），不再有自動產生的 token 檔。

---

## 6. 風險與注意事項 (Risks & Caveats)

1. **租戶層級存取風險（最高優先）：** `Mail.ReadWrite`（Application）預設可讀寫全租戶信箱。**必須** 用 §2 的 RBAC for Applications 限縮到單一信箱，且注意 **Entra 與 RBAC 為 union** — 採 RBAC 時不可在 Entra 留未限縮的同名授權，否則限縮無效。部署後務必跑 `Test-ServicePrincipalAuthorization` 驗證。

2. **憑證/密鑰到期：** cert 與 secret 都會過期（secret 上限 24 個月），到期後 job 靜默失效。設提醒、以重疊方式輪替。

3. **`$filter` / `$search` 限制：**
   - `$filter` + `$orderby` 共用時，**`$orderby` 的屬性必須也在 `$filter` 中、且順序一致並排在僅 filter 屬性之前**，否則回 `InefficientFilter`（"restriction or sort order too complex"）。**本 job 不需排序，故 §3 的查詢一律不加 `$orderby` 以避開此陷阱。**
   - 多 sender 的 `or`-group 與 `and` 混用時，Exchange filter 引擎較嚴格，較易觸發 `InefficientFilter`。**備援：** 只在 server 端 filter `isRead`/`hasAttachments`/`receivedDateTime`，sender 端只有 4 個地址，改在 Python 端比對 `STORE_MAP`；或每個 sender 各發一次查詢再合併。
   - **不要** 期待用 `ConsistencyLevel: eventual` + `$count=true` 解套 — advanced query 僅適用於 Entra/directory 物件，**不適用於 mail messages**。
   - `$search` 結果固定按 `receivedDateTime` 降序、不支援精確 `ge/lt` 半開區間，故日期視窗用 `$filter` 較合適。
   - 已知問題：不支援的 query 參數組合可能 **靜默失敗** 而非報錯 — 驗證時要確認 filter 實際生效。

4. **大附件抓取：** 見 §3 修正 — READ 不論大小都回 `contentBytes`，僅大檔有效能考量。EOD PDF 通常 < 3 MB 走簡單路徑；保留 `$value` raw-bytes 備援給超大日。**不要** 依賴 `$expand=attachments` 抓大檔（可能不 inline 展開）。

5. **中央信箱身分（非寄件者）：** 真正前提是「中央信箱」必須是租戶內 Exchange Online 信箱，且四家店信件都落入它（見 §2）。`owndaysburwood@gmail.com` 等寄件者地址的網域**不影響**——它們只是 `From` 過濾值，**非阻擋項**。

6. **`send_date` 時區語意改變：** Gmail 原本用 `Date` header（寄件者本地時區）轉 `YYYY-MM-DD`；Graph 的 `sentDateTime` / `receivedDateTime` 是 **UTC ISO 8601**。對澳洲（UTC+10/+11）的傍晚 EOD 報表，UTC 切日可能落到 **前一天**，影響 JSON 檔名日期與業務歸日。**建議在組 `send_date` 前先把 UTC 轉成澳洲時區（如 `Australia/Sydney`）再取前 10 字元**，以保留與 Gmail 原行為一致的「在地日期」語意。此為行為差異，需與用戶確認（見 §8）。

7. **Rate limiting / throttling（429）：** Gmail SDK 內建退避；改 raw `requests` 後需自行處理。Graph 超量回 **HTTP 429** 並帶 **`Retry-After`** header（秒數）。在 `_graph_get` / `mark_as_read` 加最小重試：遇 429 或 5xx 時依 `Retry-After` sleep 後重試數次。注意這是唯一新增的「retry 邏輯」，與 pipeline 的「未讀=retry queue」模型不衝突（屬 transport 層）。

8. **Body 為 HTML（若未加 Prefer header）：** Graph 預設回 HTML body。**必須** 在 message GET 送 `Prefer: outlook.body-content-type="text"` 才得純文字。若用 list 呼叫一併取 body，該 header 也要送；跟 `@odata.nextLink` 時每頁重送。

---

## 7. 分階段實施步驟 (Phased Rollout)

**Phase 0 — 管理員前置（無程式碼）**
1. 註冊 Entra app、加 `Mail.ReadWrite`（Application）、grant admin consent、上傳 certificate（§2）。
2. 用 RBAC for Applications 限縮到中央信箱，`Test-ServicePrincipalAuthorization` 驗證（注意 30 分–2 小時快取）。
3. 確認/安排把四家店（特別是 Burwood gmail）的 EOD 報表都導入中央 M365 信箱。

**Phase 1 — 連線冒煙測試（不接 pipeline）**
4. 寫一支獨立小腳本：`get_graph_token()` 取 token → `GET /users/{mailbox}/messages?$top=1` → 確認 200 與 `value`。驗證憑證、權限、mailbox 範圍三者皆通。
5. 對一封已知測試信驗證 `Prefer: text` body、`from.emailAddress`、`contentBytes`（直接 `base64.b64decode` 看是否為合法 PDF header `%PDF`）。

**Phase 2 — 實作新模組**
6. 新增 `src/graph_reader.py`（§4.1），更新 `config.py`、`.env.example`、`requirements.txt`（§4.2–4.4）。`pip install -r requirements.txt`。
7. 撰寫對照測試：用同一封測試信，比對 `graph_reader` 與舊 `gmail_reader` 產出的 dict 結構與欄位（message_id 例外，因 id 不同）。確認 `send_date` 時區處理符合預期（§6.6）。

**Phase 3 — 平行驗證（parallel run，唯讀）**
8. 在不呼叫 `mark_as_read` 的前提下，用 `fetch_emails_in_date_range` 對 **過去已處理日期** 跑 backfill，將輸出 JSON 與舊系統當日輸出逐筆 diff（金額、交易筆數、檔名）。
9. 確認下游 `claude_parser` 對 Graph body / 附件解析結果與 Gmail 路徑一致。

**Phase 4 — 切換 main.py**
10. 改 `main.py` import 與呼叫點（§4.5）。在 staging / 測試信箱跑完整 pipeline（含 `mark_as_read` PATCH `isRead`+category、SFTP 上傳）。
11. 確認 category `EOD_Processed` 正確標記、已讀狀態正確、未讀=retry 行為正常（故意製造一次 SFTP 失敗，確認信件保持未讀）。

**Rollback 策略**
- 保留舊 `gmail_reader.py` 與 Gmail 憑證至少一個完整週期（不刪除、不撤 Gmail token）。
- `main.py` 可用一個 `.env` 開關（如 `MAIL_PROVIDER=graph|gmail`）在兩模組間切換 import，遇問題即時切回 Gmail。
- 待 Graph 路徑連續穩定運行（建議 1–2 週、涵蓋多日多店）後，才移除 Google 依賴與舊模組。

---

## 8. 待澄清問題 (Open Questions for the User)

- ~~**中央信箱身分（關鍵前提）：** 中央 M365 信箱的 UPN/SMTP 是什麼？四家店信件是否都已落入？~~ ✅ **已釐清：收件信箱地址不變，僅從 Google 搬到 M365 託管。** `GRAPH_MAILBOX` 填原地址；四家店寄件行為不變，「都已落入」自動成立。前置作業：一次性把該信箱從 Google Workspace 遷移到 Exchange Online（含 MX/網域，IT/管理員作業，與程式無關）。
- ~~**憑證 vs secret：** production 用 certificate 還是先用 client secret？~~ ✅ **已定案：開發/初期驗證用 client secret，production 用 certificate。** 程式同時支援兩條路徑（有 cert 設定走 cert，否則 fallback secret），dev→prod 僅改 `.env`。待確認：憑證輪替負責人與到期提醒機制。
- ~~**`send_date` 時區：**~~ ✅ **已定案：轉成 `Australia/Sydney`。** 讀到 UTC `sentDateTime` 後先轉澳洲時區再取 `YYYY-MM-DD`，維持與 Gmail 原本「在地日期」一致（影響 dedup key 與 backfill 日期區間）。時區值以 `config.REPORT_TIMEZONE` 提供，預設 `Australia/Sydney`。
- ~~**已處理標記方式：**~~ ✅ **已定案：沿用 category `EOD_Processed`（純標籤、信留在 inbox）。** 等同現行行為，`PATCH {isRead:true, categories:["EOD_Processed"]}` 一次完成。
- **資料夾範圍：** 查詢限定 `inbox` 即可，還是需含子資料夾 / 其他資料夾？（暫以全信箱 `/messages` 實作，與 Gmail 現行不限資料夾的行為一致；如需限 inbox 再加 `/mailFolders/inbox`。）
- ~~**模組命名：**~~ ✅ **已定案：`gmail_reader.py` → `graph_reader.py`**，`main.py` 同步改 import。
- **租戶政策：** 租戶是否允許對 mail 授予 Application permissions？若被禁，是否接受 device-code delegated 備援（較不穩定）？
- ✅ **店家識別在「群組轉發」下會失效（2026-06 實測發現 → 已實作 fallback）：** 四家店 EOD 現經由通訊清單 `au.owndays.eod.report@bluebellgroup.com` 轉發進中央信箱。多數情況原店家 `From` 會保留（Sydney/Chatswood/Burwood 正常對應 `STORE_MAP`），但對受 DMARC 保護的網域（如 Hurstville `@owndays.com`），清單會把 `From` **改寫成群組地址** → `STORE_MAP` 找不到 → 該店資料被 skip 漏掉（已在 6/22 Hurstville 觀察到；6/20–6/21 同店卻正常，行為不一致，疑與進行中的 M365 migration pilot 有關）。
  - 實測結論：(a) **PDF 內容無店家識別**（`BankingTransactionReport`/`PaymentDetail` 皆為店家無關的交易表，排除「從 PDF 判定店家」）；(b) **email body 命名店家不一致**（Sydney/Chatswood/Hurstville 的 body 有店名，**Burwood 的 body 無店名**）。
  - 建議方案（分層 fallback）：主用 `From → STORE_MAP`；當寄件者為群組地址時，再用 **body / From display-name 的店名關鍵字**判定（Hurstville body 穩定含 "Owndays Hurstville"）。Burwood 因 gmail `From` 永不被改寫，其 body 無店名不影響。
  - ✅ **已實作（分層 fallback）：** `config.resolve_store_code(sender_email, sender_name, body)` — 先 `From → STORE_MAP`（不分大小寫），找不到時用 `STORE_NAME_MAP` 關鍵字比對 From 顯示名、再比對 body；同一文字若命中多店則視為 ambiguous 不猜、回 `None`（skip + 告警）。backfill 另以 `STORE_FORWARDER_ADDRESSES` 放寬寄件者過濾以納入被改寫的信。新增 `.env` 設定 `STORE_NAME_MAP`、`STORE_FORWARDER_ADDRESSES`。並修掉 email 地址大小寫比對 bug（sender filter 與 STORE_MAP 查找皆改為不分大小寫）。實測 6/22 Hurstville(被改寫)正確解析為 OWND03、4 店全通。
  - ⏳ **仍待 IT 確認：** 群組轉發＋From 改寫是否為固定終態（影響是否需把更多 forwarder 地址或店名別名加入設定）。
