// Step driver for @qsp/wasm-engine (QSP 5.9) used by `aero2qspider convert --compare`.
// Same protocol and state format as qsp57_driver.c: one command per stdin line, one JSON state per stdout line.
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { initQspEngine } from '@qsp/wasm-engine';

const require = createRequire(import.meta.url);
const gameFile = process.argv[2];
if (!gameFile) {
  console.error('usage: node qsp59-driver.mjs GAME.qsp');
  process.exit(2);
}

const wasm = fs.readFileSync(require.resolve('@qsp/wasm-engine/qsp-engine.wasm'));
const api = await initQspEngine(new Uint8Array(wasm).buffer);

const state = { main: '', stats: '', actions: [], objects: [] };
let events = [];
let error = null;
let pending = [];
let answers = 0;
let dialogs = 0;
const MAX_DIALOGS = 200;
// answers to INPUT; the same list is used by qsp57_driver.c
const INPUTS = ['1', '0', '2', '', '3'];

// pipes are asynchronous in Node.js: exit only after the output is flushed
const exit = (code) => process.stdout.write('', () => process.exit(code));

function dialog() {
  if (++dialogs > MAX_DIALOGS) {
    process.stderr.write(`the game opened more than ${MAX_DIALOGS} dialogs in one step (an endless loop?)\n`, () => exit(3));
    return false;
  }
  return true;
}

api.on('main_changed', (text) => (state.main = text));
api.on('stats_changed', (text) => (state.stats = text));
api.on('actions_changed', (actions) => (state.actions = actions.map((a) => a.name)));
api.on('objects_changed', (objects) => (state.objects = objects.map((o) => o.name)));
api.on('error', (e) => {
  error ??= { location: e.location, action: e.actionIndex, line: e.line, description: e.description };
});
api.on('msg', (text, closed) => {
  if (!dialog()) return;
  events.push(['msg', text]);
  pending.push(closed);
});
api.on('input', (_text, done) => {
  if (dialog()) pending.push(() => done(INPUTS[answers++ % INPUTS.length]));
});
api.on('menu', (items, select) => {
  if (!dialog()) return;
  for (const item of items) events.push(['menu', item.name]);
  pending.push(() => select(items.length ? answers++ % items.length : -1));
});
api.on('wait', (_ms, done) => pending.push(done));
api.on('view', (file) => file && events.push(['view', file]));
api.on('play_file', (file, _volume, done) => {
  events.push(['play', file]);
  done();
});
api.on('system_cmd', (cmd) => events.push(['system', cmd]));
api.on('version', (_type, done) => done('aero2qspider'));
api.on('open_game', (file, isNew, done) => {
  const data = fs.readFileSync(path.resolve(path.dirname(gameFile), file.replace(/\\/g, '/')));
  api.openGame(new Uint8Array(data).buffer, isNew);
  done();
});
api.on('save_game', (_file, done) => done());
api.on('load_save', (_file, done) => done());
api.on('is_play', (_file, done) => done(false));
api.on('close_file', (_file, done) => done());
for (const name of ['panel_visibility', 'user_input', 'timer']) api.on(name, () => {});

const tick = () => new Promise((resolve) => setImmediate(resolve));

// Answers to MSG/INPUT/MENU/WAIT resume the engine asynchronously, so wait until it settles.
async function run(action) {
  error = null;
  dialogs = 0;
  action();
  for (let i = 0; i < 1000; i++) {
    await tick();
    if (!pending.length) {
      await tick();
      if (!pending.length) break;
    }
    while (pending.length) pending.shift()();
  }
}

// calculateStringExpression() cuts results at 65535 characters
function readText(name, fallback) {
  let text = '';
  for (let start = 1; ; start += 30000) {
    const chunk = api.calculateStringExpression(`$MID(${name}, ${start}, 30000)`);
    if (chunk == null) return fallback;
    text += chunk;
    if (chunk.length < 30000) return text;
  }
}

// After an error inside resumed code the engine skips the screen refresh, so read the texts directly.
function print() {
  const saved = error;
  api.execCode('');
  error = saved;
  state.main = readText('$MAINTXT', state.main);
  state.stats = readText('$STATTXT', state.stats);
  const location = api.calculateStringExpression('$CURLOC') ?? '';
  process.stdout.write(JSON.stringify({ ...state, location, events, error }) + '\n');
  events = [];
}

const decode = (hex) => Buffer.from(hex, 'hex').toString('utf-8');

api.openGame(new Uint8Array(fs.readFileSync(gameFile)).buffer, true);
process.stdout.write('{"ready":true}\n');

for await (const line of readline.createInterface({ input: process.stdin })) {
  const [command, arg = ''] = line.split(' ', 2);
  if (command === 'quit') break;
  if (command === 'exec' || command === 'quiet') {
    await run(() => api.execCode(decode(arg)));
    if (command === 'quiet') {
      events = [];
      continue;
    }
  } else if (command === 'act') {
    await run(() => {
      api.selectAction(Number(arg));
      api.execSelectedAction();
    });
  } else if (command === 'obj') {
    await run(() => api.selectObject(Number(arg)));
  } else if (command === 'tick') {
    await run(() => api.execCounter());
  } else {
    console.error(`unknown command: ${command}`);
    continue;
  }
  print();
}
exit(0);
