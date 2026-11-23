import test from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync, verify} from 'node:crypto';
import {headers, host, loadKey} from './exness_signer.mjs';

test('signed request binds GET, path and empty body to an ephemeral Ed25519 key', () => {
  const {privateKey, publicKey} = generateKeyPairSync('ed25519');
  const pem = privateKey.export({format: 'pem', type: 'pkcs8'});
  const result = headers('synthetic-api-key', loadKey(pem), '/v1/example?symbol=XAUUSD');
  const payload = Buffer.from(result['EXN-DATA'], 'base64url');
  assert.equal(JSON.parse(payload).method, 'GET');
  assert.equal(JSON.parse(payload).path, '/v1/example?symbol=XAUUSD');
  assert.ok(verify(null, payload, publicKey, Buffer.from(result['EXN-SIGN'], 'base64url')));
  assert.equal(verify(null, Buffer.from('changed'), publicKey, Buffer.from(result['EXN-SIGN'], 'base64url')), false);
});

test('transport rejects unrelated hosts and URL credential tricks', () => {
  assert.equal(host('https://example.exness.com'), 'https://example.exness.com');
  for (const value of ['http://exness.com', 'https://exness.com.evil.test', 'https://user:pass@exness.com',
                       'https://exness.com/path', 'https://exness.com?token=test', 'https://exness.com:8443']) {
    assert.throws(() => host(value), /UNTRUSTED_HOST/);
  }
});

test('key loader rejects malformed material and wrong key type', () => {
  assert.throws(() => loadKey('synthetic-invalid-key'), /INVALID_KEY_FORMAT/);
  const {privateKey} = generateKeyPairSync('ec', {namedCurve: 'prime256v1'});
  assert.throws(() => loadKey(privateKey.export({format: 'pem', type: 'pkcs8'})), /WRONG_KEY_TYPE/);
});
