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
    if (parts.length < 4) return "用法: /buy CODE PRICE HKD\n例: /buy 2370 1.05 8000";
    const [, code, price, hkd] = parts;
    return await recordBuy(code, parseFloat(price), parseFloat(hkd), env);
  }

  if (cmd === "/sell") {
    if (parts.length < 4) return "用法: /sell CODE PRICE SHARES\n例: /sell 2370 1.20 4000";
    const [, code, price, shares] = parts;
    return await recordSell(code, parseFloat(price), parseInt(shares), env);
  }

  if (cmd === "/zerocost") {
    if (parts.length < 3) return "用法: /zerocost CODE SHARES\n例: /zerocost 2370 13000";
    const [, code, remaining] = parts;
    return await markZeroCost(code, parseInt(remaining), env);
  }

  if (cmd === "/status") {
    const code = parts[1];
    return await getStatus(code, env);
  }

  if (cmd === "/help" || cmd === "/start") {
    return (
      "指令:\n" +
      "/buy CODE PRICE HKD — 記錄買入\n" +
      "/sell CODE PRICE SHARES — 記錄賣出\n" +
      "/zerocost CODE SHARES — 標記0成本(剩餘股數)\n" +
      "/status CODE — 查看持倉\n" +
      "/status — 查看全部"
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

async function recordBuy(code, price, hkd, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4]) {
    state[code4] = { tier_reached: 0, tranches: [], zero_cost_achieved: false, post_zero_done: [], notes: [] };
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const shares = Math.floor(hkd / price);
  state[code4].tranches.push({ price, hkd, shares, date: now, note: "via TG" });

  await saveState(state, sha, `tg: buy ${code4} @${price} $${hkd}`, env);

  const total = state[code4].tranches.reduce((s, t) => s + t.hkd, 0);
  return `${code4} 買入 ${shares}股 @$${price} 投$${hkd}\n累計投入$${total.toLocaleString()}`;
}

async function recordSell(code, price, sharesSold, env) {
  const code4 = String(code).padStart(4, "0");
  const { state, sha } = await getState(env);
  if (!state[code4] || !state[code4].tranches.length) {
    return `${code4} 無持倉記錄`;
  }
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const hkd = sharesSold * price;
  state[code4].tranches.push({ price, hkd: -hkd, shares: -sharesSold, date: now, note: `sell via TG` });

  await saveState(state, sha, `tg: sell ${code4} ${sharesSold}股 @${price}`, env);
  return `${code4} 賣出 ${sharesSold}股 @$${price} 收$${hkd.toLocaleString()}`;
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

async function getStatus(code, env) {
  const { state } = await getState(env);
  if (!code) {
    const held = Object.entries(state).filter(([, v]) => v.tranches && v.tranches.length);
    const lines = held.map(([c, v]) => {
      const inv = v.tranches.reduce((s, t) => s + t.hkd, 0);
      const z = v.zero_cost_achieved ? " [0成本]" : "";
      return `${c}${z} 投$${inv.toLocaleString()}`;
    });
    return `持倉 ${held.length}隻:\n${lines.join("\n")}`;
  }
  const code4 = String(code).padStart(4, "0");
  const st = state[code4];
  if (!st) return `${code4} 唔存在`;
  if (!st.tranches || !st.tranches.length) return `${code4} 無持倉`;
  const inv = st.tranches.reduce((s, t) => s + t.hkd, 0);
  const z = st.zero_cost_achieved ? `\n0成本 剩${st.zero_cost_shares}股` : "";
  return `${code4}\n投$${inv.toLocaleString()}\n${st.tranches.length}筆交易${z}`;
}

// ── Telegram ──

async function sendTG(token, chatId, text) {
  return fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}
