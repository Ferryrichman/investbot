/**
 * Cloudflare Worker — Telegram Bot Webhook
 * 接收 TG 訊息，解析 /buy /sell /zerocost /status 指令
 * 透過 GitHub API 更新 watchlist_state.json
 *
 * 環境變數 (在 Cloudflare Dashboard → Settings → Variables 設定):
 *   TELEGRAM_TOKEN  — Bot token from @BotFather
 *   CHAT_ID         — Your Telegram chat ID
 *   GITHUB_TOKEN    — GitHub Personal Access Token (repo scope)
 *   GITHUB_REPO     — e.g. "Ferryrichman/investbot"
 */

const STATE_PATH = "data/watchlist_state.json";
// MJ 半新股系統 — 獨立第二層 state (由 mj_import.py 匯入每日功課 PDF)
const MJ_STATE_PATH = "data/mj_state.json";
const MJ_MOM_FLOOR = 55;   // 動能下限
const BRANCH = "main";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    try {
      const body = await request.json();
      const msg = body.message;
      const chatId = (env.CHAT_ID || "").trim();
      if (!msg || !msg.text || String(msg.chat.id) !== chatId) {
        return new Response("OK", { status: 200 });
      }

      const text = msg.text.trim();
      const reply = await handleCommand(text, env);
      if (reply) {
        await sendTG(env.TELEGRAM_TOKEN.trim(), chatId, reply);
      }
      return new Response("OK", { status: 200 });
    } catch (err) {
      try {
        await sendTG(env.TELEGRAM_TOKEN.trim(), (env.CHAT_ID || "").trim(), `Error: ${err.message}`);
      } catch (_) {}
      return new Response("OK", { status: 200 });
    }
  },

  // Cloudflare Cron Trigger → 觸發 GitHub Actions workflow
  // GitHub dispatch 偶爾回 5xx (transient) — retry 3 次先至認輸
  async scheduled(event, env, ctx) {
    const mode = "alert";  // 10:00 + 15:45 都係 full alert
    let lastErr = null;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        await triggerGitHubWorkflow(mode, env);
        return;
      } catch (err) {
        lastErr = err;
        if (attempt < 3) {
          await new Promise((r) => setTimeout(r, attempt * 10000)); // 10s, 20s
        }
      }
    }
    // 3 次都失敗先通知
    try {
      await sendTG(
        env.TELEGRAM_TOKEN.trim(),
        (env.CHAT_ID || "").trim(),
        `⚠️ Cron trigger failed (3 attempts): ${lastErr.message}\n可以手動打 /push 補返`
      );
    } catch (_) {}
  },
};

async function handleCommand(text, env) {
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();

  if (cmd === "/buy") {
    if (parts.length < 4) return "用法: /buy CODE 股數 價錢\n例: /buy 1740 70000 0.114";
    const [, code, shares, price] = parts;
    const force = (parts[4] || "").toLowerCase() === "force";
    return await recordBuy(code, parseInt(shares), parseFloat(price), env, force);
  }

  if (cmd === "/sell") {
    if (parts.length < 4) return "用法: /sell CODE 股數 價錢\n例: /sell 1740 10000 0.20";
    const [, code, shares, price] = parts;
    const force = (parts[4] || "").toLowerCase() === "force";
    return await recordSell(code, parseInt(shares), parseFloat(price), env, force);
  }

  if (cmd === "/zerocost") {
    if (parts.length < 3) return "用法: /zerocost CODE 剩餘股數\n例: /zerocost 2370 13000";
    const [, code, remaining] = parts;
    return await markZeroCost(code, parseInt(remaining), env);
  }

  if (cmd === "/modify") {
    if (parts.length < 4) return "用法: /modify CODE 股數 平均價\n例: /modify 1740 70000 0.114";
    const [, code, shares, avgPrice] = parts;
    const force = (parts[4] || "").toLowerCase() === "force";
    return await modifyHolding(code, parseInt(shares), parseFloat(avgPrice), env, force);
  }

  if (cmd === "/del") {
    if (parts.length < 2) return "用法:\n/del CODE — 刪除持倉\n/del CODE watchlist — 刪除持倉+移除監察";
    const delWL = (parts[2] || "").toLowerCase();
    if (delWL === "watchlist" || delWL === "wl") {
      return await delAll(parts[1], env);
    }
    return await delHolding(parts[1], env);
  }

  if (cmd === "/add") {
    if (parts.length < 2) {
      return (
        "用法: /add CODE [main/gem] [mj]\n" +
        "例: /add 1234\n例: /add 8888 gem\n" +
        "例: /add 8657 mj — 標記 MJ倉策略 (板塊自動判斷)\n" +
        "例: /add 2625 main mj"
      );
    }
    const t2 = (parts[2] || "").toLowerCase();
    const t3 = (parts[3] || "").toLowerCase();
    const strategy = (t2 === "mj" || t3 === "mj") ? "mj" : null;
    let board;
    if (t2 === "gem") board = "gem";
    else if (t2 === "main") board = "main";
    else if (strategy === "mj") {
      // 冇明確指定板塊: 8 字頭 = 創業板
      board = String(parts[1]).padStart(4, "0").startsWith("8") ? "gem" : "main";
    } else board = "main";
    return await addToWatchlist(parts[1], board, env, strategy);
  }

  if (cmd === "/remove") {
    if (parts.length < 2) return "用法: /remove CODE\n例: /remove 1234";
    return await removeFromWatchlist(parts[1], env);
  }

  if (cmd === "/watchlist") {
    return await getWatchlist(env);
  }

  if (cmd === "/rules") {
    return (
      "📋 買入/賣出機制\n\n" +
      "【買入】每朝09:00自動檢查市值\n" +
      "主板觸發位(百萬HKD):\n" +
      "  2億→1.5億→1.2億→1億→8千萬→6千萬\n" +
      "創業板(0.4×):\n" +
      "  8千萬→6千萬→5千萬→4千萬→3千萬\n" +
      "每跌穿一層 → TG提醒買$6,000\n" +
      "高水位：觸發過嘅層唔會重複提醒\n\n" +
      "【賣出/0成本】\n" +
      "浮盈≥100% + 市值≥5億 或 浮盈≥300%\n" +
      "→ TG提醒賣出回收成本\n" +
      "→ 剩餘股數 = 免費持倉(0成本)\n\n" +
      "【清倉】\n" +
      "全部賣晒 → 記錄已實現盈虧\n" +
      "日後重新買入 → 歷史盈虧保留累計\n\n" +
      "💡 所有交易由你手動 /buy /sell 記帳" +
      "\n\n【📘 MJ倉 (輔助)】\n用 /add CODE mj 標記\n注碼 = L型一半 · 可向下撈但唔低過放棄位\n止賺: 主板5億/GEM2億 + 回報100% → 賣回本留免費股\n0成本後跟 MJ 位置2/3 警報自由操作"
    );
  }

  if (cmd === "/status") {
    const code = parts[1];
    return await getStatus(code, env);
  }

  if (cmd === "/mj") {
    return await getMj(parts[1], env);
  }

  if (cmd === "/push") {
    // 即時觸發 GitHub Actions 跑 alert
    try {
      await triggerGitHubWorkflow("alert", env);
      return "🚀 已觸發 alert workflow，~30 秒內收到推送";
    } catch (err) {
      return `⚠️ 觸發失敗: ${err.message}`;
    }
  }

  if (cmd === "/undo") {
    const force = (parts[1] || "").toLowerCase() === "force";
    return await undoLast(env, force);
  }

  if (cmd === "/help" || cmd === "/start") {
    return (
      "指令:\n" +
      "/buy CODE 股數 價錢 — 記錄買入\n" +
      "/sell CODE 股數 價錢 — 記錄賣出\n" +
      "/modify CODE 股數 平均價 — 修正持倉\n" +
      "/del CODE — 刪除持倉（保留監察）\n" +
      "/del CODE watchlist — 刪除持倉+移除監察\n" +
      "/add CODE [main/gem] [mj] — 加入監察 (mj=MJ倉策略)\n" +
      "/remove CODE — 移除監察\n" +
      "/watchlist — 睇監察清單\n" +
      "/rules — 睇買賣機制\n" +
      "/status CODE — 查看持倉\n" +
      "/status — 查看全部\n" +
      "/mj — MJ 半新股功課摘要 (要放棄/危險位置)\n" +
      "/mj CODE — 睇該股 MJ 全部數據\n" +
      "/push — 即時觸發 alert push\n" +
      "/undo — 退回上一個 TG 命令 (再 /undo = redo)\n" +
      "\n💡 賣出後自動偵測0成本\n" +
      "💡 價錢同市價差3倍會被擋 (防打錯位), 確認無誤加 force 落命令尾"
    );
  }

  return null;
}

// ── GitHub API helpers ──

async function getState(env) {
  const token = (env.GITHUB_TOKEN || "").trim();
  const repo = (env.GITHUB_REPO || "").trim();
  const url = `https://api.github.com/repos/${repo}/contents/${STATE_PATH}?ref=${BRANCH}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.v3+json",
      "User-Agent": "investbot-tg-worker",
    },
  });
  if (!res.ok) {
    throw new Error(`GitHub API ${res.status}: ${await res.text()}`);
  }
  const data = await res.json();
  const raw = atob(data.content.replace(/\n/g, ""));
  const content = decodeURIComponent(escape(raw));
  return { state: JSON.parse(content), sha: data.sha };
}

async function saveState(state, sha, message, env) {
  const token = (env.GITHUB_TOKEN || "").trim();
  const repo = (env.GITHUB_REPO || "").trim();
  const url = `https://api.github.com/repos/${repo}/contents/${STATE_PATH}`;
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(state, null, 2))));
  const res = await fetch(url, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.v3+json",
      "User-Agent": "investbot-tg-worker",
    },
    body: JSON.stringify({
      message,
      content,
      sha,
      branch: BRANCH,
    }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub save ${res.status}: ${body.slice(0, 200)}`);
  }
  return true;
}

// 退回上一個 TG 命令: 搵最近一個 "tg: " commit, 將 state 還原到佢 parent 版本
// undo 本身都係 "tg: " commit → 再打 /undo 會 undo 呢次 undo = redo
async function undoLast(env, force = false) {
  const token = (env.GITHUB_TOKEN || "").trim();
  const repo = (env.GITHUB_REPO || "").trim();
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github.v3+json",
    "User-Agent": "investbot-tg-worker",
  };

  // a. 攞最近 15 個 state 檔案 commit
  const listUrl = `https://api.github.com/repos/${repo}/commits?path=${STATE_PATH}&sha=${BRANCH}&per_page=15`;
  const listRes = await fetch(listUrl, { headers });
  if (!listRes.ok) {
    throw new Error(`GitHub API ${listRes.status}: ${await listRes.text()}`);
  }
  const commits = await listRes.json();

  // b. 搵最近(最新)一個 TG 命令 commit
  const idx = commits.findIndex(
    (c) => c.commit && c.commit.message && c.commit.message.startsWith("tg: ")
  );
  if (idx === -1) {
    return `❌ 最近 15 個 state commit 冇 TG 命令, 冇嘢可以退回`;
  }
  const tgCommit = commits[idx];
  const originalCommitMessage = tgCommit.commit.message;
  const undoneCmd = originalCommitMessage.replace(/^tg:\s+/, "");

  // c. 24 小時防呆: 太舊嘅命令, 中間可能夾雜 auto-run 更新會一併還原
  const commitDate = new Date(tgCommit.commit.committer.date);
  const ageMs = Date.now() - commitDate.getTime();
  if (ageMs > 24 * 60 * 60 * 1000 && !force) {
    const dateStr = commitDate.toISOString().slice(0, 16).replace("T", " ");
    return (
      `⚠️ 最近嘅 TG 命令超過 24 小時:\n` +
      `${originalCommitMessage} (${dateStr})\n` +
      `中間可能有 auto-run 更新, 一併還原會覆蓋咗。\n` +
      `確認要退回請打: /undo force`
    );
  }

  // d. 攞該 commit 嘅 parent (命令之前嘅版本)
  const parentSha = tgCommit.parents && tgCommit.parents[0] && tgCommit.parents[0].sha;
  if (!parentSha) {
    return `❌ 呢個 commit 冇 parent, 無法還原`;
  }

  // e. 攞 parent 版本嘅 state 內容, decode base64 + 驗證 JSON parse
  const parentUrl = `https://api.github.com/repos/${repo}/contents/${STATE_PATH}?ref=${parentSha}`;
  const parentRes = await fetch(parentUrl, { headers });
  if (!parentRes.ok) {
    throw new Error(`GitHub API ${parentRes.status}: ${await parentRes.text()}`);
  }
  const parentData = await parentRes.json();
  const raw = atob(parentData.content.replace(/\n/g, ""));
  const oldContent = decodeURIComponent(escape(raw));
  let parsedOldState;
  try {
    parsedOldState = JSON.parse(oldContent);
  } catch (_) {
    return `❌ 還原失敗: 舊版 state 唔係 valid JSON`;
  }

  // f. 攞當前 state 嘅 sha (PUT 需要 fresh sha)
  const { sha: currentSha } = await getState(env);

  // g. 將 parent 內容寫返做當前 state
  await saveState(parsedOldState, currentSha, `tg: undo — ${originalCommitMessage}`, env);

  // h. 成功訊息
  return (
    `↩️ 已退回: ${undoneCmd}\n` +
    `state 已還原到該命令之前\n` +
    `再打 /undo 可以取消呢次退回 (redo)`
  );
}

async function recordBuy(code, shares, price, env, force = false) {
  const code4 = String(code).padStart(4, "0");
  if (shares <= 0 || price <= 0) return `❌ 股數同價錢必須 > 0`;
  const { state, sha } = await getState(env);
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  if (!force) {
    const warn = _priceSanity(state[code4], price, `/buy ${code4} ${shares} ${price}`);
    if (warn) return warn;
  }
  // Lot size check
  const lot = state[code4].lot_size || 0;
  if (lot > 0 && shares % lot !== 0) {
    return `⚠️ ${code4} 每手${lot.toLocaleString()}股，${shares.toLocaleString()}股唔係整手！\n確認無誤請用 /modify ${code4} 股數 均價`;
  }
  // Warn if not in watchlist (no lot size to validate)
  let extraWarn = "";
  if (!state[code4].board) {
    extraWarn = `\n⚠️ ${code4} 未加入監察，冇每手驗證。建議 /add ${code4} [main/gem]`;
  } else if (lot <= 0) {
    extraWarn = `\n⚠️ ${code4} 未有每手記錄，未能驗證整手`;
  }
  // 如果之前已清倉，開新倉但保留歷史盈虧
  if (state[code4].cleared) {
    state[code4].tranches = [];
    state[code4].cleared = false;
    // realized_pnl 保留！累積歷史盈虧
    // 其餘 0成本 fields 全 reset — 新一輪 cycle 唔可以繼承舊 milestone 進度
    state[code4].zero_cost_achieved = false;
    state[code4].zero_cost_shares = null;
    state[code4].zero_cost_initial_shares = null;
    state[code4].zero_cost_price = null;
    state[code4].zero_cost_date = null;
    state[code4].zero_cost_tier = null;
    state[code4].post_zero_done = [];
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = shares * price;
  state[code4].tranches.push({ price, hkd, shares, date: now, note: "via TG" });

  await saveState(state, sha, `tg: buy ${code4} ${shares}股 @${price}`, env);

  const total = state[code4].tranches.filter(t => t.hkd > 0).reduce((s, t) => s + t.hkd, 0);
  return `${code4} 買入 ${shares.toLocaleString()}股 @$${price} 投$${hkd.toLocaleString()}\n累計投入$${total.toLocaleString()}${extraWarn}`;
}

async function addToWatchlist(code, board, env, strategy = null) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  const mjTag = "📘 MJ倉策略 (止賺: 市值達標+100%)";
  if (state[code4] && state[code4].board) {
    // 已監察: 唯一可以做嘅係補標 MJ倉
    if (strategy === "mj" && state[code4].strategy !== "mj") {
      state[code4].strategy = "mj";
      await saveState(state, sha, `tg: mark ${code4} mj`, env);
      return `${code4} 已喺監察清單 (${state[code4].board}) — 現已標記\n${mjTag}`;
    }
    return `${code4} 已經喺監察清單 (${state[code4].board})${state[code4].strategy === "mj" ? " 📘MJ倉" : ""}`;
  }
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  state[code4].board = board;
  if (strategy) state[code4].strategy = strategy;

  await saveState(state, sha, `tg: add ${code4} (${board}${strategy ? " " + strategy : ""})`, env);
  let msg = `${code4} 已加入監察 (${board === "gem" ? "創業板" : "主板"})`;
  if (strategy === "mj") msg += `\n${mjTag}`;
  return msg + "\n每朝09:00自動檢查";
}

async function removeFromWatchlist(code, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) return `${code4} 唔存在`;
  delete state[code4].board;

  await saveState(state, sha, `tg: remove ${code4}`, env);
  return `${code4} 已移除監察`;
}

async function getWatchlist(env) {
  const { state } = await getState(env);
  const watched = Object.entries(state).filter(([, v]) => v.board);
  // 📘 = MJ倉策略 (止賺跟市值達標+100%, 唔跟 L型層級建倉)
  const codeMark = ([c, v]) => c + (v.strategy === "mj" ? "📘" : "");
  const mainList = watched.filter(([, v]) => v.board === "main").map(codeMark).sort();
  const gemList = watched.filter(([, v]) => v.board === "gem").map(codeMark).sort();
  const hasHolding = (v) => v.tranches && v.tranches.length > 0;
  const mainHeld = watched.filter(([, v]) => v.board === "main" && hasHolding(v)).length;
  const gemHeld = watched.filter(([, v]) => v.board === "gem" && hasHolding(v)).length;

  let msg = `監察清單 ${watched.length}隻\n`;
  msg += `\n主板 ${mainList.length}隻 (持倉${mainHeld}):\n${mainList.join(" ")}\n`;
  msg += `\n創業板 ${gemList.length}隻 (持倉${gemHeld}):\n${gemList.join(" ")}`;
  return msg;
}

async function delHolding(code, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) return `${code4} 唔存在`;
  if (!state[code4].tranches || !state[code4].tranches.length) return `${code4} 無持倉`;
  state[code4].tranches = [];
  state[code4].zero_cost_achieved = false;
  state[code4].zero_cost_shares = null;
  state[code4].zero_cost_initial_shares = null;
  state[code4].zero_cost_price = null;
  state[code4].zero_cost_date = null;
  state[code4].zero_cost_tier = null;
  state[code4].post_zero_done = [];

  await saveState(state, sha, `tg: del holding ${code4}`, env);
  return `${code4} 已刪除持倉（仍在監察）`;
}

async function delAll(code, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) return `${code4} 唔存在`;
  delete state[code4];

  await saveState(state, sha, `tg: del all ${code4}`, env);
  return `${code4} 已刪除持倉 + 移除監察`;
}

async function modifyHolding(code, shares, avgPrice, env, force = false) {
  const code4 = String(code).padStart(4, "0");
  if (shares <= 0) return `❌ 股數必須 > 0`;
  const { state, sha } = await getState(env);
  // 防呆: /modify 一個從未存在嘅 code (唔喺監察 + 冇持倉) 多數係 code typo
  // (2026-07-16 事故: /modify 8026 其實想打 8036, 產生幽靈持倉)
  const existing = state[code4];
  if (!existing || (!existing.board && !(existing.tranches || []).length)) {
    if (!force) {
      return (
        `⚠️ ${code4} 唔喺監察名單, 亦冇持倉記錄 — 懷疑 code 打錯?\n` +
        `想真係新增: /add ${code4} [main/gem] 先, 或者命令尾加 force`
      );
    }
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  if (!force) {
    const warn = _priceSanity(state[code4], avgPrice, `/modify ${code4} ${shares} ${avgPrice}`);
    if (warn) return warn;
  }
  // Lot size warning (not blocking — modify is for corrections)
  const lot = state[code4].lot_size || 0;
  let warn = "";
  if (lot > 1 && shares % lot !== 0) {
    warn = `\n⚠️ 每手${lot.toLocaleString()}股，${shares.toLocaleString()}股唔係整手`;
  }
  // Show before vs after
  const oldTr = state[code4].tranches || [];
  const oldShares = oldTr.reduce((s, t) => s + (t.shares || 0), 0);
  const oldInv = oldTr.reduce((s, t) => s + (t.hkd || 0), 0);
  const oldAvg = oldShares > 0 ? oldInv / oldShares : 0;

  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = shares * avgPrice;
  state[code4].tranches = [{ price: avgPrice, hkd, shares, date: now, note: "modified via TG" }];
  // /modify = 重新定義持倉 → 清晒 0成本/清倉 state, 避免矛盾
  state[code4].zero_cost_achieved = false;
  state[code4].zero_cost_shares = null;
  state[code4].zero_cost_initial_shares = null;
  state[code4].zero_cost_price = null;
  state[code4].zero_cost_date = null;
  state[code4].zero_cost_tier = null;
  state[code4].post_zero_done = [];
  state[code4].cleared = false;

  await saveState(state, sha, `tg: modify ${code4} ${shares}股 @${avgPrice}`, env);
  return `${code4} 已修正${warn}\n舊: ${oldShares.toLocaleString()}股 @$${oldAvg.toFixed(4)} 投$${oldInv.toLocaleString()}\n新: ${shares.toLocaleString()}股 @$${avgPrice} 投$${hkd.toLocaleString()}`;
}

async function recordSell(code, sharesSold, price, env, force = false) {
  const code4 = String(code).padStart(4, "0");
  if (sharesSold <= 0 || price <= 0) return `❌ 股數同價錢必須 > 0`;
  const { state, sha } = await getState(env);
  if (!state[code4] || !state[code4].tranches.length) {
    return `${code4} 無持倉記錄`;
  }
  if (!force) {
    const warn = _priceSanity(state[code4], price, `/sell ${code4} ${sharesSold} ${price}`);
    if (warn) return warn;
  }
  // Check not selling more than held
  const held = state[code4].tranches.reduce((s, t) => s + (t.shares || 0), 0);
  if (sharesSold > held) {
    return `⚠️ ${code4} 只持${held.toLocaleString()}股，唔可以賣${sharesSold.toLocaleString()}股`;
  }
  // Lot size check
  const lot = state[code4].lot_size || 0;
  if (lot > 0 && sharesSold % lot !== 0) {
    return `⚠️ ${code4} 每手${lot.toLocaleString()}股，${sharesSold.toLocaleString()}股唔係整手！`;
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = sharesSold * price;
  state[code4].tranches.push({ price, hkd: -hkd, shares: -sharesSold, date: now, note: `sell via TG` });

  const totalCost = state[code4].tranches.filter(t => t.hkd > 0).reduce((s, t) => s + t.hkd, 0);
  const totalRecv = state[code4].tranches.filter(t => t.hkd < 0).reduce((s, t) => s + Math.abs(t.hkd), 0);
  const totalInv = totalCost - totalRecv;
  const remainShares = _shares(state[code4].tranches);
  let msg = `${code4} 賣出 ${sharesSold.toLocaleString()}股 @$${price} 收$${hkd.toLocaleString()}`;

  if (remainShares <= 0) {
    // 全部賣清 → 累積已實現盈虧
    const thisPnl = totalRecv - totalCost;
    const prevPnl = state[code4].realized_pnl || 0;
    const totalPnl = prevPnl + thisPnl;
    const sign = totalPnl >= 0 ? "+" : "";
    state[code4].cleared = true;
    state[code4].realized_pnl = totalPnl;
    state[code4].tranches = [];
    msg += `\n已清倉 本次${thisPnl >= 0 ? "+" : ""}$${thisPnl.toLocaleString(undefined, {maximumFractionDigits: 0})}`;
    if (prevPnl !== 0) msg += ` 累計${sign}$${totalPnl.toLocaleString(undefined, {maximumFractionDigits: 0})}`;
  } else if (totalInv <= 0 && !state[code4].zero_cost_achieved) {
    // 已回收成本 → 自動標記0成本
    state[code4].zero_cost_achieved = true;
    state[code4].zero_cost_shares = remainShares;
    state[code4].zero_cost_initial_shares = remainShares;
    state[code4].zero_cost_date = now.slice(0, 10);
    // M1 於0成本時視為完成; 鎖定重新建倉 floor
    if (!state[code4].post_zero_done) state[code4].post_zero_done = [];
    if (!state[code4].post_zero_done.includes(0)) state[code4].post_zero_done.push(0);
    const mcapM0 = state[code4].last_mcap_m;
    if (mcapM0) state[code4].zero_cost_tier = _tierReached(mcapM0, state[code4].board || "main");
    msg += `\n剩${remainShares.toLocaleString()}股 🎉 0成本達成！免費持倉`;
  } else if (state[code4].zero_cost_achieved) {
    // 0成本後賣出 (M2-M5 或自行減持): 減免費股數 + 標記已到達嘅 milestone
    const prevZ = state[code4].zero_cost_shares || 0;
    state[code4].zero_cost_shares = Math.max(0, prevZ - sharesSold);
    if (!state[code4].post_zero_done) state[code4].post_zero_done = [];
    const done = state[code4].post_zero_done;
    if (!done.includes(0)) done.push(0); // M1 完成於0成本時
    const MS = (state[code4].board === "gem") ? [150, 300, 450, 600, 750] : [400, 800, 1200, 1600, 2000];
    const mcapM = state[code4].last_mcap_m || 0;
    for (let i = 1; i < MS.length; i++) {
      if (mcapM >= MS[i] && !done.includes(i)) {
        done.push(i);
        msg += `\n✅ M${i + 1} 標記完成`;
        break;
      }
    }
    msg += `\n剩${remainShares.toLocaleString()}股 (0成本剩${state[code4].zero_cost_shares.toLocaleString()}股)`;
  } else {
    msg += `\n剩${remainShares.toLocaleString()}股`;
  }

  await saveState(state, sha, `tg: sell ${code4} ${sharesSold}股 @${price}`, env);
  return msg;
}

async function markZeroCost(code, remainShares, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) return `${code4} 唔存在`;

  const now = new Date().toISOString().slice(0, 10);
  state[code4].zero_cost_achieved = true;
  state[code4].zero_cost_shares = remainShares;
  state[code4].zero_cost_date = now;
  if (!state[code4].post_zero_done) state[code4].post_zero_done = [];
  if (!state[code4].post_zero_done.includes(0)) {
    state[code4].post_zero_done.push(0);
  }

  await saveState(state, sha, `tg: zerocost ${code4} remain=${remainShares}`, env);
  return `${code4} 已標記0成本 剩${remainShares}股免費持倉`;
}

function _pnl(inv, val) {
  const diff = val - inv;
  const pct = inv > 0 ? (diff / inv * 100).toFixed(0) : 0;
  const sign = diff >= 0 ? "+" : "";
  return `${sign}$${diff.toLocaleString(undefined, {maximumFractionDigits: 0})} (${sign}${pct}%)`;
}

function _shares(tranches) {
  return tranches.reduce((s, t) => s + (t.shares || 0), 0);
}

// 價錢防呆: 同 last_price 差 3 倍以上多數係 typo (e.g. 0.38 vs 0.038)
// 真係大幅跳價可以加 "force" 落命令尾 bypass
function _priceSanity(st, price, cmdHint) {
  const ref = st && st.last_price;
  if (!ref || ref <= 0 || !price || price <= 0) return null;
  const ratio = price / ref;
  if (ratio >= 3 || ratio <= 1 / 3) {
    return (
      `⚠️ 價錢可疑: 輸入 $${price} vs 最近價 $${ref} (${ratio >= 3 ? ratio.toFixed(1) + "×" : "1/" + (1 / ratio).toFixed(1)})\n` +
      `懷疑打錯位。確認無誤請喺命令尾加 force:\n${cmdHint} force`
    );
  }
  return null;
}

// 觸發層數: mcap 跌穿幾多個 tier (至少 1, 用於 zero_cost_tier floor)
function _tierReached(mcapM, board) {
  const tiers = board === "gem" ? [80, 60, 50, 40, 30] : [200, 150, 120, 100, 80, 60];
  let n = 0;
  for (const t of tiers) {
    if (mcapM <= t) n++;
    else break;
  }
  return Math.max(1, n);
}

function _stockLine(c, v) {
  const shares = _shares(v.tranches);
  const price = v.last_price || 0;

  if (v.cleared) {
    // 已清倉：顯示已實現盈虧
    const pnl = v.realized_pnl || 0;
    const sign = pnl >= 0 ? "+" : "";
    return `${c} | 0股 | 已清倉 | ${sign}$${pnl.toLocaleString(undefined, {maximumFractionDigits: 0})}`;
  }

  const inv = v.tranches.reduce((s, t) => s + t.hkd, 0);
  const val = shares * price;
  const avg = shares > 0 ? (inv / shares) : 0;
  const hist = (v.realized_pnl && !v.cleared) ? ` 歷史${v.realized_pnl >= 0 ? "+" : ""}$${v.realized_pnl.toLocaleString(undefined, {maximumFractionDigits: 0})}` : "";
  return `${c} | ${shares.toLocaleString()}股 | @$${avg.toFixed(3)} | ${_pnl(inv, val)}${hist}`;
}

async function getStatus(code, env) {
  const { state } = await getState(env);
  if (!code) {
    // 有 tranches 或已清倉嘅都顯示
    const held = Object.entries(state).filter(([, v]) =>
      (v.tranches && v.tranches.length) || v.cleared
    );
    let totalInv = 0, totalVal = 0, totalRealized = 0;
    const lines = held.map(([c, v]) => {
      totalRealized += (v.realized_pnl || 0);
      if (!v.cleared && v.tranches && v.tranches.length) {
        const inv = v.tranches.reduce((s, t) => s + t.hkd, 0);
        const shares = _shares(v.tranches);
        const val = shares * (v.last_price || 0);
        totalInv += inv;
        totalVal += val;
      }
      return _stockLine(c, v);
    });
    const unrealized = totalVal - totalInv;
    const totalPnl = unrealized + totalRealized;
    const sign = totalPnl >= 0 ? "+" : "";
    let summary = `\n——\n總投$${totalInv.toLocaleString()} 現值$${totalVal.toLocaleString(undefined, {maximumFractionDigits: 0})} ${_pnl(totalInv, totalVal)}`;
    if (totalRealized !== 0) {
      summary += `\n已實現${totalRealized >= 0 ? "+" : ""}$${totalRealized.toLocaleString(undefined, {maximumFractionDigits: 0})} 總盈虧${sign}$${totalPnl.toLocaleString(undefined, {maximumFractionDigits: 0})}`;
    }
    return `持倉 ${held.length}隻:\n${lines.join("\n")}${summary}`;
  }
  const code4 = String(code).padStart(4, "0");
  const st = state[code4];
  if (!st) return `${code4} 唔存在`;
  if (st.cleared) {
    const pnl = st.realized_pnl || 0;
    const sign = pnl >= 0 ? "+" : "";
    return `${code4}\n已清倉 ${sign}$${pnl.toLocaleString(undefined, {maximumFractionDigits: 0})}`;
  }
  if (!st.tranches || !st.tranches.length) return `${code4} 無持倉`;
  const inv = st.tranches.reduce((s, t) => s + t.hkd, 0);
  const shares = _shares(st.tranches);
  const price = st.last_price || 0;
  const val = shares * price;
  return `${code4}\n${shares.toLocaleString()}股 @$${price}\n投$${inv.toLocaleString()} 值$${val.toLocaleString(undefined, {maximumFractionDigits: 0})} ${_pnl(inv, val)}`;
}

// ── MJ 半新股系統 (獨立層, 只做警告 — 唔會出買賣命令) ──

async function getMjState(env) {
  const token = (env.GITHUB_TOKEN || "").trim();
  const repo = (env.GITHUB_REPO || "").trim();
  const url = `https://api.github.com/repos/${repo}/contents/${MJ_STATE_PATH}?ref=${BRANCH}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.v3+json",
      "User-Agent": "investbot-tg-worker",
    },
  });
  if (res.status === 404) return null;   // 未匯入過功課
  if (!res.ok) {
    throw new Error(`GitHub API ${res.status}: ${await res.text()}`);
  }
  const data = await res.json();
  const raw = atob(data.content.replace(/\n/g, ""));
  return JSON.parse(decodeURIComponent(escape(raw)));
}

// MJ 現價: monitor 寫落嘅 last_price 優先, 否則用 PDF snapshot
function _mjPrice(s) {
  return s.last_price || s.pdf_price || 0;
}

// 主 state cross-reference: 返回 {held, tag}
function _mjHold(state, code) {
  const st = state[code];
  const held = (st && st.tranches && st.tranches.length) ? _shares(st.tranches) : 0;
  if (held > 0) return { held, tag: `持${held.toLocaleString()}股` };
  if (st && st.board) return { held: 0, tag: "監察" };
  return { held: 0, tag: "未監察" };
}

function _mjPos(s) {
  const p = _mjPrice(s);
  if (s.p3 && p >= s.p3) return "位置3";
  if (s.p2 && p >= s.p2) return "位置2";
  if (s.p0 && p >= s.p0) return "位置0";
  return null;
}

async function getMj(code, env) {
  const mj = await getMjState(env);
  if (!mj || !mj.stocks || !Object.keys(mj.stocks).length) {
    return (
      "📘 MJ 半新股: 未有功課數據\n" +
      "先跑 python mj_import.py 匯入每日功課 PDF, push 上 GitHub 之後再試"
    );
  }
  const meta = mj._meta || {};
  const stocks = mj.stocks;
  const { state } = await getState(env);

  // ── /mj CODE — 單隻全部數據 ──
  if (code) {
    const c4 = String(code).padStart(4, "0");
    const s = stocks[c4];
    if (!s) return `${c4} 唔喺 MJ 名單 (功課 ${meta.pdf_date || "?"})`;
    const p = _mjPrice(s);
    const h = _mjHold(state, c4);
    const st = state[c4];
    const lines = [
      `📘 ${c4} ${s.name || ""} (功課 ${meta.pdf_date || "?"})`,
      `動能 ${(s.mom || 0).toFixed(2)} (變化${(s.mom_chg || 0) >= 0 ? "+" : ""}${(s.mom_chg || 0).toFixed(2)})`,
      `現價 $${p.toFixed(3)}${s.last_check ? "" : " (PDF價)"} | 狀態 ${(s.status_pct || 0) >= 0 ? "+" : ""}${(s.status_pct || 0).toFixed(1)}%`,
      `留意日 ${s.watch_date || "?"} 收$${(s.watch_close || 0).toFixed(3)}`,
      `🛑 放棄位 $${(s.abandon || 0).toFixed(3)}${s.abandon && p <= s.abandon ? "  ← 已到!" : ""}`,
      `位置0 $${(s.p0 || 0).toFixed(3)} | 位置2 $${(s.p2 || 0).toFixed(3)} | 位置3 $${(s.p3 || 0).toFixed(3)}`,
    ];
    const pos = _mjPos(s);
    if (pos) lines.push(`⚡ 已到 ${pos}`);
    lines.push(
      `每手$${(s.lot_hkd || 0).toLocaleString()} | 市值${(s.mcap_y || 0).toFixed(2)}億`,
      `CCASS Top5 ${(s.ccass5 || 0).toFixed(1)}% | 券商${s.brokers || 0}間`,
      `IPO ${s.ipo || "?"}`
    );
    if (s.info && s.info.length) lines.push(`Tags: ${s.info.join(" ")}`);
    if (st && st.strategy === "mj") {
      lines.push("📘 已標記 MJ倉 (止賺: 5億/2億 + 100%)");
    }
    if (h.held > 0 && st && st.tranches) {
      const inv = st.tranches.reduce((a, t) => a + t.hkd, 0);
      const val = h.held * (st.last_price || p);
      lines.push(`\n你嘅持倉: ${h.held.toLocaleString()}股 ${_pnl(inv, val)}`);
      if (s.mom < MJ_MOM_FLOOR || (s.abandon && p <= s.abandon)) {
        lines.push("⚠️ MJ 叫放棄, 但你有持倉 — 自己決定");
      }
    } else {
      lines.push(`\n你嘅持倉: 冇 (${h.tag})`);
    }
    return lines.join("\n");
  }

  // ── /mj — 摘要 ──
  const codes = Object.keys(stocks).sort();
  const drop = [];
  const entry = [];  // 💰 入場區: 現價貼近留意位 ±30%
  let nLowMom = 0, nDanger = 0, nHeldDrop = 0;
  for (const c of codes) {
    const s = stocks[c];
    const p = _mjPrice(s);
    const rs = [];
    if ((s.mom || 0) < MJ_MOM_FLOOR) {
      rs.push(`低動能${(s.mom || 0).toFixed(1)}`);
      nLowMom++;
    }
    if (s.abandon && p && p <= s.abandon) {
      rs.push(`到放棄位$${s.abandon.toFixed(3)}`);
    }
    if (_mjPos(s)) nDanger++;
    if (rs.length) {
      const h = _mjHold(state, c);
      if (h.held > 0) nHeldDrop++;
      drop.push(
        `${h.held > 0 ? "⚠️ " : ""}${c} ${s.name || ""} ${h.tag}\n   ${rs.join(" ")}`
      );
    } else {
      // 入場區: 建議買入位 = 留意日收市價, 差距 ±30% 內 (排除已叫放棄)
      const wc = s.watch_close || 0;
      if (wc && p) {
        const st_pct = (p - wc) / wc * 100;
        if (st_pct >= -30 && st_pct <= 30) {
          const h = _mjHold(state, c);
          entry.push({ d: Math.abs(st_pct),
            row: `${c} ${s.name || ""} ${st_pct >= 0 ? "+" : ""}${st_pct.toFixed(0)}% 留意$${wc.toFixed(3)} 現$${p.toFixed(3)} 動能${(s.mom || 0).toFixed(0)} ${h.tag}` });
        }
      }
    }
  }

  const out = [
    `📘 MJ 半新股系統`,
    `功課日期 ${meta.pdf_date || "?"} | 共 ${codes.length} 隻`,
    `匯入 ${meta.imported_at || "?"}`,
    "-------------------",
  ];
  if (drop.length) {
    out.push(`🛑 要放棄 ${drop.length}隻${nHeldDrop ? ` (其中 ${nHeldDrop} 隻你有持倉!)` : ""}`);
    out.push(drop.slice(0, 15).join("\n"));
    if (drop.length > 15) out.push(`…及其他${drop.length - 15}隻`);
  } else {
    out.push("🛑 要放棄: 冇");
  }
  if (entry.length) {
    entry.sort((a, b) => a.d - b.d);  // 最貼留意位排最前
    out.push("-------------------");
    out.push(`💰 入場區 ±30% (${entry.length}隻) — 貼近留意位, 可細注試倉`);
    out.push(entry.slice(0, 20).map(e => e.row).join("\n"));
    if (entry.length > 20) out.push(`…及其他${entry.length - 20}隻`);
  }
  out.push("-------------------");
  out.push(`⚡ 到危險位置: ${nDanger}隻`);
  out.push(`📉 低動能(<${MJ_MOM_FLOOR}): ${nLowMom}隻`);
  out.push("\n💡 MJ 信號只係警告, 買賣自己決定");
  out.push("💡 /mj CODE 睇單隻詳情");
  return out.join("\n");
}

// ── GitHub Actions workflow_dispatch ──

async function triggerGitHubWorkflow(mode, env) {
  const token = (env.GITHUB_TOKEN || "").trim();
  const repo = (env.GITHUB_REPO || "").trim();
  const url = `https://api.github.com/repos/${repo}/actions/workflows/watchlist_monitor.yml/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.v3+json",
      "User-Agent": "investbot-tg-worker",
    },
    body: JSON.stringify({
      ref: BRANCH,
      inputs: { mode },
    }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub dispatch ${res.status}: ${body.slice(0, 200)}`);
  }
  return true;
}

// ── Telegram ──

async function sendTG(token, chatId, text) {
  return fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}
