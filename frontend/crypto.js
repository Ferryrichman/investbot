// Browser-side decryption (matches shared/encrypt.py)
// AES-GCM + PBKDF2-HMAC-SHA256, 250k iterations

const ITERATIONS = 250000;
const KEY_LEN = 32;

function b64decode(s) {
  const bin = atob(s);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

async function deriveKey(passphrase, salt, iterations = ITERATIONS) {
  const enc = new TextEncoder();
  const pwKey = await crypto.subtle.importKey(
    'raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveKey']
  );
  return crypto.subtle.deriveKey(
    {
      name: 'PBKDF2',
      salt: salt,
      iterations: iterations,
      hash: 'SHA-256',
    },
    pwKey,
    { name: 'AES-GCM', length: 256 },
    false,
    ['decrypt']
  );
}

/**
 * Decrypt an encrypted blob {v, iter, salt, iv, ct} with passphrase.
 * Returns parsed JSON object, or throws.
 */
async function decryptBlob(blob, passphrase) {
  const salt = b64decode(blob.salt);
  const iv = b64decode(blob.iv);
  const ct = b64decode(blob.ct);
  const iterations = blob.iter || ITERATIONS;
  const key = await deriveKey(passphrase, salt, iterations);
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: iv }, key, ct);
  const text = new TextDecoder().decode(plain);
  return JSON.parse(text);
}

window.FRCrypto = { decryptBlob };
