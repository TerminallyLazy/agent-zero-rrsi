// Run with Node.js; tests the shipped store without installing dependencies.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../webui/settings-store.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export const store =', 'globalThis.store =');
const pending = [];
const context = vm.createContext({
    createStore: (_, store) => store,
    API: {callJsonApi: () => new Promise((resolve, reject) => pending.push({resolve, reject}))},
    toastFrontendError: () => {}, toastFrontendSuccess: () => {},
});
vm.runInContext(source, context);
const store = context.store;
const image = {id: 'sha256:' + 'a'.repeat(64), tags: ['agent0ai/agent-zero:latest'], name: 'agent0ai/agent-zero:latest', recommended: true, selectable: true, agent_zero: true};
const answer = (images = [image]) => ({success: true, data: {images, availability: 'ready', reason: null, truncated: false}});
const config = {framework_image: image.tags[0]};
const first = store.loadImages();
assert.equal(store.imagesLoading, true);
pending.shift().resolve(answer()); await first;
assert.equal(store.imagesLoading, false);
assert.equal(store.imageSelection(config), image.id);
assert.equal(config.framework_image, image.tags[0], 'Loading preserves saved tag');
assert.equal(store.imageSelection({framework_image: 'custom/image:local'}), 'custom/image:local');
assert.equal(store.imageSelection({framework_image: ''}), '', 'Loading never silently selects an image');
assert.equal(store.groupedImages('recommended').length, 1);
assert.match(store.imageLabel({...image, current_instance: true}), /^Current Agent Zero image · Recommended/);

const older = store.loadImages(); const newer = store.loadImages();
const olderReply = pending.shift(); pending.shift().resolve(answer([])); await newer;
olderReply.resolve(answer()); await older;
assert.equal(store.images.length, 0, 'Stale refresh cannot replace new data');
assert.equal(config.framework_image, image.tags[0]);

const failed = store.loadImages(); pending.shift().reject(new Error('unavailable')); await failed;
assert.equal(store.imageAvailability, 'unavailable');
assert.equal(config.framework_image, image.tags[0]);
const closed = store.loadImages(); store.alive = false; pending.shift().resolve(answer()); await closed;
assert.equal(store.images.length, 0, 'Closed view ignores late response');
console.log('Image settings: selection preservation, empty/custom references, grouping, refresh races, failure and closed-view checks passed.');
