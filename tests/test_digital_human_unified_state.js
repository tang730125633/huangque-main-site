'use strict';
const assert=require('assert');
const state=require('../site/workbench/digital-human-unified-state.js');

let prompted=0,confirmed=0;
const automatic=state.selectCloneSlot([
  {slot_id:'ready-1',status:'ready',voice_name:'旧音色'},
  {slot_id:'empty-1',status:'active'}
],()=>{prompted++;},()=>{confirmed++;});
assert.strictEqual(automatic.slot.slot_id,'empty-1');
assert.strictEqual(prompted,0);
assert.strictEqual(confirmed,0);

assert.throws(()=>state.selectCloneSlot(
  [{slot_id:'ready-1',status:'ready',voice_name:'旧音色'}],
  ()=>'',()=>true
),/未选择要覆盖/);
assert.throws(()=>state.selectCloneSlot(
  [{slot_id:'ready-1',status:'ready',voice_name:'旧音色'}],
  ()=>'ready-1',()=>false
),/已取消覆盖/);
const overwrite=state.selectCloneSlot(
  [{slot_id:'ready-1',status:'ready',voice_name:'岳磊原音色'}],
  slots=>slots[0].slot_id,(slot,name)=>slot.slot_id==='ready-1'&&name==='岳磊原音色'
);
assert.deepStrictEqual(overwrite,{
  slot:{slot_id:'ready-1',status:'ready',voice_name:'岳磊原音色'},
  overwrite_confirmed:true,overwrite_voice_name:'岳磊原音色'
});

function cloneState(attempt,sample){return {
  voiceKey:'vip_slot-1',consent:{clone_attempt_id:attempt},sample:{sha256:sample}
};}
const first=JSON.stringify(state.audioKeyPayload(cloneState('attempt-a','a'.repeat(64)),'同一文案'));
const retry=JSON.stringify(state.audioKeyPayload(cloneState('attempt-a','a'.repeat(64)),'同一文案'));
const reclone=JSON.stringify(state.audioKeyPayload(cloneState('attempt-b','b'.repeat(64)),'同一文案'));
assert.strictEqual(first,retry,'same clone retry must keep one idempotency payload');
assert.notStrictEqual(first,reclone,'different clone must not replay the old audio job');

console.log('digital human unified state tests passed');
