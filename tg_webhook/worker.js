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
};

async function handleCommand(text, env) {
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();

  if (cmd === "/buy") {
    if (parts.length < 4) return "用法: /buy CODE 股數 價錢\n例: /buy 1740 70000 0.114";
    const [, code, shares, price] = parts;
    return await recordBuy(code, parseInt(shares), parseFloat(price), env);
  }

  if (cmd === "/sell") {
    if (parts.length < 4) return "用法: /sell CODE 股數 價錢\n例: /sell 1740 10000 0.20";
    const [, code, shares, price] = parts;
    return await recordSell(code, parseInt(shares), parseFloat(price), env);
  }

  if (cmd === "/zerocost") {
    if (parts.length < 3) return "用法: /zerocost CODE 剩餘股數\n例: /zerocost 2370 13000";
    const [, code, remaining] = parts;
    return await markZeroCost(code, parseInt(remaining), env);
  }

  if (cmd === "/modify") {
    if (parts.length < 4) return "用法: /modify CODE 股數 平均價\n例: /modify 1740 70000 0.114";
    const [, code, shares, avgPrice] = parts;
    return await modifyHolding(code, parseInt(shares), parseFloat(avgPrice), env);
  }

  if (cmd === "/del") {
    if (parts.length < 2) return "用法: /del CODE\n例: /del 0368";
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
      "每跌穿一層 → TG提醒買$8,000\n" +
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

  if (cmd === "/help" || cmd === "/start") {
    return (
      "指令:\n" +
      "/buy CODE 股數 價錢 — 記錄買入\n" +
      "/sell CODE 股數 價錢 — 記錄賣出\n" +
      "/modify CODE 股數 平均價 — 修正持倉\n" +
      "/del CODE — 刪除持倉\n" +
      "/add CODE [main/gem] — 加入監察\n" +
      "/remove CODE — 移除監察\n" +
      "/watchlist — 睇監察清單\n" +
      "/rules — 睇買賣機制\n" +
      "/status CODE — 查看持倉\n" +
      "/status — 查看全部\n" +
      "\n💡 賣出後自動偵測0成本"
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

async function recordBuy(code, shares, price, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  // 如果之前已清倉，開新倉但保留歷史盈虧
  if (state[code4].cleared) {
    state[code4].tranches = [];
    state[code4].cleared = false;
    // realized_pnl 保留！累積歷史盈虧
    state[code4].zero_cost_achieved = false;
    state[code4].zero_cost_shares = null;
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = shares * price;
  state[code4].tranches.push({ price, hkd, shares, date: now, note: "via TG" });

  // 每次 /buy 推進 tier_reached +1，令 monitor 唔再提醒已買層
  const prev = state[code4].tier_reached || 0;
  state[code4].tier_reached = prev + 1;

  await saveState(state, sha, `tg: buy ${code4} ${shares}股 @${price}`, env);

  const total = state[code4].tranches.reduce((s, t) => s + t.hkd, 0);
  const tier = state[code4].tier_reached;
  return `${code4} 買入 ${shares.toLocaleString()}股 @$${price} 投$${hkd.toLocaleString()}\n累計投入$${total.toLocaleString()} (tier ${tier})`;
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

  await saveState(state, sha, `tg: del ${code4}`, env);
  return `${code4} 已刪除持倉`;
}

async function modifyHolding(code, shares, avgPrice, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = shares * avgPrice;
  state[code4].tranches = [{ price: avgPrice, hkd, shares, date: now, note: "modified via TG" }];

  await saveState(state, sha, `tg: modify ${code4} ${shares}股 @${avgPrice}`, env);
  return `${code4} 已修正\n${shares.toLocaleString()}股 @$${avgPrice}\n總投入$${hkd.toLocaleString()}`;
}

async function recordSell(code, sharesSold, price, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4] || !state[code4].tranches.length) {
    return `${code4} 無持倉記錄`;
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
    state[code4].zero_cost_date = now.slice(0, 10);
    msg += `\n剩${remainShares.toLocaleString()}股 🎉 0成本達成！免費持倉`;
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

// ── Telegram ──

async function sendTG(token, chatId, text) {
  return fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}
