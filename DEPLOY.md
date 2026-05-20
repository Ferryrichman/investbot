# Investbot 部署指南

## 架構總覽

```
stocks.ferryrichman.com          ← GitHub Pages (自動部署)
  /                              ← landing/index.html
  /caitech/                      ← caitech/index.html (港股財技)
  /caitech/holying/              ← caitech/holying/index.html (好L型 Dashboard)

investbot-tg-webhook.ferryrichman.workers.dev  ← Cloudflare Worker (TG Bot)

GitHub Actions (自動):
  - push main → deploy Pages
  - push tg_webhook/ → deploy Cloudflare Worker
  - cron 每日 09:00 HKT → 全面 alert report
  - cron 每15分鐘 09:15-16:00 HKT → intraday price alert
```

---

## 1. DNS 設定 (stocks.ferryrichman.com)

在你的 DNS 管理介面 (Cloudflare / Namecheap 等) 加：

| Type  | Name     | Value                          |
|-------|----------|--------------------------------|
| CNAME | stocks   | ferryrichman.github.io         |

> GitHub Pages 會自動讀取 `landing/CNAME` 裡面的 `stocks.ferryrichman.com`。

設定完之後去 GitHub repo → Settings → Pages：
- Source: **GitHub Actions**
- Custom domain: `stocks.ferryrichman.com`
- 勾選 **Enforce HTTPS**

---

## 2. 另一部電腦 Clone + 開發

```bash
git clone https://github.com/Ferryrichman/investbot.git
cd investbot
pip install requests truststore
```

### 本地預覽 (optional)
```bash
bash build.sh
cd dist && python -m http.server 8000
# 打開 http://localhost:8000
```

### 改完 push 即自動部署
```bash
git add -A && git commit -m "update" && git push
```

Pages workflow 會自動 run（觸發條件：`landing/**`, `caitech/**`, `build.sh` 有改動）。

---

## 3. GitHub Secrets (已設定)

Repo → Settings → Secrets and variables → Actions：

| Secret                 | 用途                      |
|------------------------|---------------------------|
| `TELEGRAM_TOKEN`       | TG Bot token              |
| `CHAT_ID`              | TG 目標 chat ID           |
| `CLOUDFLARE_API_TOKEN` | Cloudflare Workers deploy |
| `CLOUDFLARE_ACCOUNT_ID`| Cloudflare Account ID     |

---

## 4. Cloudflare Worker (TG Bot)

Worker secrets（Cloudflare Dashboard → Workers → investbot-tg-webhook → Settings → Variables）：

| Secret         | 值                            |
|----------------|-------------------------------|
| TELEGRAM_TOKEN | TG Bot token                  |
| CHAT_ID        | TG chat ID                    |
| GITHUB_TOKEN   | GitHub PAT (repo scope)       |
| GITHUB_REPO    | Ferryrichman/investbot        |

### 手動部署 Worker (如需)
```bash
cd tg_webhook
npx wrangler deploy
```

### 設定 TG Webhook
```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://investbot-tg-webhook.ferryrichman.workers.dev/webhook"
```

---

## 5. 每季 XLS 更新流程

1. 用 XLS 篩出新一批 L 形股
2. 用 TG Bot 管理持倉：
   - `/add 1234 main` — 加入監察
   - `/buy 1234 10000 0.05` — 買入
   - `/sell 1234 5000 0.10` — 賣出
   - `/modify 1234 8000 0.056` — 改持倉
   - `/del 1234` — 移除
   - `/list` — 列出全部
3. 每日 09:00 HKT 自動推送 alert
4. 每 15 分鐘 intraday price alert (交易時段)
5. Dashboard 即時同步：`stocks.ferryrichman.com/caitech/holying/`

---

## 6. 數據流

```
TG Bot /buy /sell /modify
        │
        ▼
GitHub state.json (data/watchlist_state.json)
        │
        ├──▶ hk_watchlist_monitor.py (GitHub Actions cron)
        │       更新 price / mcap / debt / signals
        │       寫回 state.json
        │
        └──▶ HTML Dashboard (fetch GitHub API)
                即時讀取 state.json 渲染
```

---

## 7. 常見問題

**Q: Push 之後 Pages 幾耐更新？**
A: 通常 1-2 分鐘。去 Actions tab 睇 "Deploy Pages on push" workflow。

**Q: GitHub Actions cron 冇 run？**
A: 正常，GitHub 不保證 cron 準時，可能延遲 5-30 分鐘，偶爾 skip。可以 workflow_dispatch 手動觸發。

**Q: TG Bot 冇回應？**
A: 檢查 Cloudflare Workers dashboard logs。確認 webhook URL 正確。

**Q: HTML 數據唔更新？**
A: GitHub API 有短暫 CDN cache (1-2 分鐘)。Hard refresh 頁面即可。
