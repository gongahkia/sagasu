const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../bot/passkey_cancel.js'), 'utf8');

function fixture(hostname = 'login.microsoft.com') {
  const seen = [];
  const credentials = {
    get(options) {
      seen.push(options);
      if (!options?.publicKey) return Promise.resolve('non-passkey');
      return new Promise((resolve, reject) => {
        if (options.signal.aborted) reject(options.signal.reason);
        else options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true});
      });
    }
  };
  const window = {};
  vm.runInNewContext(source, {location:{hostname}, navigator:{credentials}, window, AbortController});
  return {credentials, window, seen};
}

test('pending passkey request rejects when explicitly cancelled; no credential is returned', async () => {
  const {credentials, window} = fixture();
  const pending = credentials.get({publicKey:{challenge:new Uint8Array(32)}});
  const rejected = assert.rejects(pending, {name:'AbortError'});
  assert.equal(window.__sagasuCancelPasskeyRequest(), 1);
  await rejected;
  assert.equal(window.__sagasuCancelPasskeyRequest(), 0);
});

test('the original caller can still cancel its own request', async () => {
  const {credentials, window} = fixture('login.microsoftonline.com');
  const controller = new AbortController();
  const pending = credentials.get({publicKey:{}, signal:controller.signal});
  const rejected = assert.rejects(pending, {name:'AbortError'});
  controller.abort();
  await rejected;
  assert.equal(window.__sagasuCancelPasskeyRequest(), 0);
});

test('non-passkey calls are delegated without changing their options', async () => {
  const {credentials, seen} = fixture();
  const options = {password:true};
  assert.equal(await credentials.get(options), 'non-passkey');
  assert.equal(seen[0], options);
});

test('the hook is restricted to the actual Microsoft login hosts', () => {
  for (const host of ['fbs.intranet.smu.edu.sg', 'login.microsoft.com.example.org']) {
    assert.equal(fixture(host).window.__sagasuCancelPasskeyRequest, undefined);
  }
});
