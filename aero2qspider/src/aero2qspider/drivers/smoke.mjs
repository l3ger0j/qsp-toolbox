// Runs a QSP game on @qsp/wasm-engine (the engine qSpider uses), clicks random actions, objects and links
// and prints the runtime errors as JSON. Usage: node smoke.mjs GAME.qsp|GAME.qsps [steps] [seed]
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { initQspEngine } from '@qsp/wasm-engine';
import { readQsps, writeQsp } from '@qsp/converters';

const require = createRequire(import.meta.url);
const [file, stepsArg = '1500', seedArg = '1'] = process.argv.slice(2);
if (!file) {
  console.error('usage: node smoke.mjs <game.qsp|game.qsps> [steps] [seed]');
  process.exit(2);
}

let seed = Number(seedArg);
const rnd = (n) => {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed % n;
};

const wasm = fs.readFileSync(require.resolve('@qsp/wasm-engine/qsp-engine.wasm'));
const api = await initQspEngine(new Uint8Array(wasm).buffer);

let game = fs.readFileSync(file);
if (file.toLowerCase().endsWith('.qsps')) {
  game = Buffer.from(writeQsp(readQsps(game.toString('utf-8').replace(/^﻿/, ''))));
}

const state = { main: '', stats: '', actions: [], objects: [], pending: [] };
const errors = new Map();
const visited = new Map();
const views = new Set();
const sounds = new Set();
const systemCommands = new Set();
let step = 0;
let waitingTicks = 0;

api.on('main_changed', (text) => (state.main = text));
api.on('stats_changed', (text) => (state.stats = text));
api.on('actions_changed', (actions) => (state.actions = actions));
api.on('objects_changed', (objects) => (state.objects = objects));
api.on('error', (e) => {
  const key = `${e.location}#${e.actionIndex}:${e.line} ${e.description}`;
  if (!errors.has(key)) {
    errors.set(key, {
      location: e.location,
      actionIndex: e.actionIndex,
      line: e.line,
      description: e.description,
      lineSrc: e.lineSrc,
      count: 0,
      firstStep: step,
    });
  }
  errors.get(key).count++;
});
api.on('msg', (_text, closed) => state.pending.push(closed));
const INPUTS = ['1', '0', '2', '', '3'];
api.on('input', (_text, done) => state.pending.push(() => done(INPUTS[rnd(INPUTS.length)])));
api.on('menu', (items, select) => state.pending.push(() => select(rnd(items.length))));
api.on('wait', (_ms, done) => state.pending.push(done));
api.on('view', (path) => path && views.add(path));
api.on('version', (_type, done) => done('qSpider'));
api.on('open_game', (_path, _isNew, done) => done());
api.on('save_game', (_path, done) => done());
api.on('load_save', (_path, done) => done());
api.on('is_play', (_file, done) => done(false));
api.on('play_file', (path, _volume, done) => {
  sounds.add(path);
  done();
});
api.on('close_file', (_path, done) => done());
api.on('system_cmd', (cmd) => systemCommands.add(cmd));
for (const event of ['panel_visibility', 'user_input', 'timer']) api.on(event, () => {});

const settle = () => new Promise((resolve) => setImmediate(resolve));

// Answers to MSG/INPUT/MENU/WAIT resume the engine asynchronously, so wait until it settles.
async function flush() {
  for (let i = 0; i < 1000; i++) {
    await settle();
    if (!state.pending.length) {
      await settle();
      if (!state.pending.length) break;
    }
    while (state.pending.length) state.pending.shift()();
  }
}

function links(html) {
  const result = [];
  const re = /<a\s[^>]*href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi;
  let m;
  while ((m = re.exec(html))) result.push(m[1] ?? m[2] ?? m[3]);
  return result.filter((href) => /^exec:/i.test(href) || parseInt(href) > 0);
}

api.openGame(new Uint8Array(game).buffer, true);
const firstLocation = api.getLocationsList()[0];
// restartGame() produces no screen updates under Node in wasm-engine 1.6.1
const start = async () => {
  api.execCode(`KILLALL & CLS & GT '${firstLocation.replace(/'/g, "''")}'`);
  await flush();
};
await start();

// $COUNTER runs on a timer in qSpider
const tick = async () => {
  api.execCounter();
  await flush();
};

const steps = Number(stepsArg);
for (step = 1; step <= steps; step++) {
  if (rnd(3) === 0) await tick();
  const location = api.calculateStringExpression('$CURLOC') ?? '';
  visited.set(location, (visited.get(location) ?? 0) + 1);
  const choices = [];
  state.actions.forEach((_, i) =>
    choices.push(() => {
      api.selectAction(i);
      api.execSelectedAction();
    }),
  );
  state.objects.forEach((_, i) => choices.push(() => api.selectObject(i)));
  for (const href of [...links(state.main), ...links(state.stats)]) {
    if (/^exec:/i.test(href)) {
      choices.push(() => api.execCode(href.slice(5).replace(/&quot;/g, '"')));
    } else {
      const index = parseInt(href) - 1;
      choices.push(() => {
        if (index < state.actions.length) {
          api.selectAction(index);
          api.execSelectedAction();
        }
      });
    }
  }
  if (!choices.length) {
    // nothing to click: wait for timer-driven screens, then restart
    if (waitingTicks++ < 30) {
      await tick();
    } else {
      waitingTicks = 0;
      await start();
    }
    continue;
  }
  waitingTicks = 0;
  choices[rnd(choices.length)]();
  await flush();
}

console.log(
  JSON.stringify(
    {
      steps,
      errors: [...errors.values()],
      visited: Object.fromEntries(visited),
      views: [...views],
      sounds: [...sounds],
      systemCommands: [...systemCommands],
    },
    null,
    2,
  ),
);
