# ticketGangster

KKTIX 自動搶票工具。可設定場次 / 開賣時間 / 票價，自動倒數、刷新、選票、送出，停在付款頁讓使用者刷卡。

## 功能

- ✅ 任意 KKTIX 場次（只要丟活動頁 URL）
- ✅ 自訂開賣時間（精準到秒）+ 伺服器時間自動校準
- ✅ 指定主票價 + 多個備援票價
- ✅ 自動進入報名頁、自動選票、自動勾同意、自動送出
- ✅ 偵測到付款頁時停手（鈴聲提醒），交給使用者刷卡
- ✅ 持久化登入 session（只需登入一次）
- ✅ 多分頁並行衝刺（可選）

---

## 安裝

需求：Python 3.10+ 和 Linux/macOS（Windows 也能跑，但本說明以 Linux 為例）。

```bash
cd ~/ticketGangster

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# 安裝 Playwright 用的 Chromium（會下載約 150MB）
playwright install chromium
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
4. **開賣前 `attack_lead_ms` 毫秒**開始嘗試進入報名頁，每 `refresh_interval_ms` 毫秒重試一次
5. 進到報名頁後立刻：選票價 → 設定數量 → 勾同意 → 送出
6. 偵測到付款頁時 **嗶嗶嗶** 停手，瀏覽器保留開啟，請立刻刷信用卡

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
| 進入報名頁 | ✅ |  |
| 選票價 + 數量 | ✅ |  |
| 勾選同意條款 | ✅ |  |
| 填姓名 / 手機 | ✅ (從 KKTIX 預填資料帶入) |  |
| 送出表單 | ✅ |  |
| reCAPTCHA | ⚠️ | 平常 KKTIX 不會跳，預熱過 Cloudflare 通常沒事 |
| 信用卡資料 | ❌ | **使用者接手** |
| 3D 驗證 | ❌ | **使用者接手** |

---

## 故障排除

### 「找不到任何票種 select」
- 確認真的進到 `/registrations/new`（看瀏覽器網址列）
- 不同主辦單位 HTML 結構可能略有差異，可以開瀏覽器 devtools 看 `<select>` 用什麼 class，回報後我再調 selector

### 「找不到指定票價」
- KKTIX 顯示的價格可能含逗號（"NT$4,500"）— 程式已處理
- 確認價格寫的是純數字（4500，不是 "4,500" 或 "$4500"）
- 設 `fallback_prices` 多放幾個

### Cloudflare 一直擋
- 用 `login.py` 開的瀏覽器先逛幾個 KKTIX 頁面，建立信任
- 確認 profile 路徑跟 grab.py 用的是同一個

### 時間同步顯示 offset 很大（>1 秒）
- 這是正常的，本機時鐘可能有漂移；程式會用校準後的時間倒數
- 但如果差到 >10 秒，建議系統執行 `sudo timedatectl set-ntp true`

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
