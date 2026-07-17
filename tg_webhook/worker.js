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
  async scheduled(event, env, ctx) {
    try {
      const mode = "alert";  // 09:00 + 15:30 都係 full alert
      await triggerGitHubWorkflow(mode, env);
    } catch (err) {
      // 通知自己 cron 失敗
      try {
        await sendTG(
          env.TELEGRAM_TOKEN.trim(),
          (env.CHAT_ID || "").trim(),
          `⚠️ Cron trigger failed: ${err.message}`
        );
      } catch (_) {}
    }
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
    if (parts.length < 2) return "用法: /add CODE [main/gem]\n例: /add 1234\n例: /add 8888 gem";
    const board = (parts[2] || "").toLowerCase() === "gem" ? "gem" : "main";
    return await addToWatchlist(parts[1], board, env);
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
      "浮盈≥100% + 市值≥4億 或 浮盈≥200%\n" +
      "→ TG提醒賣出回收成本\n" +
      "→ 剩餘股數 = 免費持倉(0成本)\n\n" +
      "【清倉】\n" +
      "全部賣晒 → 記錄已實現盈虧\n" +
      "日後重新買入 → 歷史盈虧保留累計\n\n" +
      "💡 所有交易由你手動 /buy /sell 記帳"
    );
  }

  if (cmd === "/status") {
    const code = parts[1];
    return await getStatus(code, env);
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

  if (cmd === "/help" || cmd === "/start") {
    return (
      "指令:\n" +
      "/buy CODE 股數 價錢 — 記錄買入\n" +
      "/sell CODE 股數 價錢 — 記錄賣出\n" +
      "/modify CODE 股數 平均價 — 修正持倉\n" +
      "/del CODE — 刪除持倉（保留監察）\n" +
      "/del CODE watchlist — 刪除持倉+移除監察\n" +
      "/add CODE [main/gem] — 加入監察\n" +
      "/remove CODE — 移除監察\n" +
      "/watchlist — 睇監察清單\n" +
      "/rules — 睇買賣機制\n" +
      "/status CODE — 查看持倉\n" +
      "/status — 查看全部\n" +
      "/push — 即時觸發 alert push\n" +
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

async function addToWatchlist(code, board, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (state[code4] && state[code4].board) {
    return `${code4} 已經喺監察清單 (${state[code4].board})`;
  }
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  state[code4].board = board;

  await saveState(state, sha, `tg: add ${code4} (${board})`, env);
  return `${code4} 已加入監察 (${board === "gem" ? "創業板" : "主板"})\n每朝09:00自動檢查`;
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
  const mainList = watched.filter(([, v]) => v.board === "main").map(([c]) => c).sort();
  const gemList = watched.filter(([, v]) => v.board === "gem").map(([c]) => c).sort();
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
