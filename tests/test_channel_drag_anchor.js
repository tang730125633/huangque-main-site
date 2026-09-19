const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function harness(sortable=false,back=false){
 const events={},globalEvents={},calls=[];
 const cls={add(){},remove(){}};
 const anchor={dataset:{cmPriorityAnchor:'op'},style:{},classList:cls,getBoundingClientRect:()=>({top:0,bottom:80,height:80})};
 if(sortable){anchor.dataset.cmPriorityChannel='@original';anchor.dataset.cmPriorityOperation='op'}
 const list={querySelectorAll:()=>[anchor,row],getBoundingClientRect:()=>({left:0,right:500,top:0,bottom:170})};
 const row={dataset:{cmPriorityChannel:'candidate',cmPriorityOperation:'op'},style:{},classList:cls,parentElement:list,isConnected:true,getBoundingClientRect:()=>({left:0,top:90,bottom:170,width:500,height:80}),cloneNode:()=>({style:{},classList:cls,removeAttribute(){},setAttribute(){},remove(){}})};
 if(back){Object.assign(anchor,{parentElement:list,isConnected:true,cloneNode:row.cloneNode});anchor.getBoundingClientRect=()=>({left:0,top:90,bottom:170,width:500,height:80});row.getBoundingClientRect=()=>({left:0,top:0,bottom:80,width:500,height:80});list.querySelectorAll=()=>[row,anchor]}
 const handle={closest:()=>back?anchor:row,setPointerCapture(){},hasPointerCapture:()=>false};
 const root={addEventListener:(n,fn)=>events[n]=fn};
 const window={addEventListener:(n,fn)=>globalEvents[n]=fn};
 vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-priority-drag.js'),'utf8'),{window,document:{body:{appendChild(){}}}});
 window.ChannelPriorityDrag(root,(op,order)=>calls.push({op,order:Array.from(order)}),()=>false);
 events.pointerdown({target:{closest:()=>handle},button:0,pointerId:1,clientY:100,preventDefault(){}});
 return {events,globalEvents,calls};
}
test('单候选可拖越只读原线路，提交仅包含托管ID',()=>{
 const h=harness();h.events.pointermove({pointerId:1,clientY:0});h.events.pointerup({pointerId:1,clientX:20,clientY:0});assert.deepEqual(h.calls,[{op:'op',order:['candidate']}]);
});
test('单候选取消或滚动不会提交',()=>{
 for(const cancel of ['pointercancel','scroll']){const h=harness();h.events.pointermove({pointerId:1,clientY:0});(cancel==='scroll'?h.globalEvents.scroll:h.events.pointercancel)();assert.equal(h.calls.length,0)}
});
test('未越过原主线路不申请接管',()=>{
 const h=harness();h.events.pointermove({pointerId:1,clientY:105});h.events.pointerup({pointerId:1,clientX:20,clientY:105});assert.equal(h.calls.length,0);
});


test('原厂参与同一排序，第二条拖首位后原厂保留第二位',()=>{
 const h=harness(true);h.events.pointermove({pointerId:1,clientY:0});h.events.pointerup({pointerId:1,clientX:20,clientY:0});
 assert.deepEqual(h.calls,[{op:'op',order:['candidate','@original']}]);
});


test('将第二位原厂拖回首位，托管线路保留在第二位',()=>{
 const h=harness(true,true);h.events.pointermove({pointerId:1,clientY:0});h.events.pointerup({pointerId:1,clientX:20,clientY:0});
 assert.deepEqual(h.calls,[{op:'op',order:['@original','candidate']}]);
});
