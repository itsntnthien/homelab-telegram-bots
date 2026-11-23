// Reused from Indicator/local-terminal/exness-probe.mjs; Ed25519 GET signing only.
import {createHash,createPrivateKey,createPublicKey,sign} from 'node:crypto';
export function loadKey(text) {
  const value = text.trim();
  let key;
  if (value.startsWith('-----BEGIN PRIVATE KEY-----')) key = createPrivateKey(value);
  else {
    if (!/^[a-f0-9]{128}$/i.test(value)) throw Error('INVALID_KEY_FORMAT');
    const raw = Buffer.from(value, 'hex');
    key = createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), raw.subarray(0, 32)]), format: 'der', type: 'pkcs8' });
    if (!Buffer.from(createPublicKey(key).export({ format: 'jwk' }).x, 'base64url').equals(raw.subarray(32))) throw Error('KEY_MISMATCH');
  }
  if (key.asymmetricKeyType !== 'ed25519') throw Error('WRONG_KEY_TYPE');
  return key;
}
export function headers(apiKey, key, path) {
  const timestamp = Date.now();
  const payload = Buffer.from(JSON.stringify({ api_key: apiKey, idempotency_key: '', timestamp, sign_version: 1,
    method: 'GET', path, body_hash: createHash('sha256').update('').digest('base64url') }));
  return { 'EXN-API-KEY': apiKey, 'EXN-IDEMPOTENCY-KEY': '', 'EXN-TIMESTAMP': String(timestamp), 'EXN-SIGN-VERSION': '1',
    'EXN-DATA': payload.toString('base64url'), 'EXN-SIGN': sign(null, payload, key).toString('base64url') };
}
export function host(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.pathname !== '/' || url.search || url.hash ||
      !['exness.com', 'exness-api.com'].some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain))) throw Error('UNTRUSTED_HOST');
  return url.origin;
}
