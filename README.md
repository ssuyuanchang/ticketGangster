# ticketGangster

KKTIX 自動搶票工具。可設定場次 / 開賣時間 / 票價，自動倒數、刷新、選票、送出，停在付款頁讓使用者刷卡。

## 功能

- ✅ 任意 KKTIX 場次（只要丟活動頁 URL）
- ✅ 自訂開賣時間（精準到秒）+ 伺服器時間自動校準
- ✅ 指定主票價 + 多個備援票價
- ✅ 自動進入報名頁、自動選票、自動勾同意、自動送出（電腦配位 / Best Available）
- ✅ **自動偵測售完並持續刷新等釋票**（不會像簡易腳本那樣搶不到就退出）
- ✅ **送出後若卡 queue 太久自動放棄並刷新**重新搶
- ✅ **隨機 jitter 刷新間隔**降低被 Cloudflare 標記成 bot 的風險
- ✅ 偵測到付款頁時停手（鈴聲提醒），交給使用者刷卡
- ✅ 持久化登入 session（只需登入一次）
- ✅ 多分頁並行衝刺（可選）
- ✅ Linux / macOS / Windows 全平台

---

## 安裝

需求：Python 3.10+，支援 Linux / macOS / Windows。

### Linux / macOS

```bash
cd ~/ticketGangster

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# 安裝 Playwright 用的 Chromium（會下載約 150MB）
playwright install chromium
```

### Windows (PowerShell)

```powershell
cd ~\Develop\ticketGangster

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# 安裝 Playwright 用的 Chromium（會下載約 300MB，含 headless shell）
python -m playwright install chromium
```

幾個與 Linux/macOS 不同的注意事項：

- **`source .venv/bin/activate` → `.\.venv\Scripts\Activate.ps1`**：Windows 啟動 venv 用 `Scripts\Activate.ps1`。venv 啟動成功時，prompt 開頭會出現 `(.venv)`。要關閉 venv 用 `deactivate`。
- **`python3` → `python`**：Windows 上 Python 3 通常就叫 `python`。直接打 `python3` 可能會啟動到 Microsoft Store 的轉址器。
- **`playwright install chromium` → `python -m playwright install chromium`**：因為 `playwright.exe` 安裝路徑（`...\Scripts\`）預設不在 PATH 上，用 `python -m playwright` 走當前 venv 最穩。
- **如果 `Activate.ps1` 跳「無法載入指令碼，因為這個系統上已停用指令碼執行」**：以系統管理員身分或一般使用者執行一次：
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```

---

## 使用流程

### 1. 設定 config

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

至少要改：
- `event.url` — 活動頁 URL
- `event.sale_start` — 開賣時間（台北時區，YYYY-MM-DD HH:MM:SS）
- `ticket.price` — 想搶的票價
- `ticket.quantity` — 張數（1~4，KKTIX 上限）

### 2. 登入 KKTIX（只需做一次）

```bash
python login.py
```

會開一個瀏覽器，請完成下列步驟：

1. **登入 KKTIX 帳號**
2. **驗證手機 + Email**（會員設定 → 個人資料）
   - 沒驗證手機的帳號很多熱門場次不能買
3. **預先填好「報名預填資料」**（會員設定 → 報名預填資料）
   - 至少存好「姓名」、「手機」
   - 開賣時系統會自動帶入這些欄位，省掉打字的時間
4. **（強烈推薦）先去任何一個目前還能買的測試場次走一次完整流程**
   - 讓 Cloudflare 把這個瀏覽器標記為「人類」，搶票時不會跳 CAPTCHA
   - Engelbert Humperdinck 那場 (`https://globalmusic.kktix.cc/events/5dee326c`) 可以拿來測試（買最便宜的 NT$1,250 一張，3 天內可退）

完成後關閉瀏覽器，session 會存到 `./profile/` 資料夾。

### 3. 預演（強烈建議）

開賣前一兩天，用測試場次先實跑一次，確認流程沒問題：

```yaml
# config.yaml 設成 Engelbert 場次
event:
  url: "https://globalmusic.kktix.cc/events/5dee326c"
  sale_start: "2026-05-08 14:00:00"   # 設成 5 分鐘後就好
ticket:
  price: 1250
  quantity: 1
```

```bash
python grab.py
```

確認程式能在開賣時間自動進入報名頁、選到正確票價、送出表單、停在付款頁。**測試完記得三天內退票**。

### 4. 正式搶票

開賣前 3~5 分鐘啟動：

```bash
python grab.py
```

程式會：

1. 校準伺服器時間
2. 倒數至 `prewarm_seconds` 秒前打開活動頁（建立 TLS / Cloudflare cookie）
3. 持續倒數
4. **開賣前 `attack_lead_ms` 毫秒**開始嘗試進入報名頁，每 `refresh_interval_ms` 毫秒（±jitter）重試一次
5. 進到報名頁後解析票種：
   - 目標票價可購買 → 選張數 → 勾同意 → 送出 (Best Available 電腦配位)
   - 目標票價售完 → **持續刷新報名頁**，等他人退票釋出
   - 頁面上根本沒這個票價 → 直接放棄（可能 config 寫錯）
6. 送出後等付款頁出現（最多 `payment_wait_timeout_seconds` 秒）：
   - 順利進付款頁 → **嗶嗶嗶** 停手，瀏覽器保留開啟，請立刻刷信用卡
   - 卡 queue / 網路問題逾時 → 放棄此次提交，刷新報名頁重新搶
7. 整個過程最多持續 `attack_duration_minutes` 分鐘，超時就放棄但保留瀏覽器

完成付款後在終端機按 `Ctrl+C` 結束。

---

## 設定詳解

### `event.sale_start`

台北時區，秒級精度。例如：
```yaml
sale_start: "2026-05-11 12:00:00"
```

> 程式會用 KKTIX 伺服器的 `Date` header 校準本地時鐘。如果你的機器時鐘有漂移（一般 PC 可能差個幾秒），仍能在伺服器時間 12:00:00 那一瞬間開搶。

### `ticket.price` 與 `fallback_prices`

```yaml
ticket:
  price: 4500
  fallback_prices: [4000, 3500]
```

主票價搶不到（解析報名頁時找不到該票種）會依序嘗試 fallback。建議先確認 KKTIX 活動頁列出的所有票價，再設定。

### `strategy.attack_lead_ms`

預設 200ms。代表**比開賣時間早 200ms 開始衝**。原因：
- 網路 RTT 大約 30~80ms
- KKTIX 伺服器釋放票的瞬間可能有 ±100ms 的不確定性
- 早一點點先送 request，可以搶在伺服器釋放票的那一刻

太早 (>2000ms) 也沒用，伺服器會直接拒絕。預設 200ms 是經驗值。

### `strategy.parallel_tabs`

預設 1。設成 2~3 在熱門場次能提高成功率，但每多一個分頁吃一份 CPU + 網路。建議：
- 一般場次：1
- 熱門場次（G-DRAGON 等級）：2 或 3
- 注意：同一個帳號送出兩筆訂單只有第一筆有效，多分頁主要是搶「進入報名頁」的成功率

### `strategy.use_server_time`

預設 `true`，建議保留。會在啟動時花 ~1 秒做時間同步。

### `strategy.refresh_interval_ms` 與 `strategy.refresh_jitter_pct`

每輪刷新的基礎間隔 + 隨機抖動百分比。實際每次 sleep 是 `interval × (1 ± pct/100)`，避免請求出現「精確 200ms 一次」的機械式規律，被 Cloudflare 標記成 bot。

| 場景 | `refresh_interval_ms` | `refresh_jitter_pct` |
|---|---|---|
| 一般場次（預設） | 200 | 20 |
| G-DRAGON / Mayday 等級熱門 | 250~350 | 20~30 |
| 想保守、長時間刷 | 400~500 | 20 |

- `refresh_interval_ms < 150`（每秒 6+ 次請求）容易觸發 CF 限流
- `refresh_jitter_pct = 0` 表示不抖動，請求完全規律 → 不建議
- `refresh_jitter_pct > 50` 抖動太誇張會偶爾出現 1 秒以上空檔，影響搶票

### `strategy.attack_duration_minutes`

預設 90 分鐘。從 attack 開始（開賣時間 - `attack_lead_ms`）之後，總共最多搶多久就放棄。

- 一般場次設 60 分鐘已夠
- 預設 90 分鐘適用大部分熱門場次（涵蓋第一波 + 退票釋出）
- 想徹夜守候極熱門場次可以調到 120~240
- **不要設太短**（< 5 分鐘），因為熱門場次第一波等候室可能就要排 2~3 分鐘

到時間就會結束 attack 迴圈、log 出『N 分鐘到，放棄』，但瀏覽器仍保留開啟讓你接手手動操作。

### `strategy.payment_wait_timeout_seconds`

預設 45 秒。**送出表單之後**，等付款頁出現的 timeout。超時代表卡在 KKTIX 等候室或網路有問題，這時程式會放棄這次提交、刷新報名頁重新搶。

- 一般場次：30 秒
- 通用建議：45 秒（預設）
- G-DRAGON / Mayday 等級極熱門：60 秒
- > 90 秒不建議，KKTIX submit 後正常 5~25 秒就會回應，等更久幾乎都是已經卡死

⚠️ **副作用**：如果 KKTIX 已經為你 reserve 了座位但只是付款頁載入慢，刷新會放棄這個座位。權衡的是「卡 2 分鐘風險」 vs 「持續搶其它釋出票的機會」，預設值偏向後者。

### 行為總覽：搶票過程的決策樹

進入報名頁後，每次解析票種會走以下其中一條：

| 狀況 | 行為 |
|---|---|
| 目標票價可購買 | 選張數 → 勾同意 → 送出 → 等付款頁 |
| 目標票價售完（頁面有但 ticket-quantity 沒 input） | **刷新報名頁繼續刷**，等釋票 |
| 頁面上根本沒這個價錢（config 寫錯） | 直接放棄，不浪費 deadline |
| 送出後 N 秒沒進付款頁 | 放棄這次提交，**刷新重來** |
| `attack_duration_minutes` 到 | 全部放棄，瀏覽器保留 |

「刷新」 = `page.goto(register_url)` 整頁重新導航，等同於 F5。

---

## 提高成功率的關鍵 Tips

1. **網路要好** — 用有線網路、靠近主要 ISP 的線路，RTT 越低越好
2. **時鐘要校準** — 預設已啟用，但作業系統時鐘也建議開 NTP（`timedatectl` / Settings）
3. **預先驗證手機** — 沒驗證手機的帳號 KKTIX 會擋很多熱門場次
4. **預填資料** — 在 KKTIX 會員設定裡存好姓名 / 手機，否則報名頁會多一段表單要填
5. **預熱 Cloudflare** — 用同一個 profile 提前去任何 KKTIX 頁面點一點，避免搶票瞬間跳 challenge
6. **不要關燈睡覺搶票** — 螢幕休眠會讓瀏覽器被降頻

---

## 限制 / 需要使用者接手的部分

| 步驟 | 自動 | 備註 |
|---|---|---|
| 登入 KKTIX | ❌ | 第一次手動登入，session 持久化 |
| 手機驗證 | ❌ | 帳號設定階段 |
| 倒數 + 開搶 | ✅ |  |
| 進入報名頁 | ✅ | URL 自動轉換到 `kktix.com` 主站 |
| 售完偵測 + 持續刷新等釋票 | ✅ |  |
| 選票價 + 數量 | ✅ | 用 AngularJS `+` 按鈕點擊 |
| 勾選同意條款 | ✅ |  |
| 填姓名 / 手機 | ✅ | 從 KKTIX 預填資料帶入 |
| 送出表單 | ✅ | 優先用「電腦配位 / Best Available」 |
| 送出後 queue 卡住 → 自動刷新 | ✅ | timeout 由 `payment_wait_timeout_seconds` 控 |
| reCAPTCHA | ⚠️ | 平常 KKTIX 不會跳，預熱過 Cloudflare 通常沒事；萬一觸發 visible challenge 必須手動 |
| 信用卡資料 | ❌ | **使用者接手** |
| 3D 驗證 | ❌ | **使用者接手** |

---

## 故障排除

### 「找不到指定票價 [...] (頁面看到: [...])」
代表 config 設的 `ticket.price` / `fallback_prices` 在這場頁面上根本不存在（不是售完、是不存在）。

- 看 log 末尾「頁面看到: [...]」這一段，那是頁面實際提供的價錢
- 對照 KKTIX 活動頁的票價，更新 config
- 注意 KKTIX 顯示的價格可能含逗號（"NT$4,500"）— 程式會幫你去掉，**config 寫純數字 4500 即可**

### 「目標票價 [...] 售完」一直刷
程式現在的設計就是售完會一直刷新等釋票。如果你想：
- 改搶其他票價：停掉程式（Ctrl+C），改 config 的 `fallback_prices` 或 `price`
- 設定「一直刷直到我手動關」：把 `attack_duration_minutes` 調很大（240 = 4 小時）
- 接受搶不到：等 deadline 到自動結束

### 「一直刷新但跳回 `https://kktix.com/` 主頁」
這個 bug 已修復（之前是 `derive_register_url` 把報名頁網址錯算到主辦單位子網域）。如果你還看到，請確認：
- `src/kktix.py` 裡的 `derive_register_url` 會把 host 換成 `kktix.com`（主站才有 `/registrations/new` 路由）
- 主辦單位子網域（例如 `globalmusic.kktix.cc/events/{slug}/registrations/new`）會被 KKTIX 直接 redirect 到主頁

### Cloudflare 一直擋 / 跳 reCAPTCHA
- 用 `login.py` 開的瀏覽器先逛幾個 KKTIX 頁面、買一張便宜的測試票，建立信任
- 確認 profile 路徑跟 grab.py 用的是同一個（都看 `browser.user_data_dir`）
- 如果開賣中跳 challenge，把 `refresh_interval_ms` 拉高到 350~500、`refresh_jitter_pct` 拉到 30
- `parallel_tabs` 設到 2 以上更容易被視為可疑流量，熱門場次再開

### 「送出後 N 秒仍未進付款頁」一直重試
- 正常情況：KKTIX queue 卡住，程式在自我保護地刷新重搶
- 如果連 5 次以上都這樣，可能：
  - 你的 `payment_wait_timeout_seconds` 太短（拉到 60 試試）
  - 觸發了 reCAPTCHA visible challenge（停掉程式手動操作一次釋放）

### 時間同步顯示 offset 很大（>1 秒）
- 這是正常的，本機時鐘可能有漂移；程式會用校準後的時間倒數
- 但如果差到 >10 秒，建議系統執行 `sudo timedatectl set-ntp true`（Linux/macOS）或 Windows 設定 → 時間 → 開「自動設定時間」

### Windows: 中文輸出變成亂碼（"Σ┐¥τòÖ" 之類）
- PowerShell 預設 cp1252，吃不下中文。每次跑前先設：
  ```powershell
  $env:PYTHONIOENCODING="utf-8"
  python grab.py
  ```
- 或永久設環境變數：`[Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8', 'User')`

---

## 專案結構

```
ticketGangster/
├── README.md
├── requirements.txt
├── config.example.yaml
├── login.py                    # 第一次手動登入，存 session
├── grab.py                     # 主搶票腳本
├── src/
│   ├── __init__.py
│   ├── config.py               # YAML 設定載入 + 驗證
│   ├── time_sync.py            # 伺服器時間校準
│   ├── kktix.py                # KKTIX 頁面操作邏輯
│   └── logger.py               # 帶毫秒時間戳的 log
├── profile/                    # (gitignore) Playwright 持久化資料
└── config.yaml                 # (gitignore) 你的設定
```

---

## 法律與使用提醒

- 本工具僅供個人購票自用，**嚴禁加價轉售**（違反《文化創意產業發展法》第 10-1 條，可處票面金額 10~50 倍罰鍰）
- 大量搶票可能違反平台 ToS，請自行判斷風險
- KKTIX 隨時可能更動 HTML 結構或加入反 bot 機制，本工具可能需要持續更新
