import json
import shutil
import subprocess
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12" / "templates" / "index.html"
SERVER = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12" / "server.py"


class IP12AgentProductionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.server = SERVER.read_text(encoding="utf-8")

    def test_resizable_panel_has_desktop_bounds_and_accessible_controls(self):
        self.assertIn("--production-panel-width:440px", self.html)
        self.assertIn("min:360,max:Math.min(720,Math.floor(window.innerWidth*.5))", self.html)
        self.assertIn('id="productionPanelResizer"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn("beginProductionResize(event)", self.html)
        self.assertIn("resizeProductionByKey(event)", self.html)
        self.assertIn("localStorage.setItem(productionStorageKey(),next)", self.html)
        self.assertIn("localStorage.getItem(productionStorageKey())", self.html)
        self.assertIn("@media(max-width:1100px){\n  .rpn.open{position:fixed", self.html)
        self.assertIn("@media(max-width:720px)", self.html)

    def test_one_contextual_panel_renders_all_four_result_shapes(self):
        start = self.html.index("function renderProductionPanel")
        panel = self.html[start:self.html.index("function restoreProductionPanel()", start)]
        self.assertIn("来源版本", panel)
        self.assertIn("为什么推荐", panel)
        self.assertIn("素材与参数", panel)
        self.assertIn("实时报价", panel)
        self.assertIn("本次消耗", panel)
        self.assertIn("当前余额", panel)
        self.assertIn("任务与结果", panel)
        result = self.html[self.html.index("function productionResultHtml"):self.html.index("function productionOptionsHtml")]
        self.assertIn("<img", result)
        self.assertIn("<video controls", result)
        self.assertIn("<audio controls", result)
        self.assertIn("打开 Canvas", result)
        self.assertIn("下载", result)
        self.assertIn("function productionTaskLabel", self.html)
        self.assertNotIn("record.job_id", panel)
        self.assertIn("productionProgressHtml(record)+productionResultHtml(record)", panel)

    def test_only_the_quote_card_exposes_the_paid_confirmation(self):
        confirm = self.html[self.html.index("async function confirmProduction"):self.html.index("async function refreshProduction")]
        self.assertIn("/api/ip12/productions/confirm", confirm)
        self.assertIn("productionHasValidQuote(record)", confirm)
        quote_card = self.html[self.html.index("if(quoted){"):self.html.index("html+='<div class=\"rpn-card\"><div class=\"rpn-card-header\">任务与结果")]
        self.assertIn('data-production-quote-card="true"', quote_card)
        self.assertIn('data-production-confirm="true"', quote_card)
        self.assertIn("确认并提交这次生产", quote_card)
        self.assertNotIn('onclick="confirmProduction()"', self.html)
        self.assertEqual(
            self.html.count('onclick="confirmProduction(this.dataset.productionId)"'), 2,
        )
        quote_guard = self.html[self.html.index("function productionHasValidQuote"):self.html.index("function renderProductionPanel")]
        self.assertIn("record.status!=='quoted'", quote_guard)
        self.assertIn("productionUnfilledFields", quote_guard)
        actions = self.html[self.html.index("function runStateAction"):self.html.index("function attachHarnessActions")]
        self.assertIn("if(item.type==='confirm_paid_job'){", actions)
        self.assertNotIn("confirmProduction()", actions)
        message = self.html[self.html.index("function sendMessage"):self.html.index("async function sendTurn")]
        self.assertIn("var turn={message:text}", message)
        self.assertNotIn("confirmProduction", message)
        continuation = self.html[self.html.index("async function sendJumpMsg"):self.html.index("function renderProjectPanel")]
        self.assertNotIn("confirmProduction", continuation)

    def test_terminal_inline_status_never_reports_failure_as_completed(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function productionTerminalNoteHtml")
        end = self.html.index("function productionSpecialist", start)
        script = self.html[start:end] + r"""
const statuses=['done','failed','refund_pending','refunded','unknown'];
console.log(JSON.stringify(Object.fromEntries(
  statuses.map(status=>[status,productionTerminalNoteHtml({status})])
)));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertIn("制作已完成", got["done"])
        self.assertIn("制作失败", got["failed"])
        self.assertNotIn("制作已完成", got["failed"])
        self.assertIn("退款正在处理中", got["refund_pending"])
        self.assertIn("点数已退回", got["refunded"])
        self.assertEqual(got["unknown"], "")

    def test_expired_quote_becomes_stale_without_a_submit_request(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        quote_start = self.html.index("function productionQuote")
        quote_end = self.html.index("function renderProductionPanel", quote_start)
        confirm_start = self.html.index("async function confirmProduction")
        confirm_end = self.html.index("async function refreshProduction", confirm_start)
        script = self.html[quote_start:quote_end] + self.html[confirm_start:confirm_end] + r"""
let cid='project-1',state={revision:7},activeProductionId='production-1';
let productions={'production-1':{
  id:'production-1',status:'quoted',options:{},
  quote:{cost:90,points:94359,expires_at:1}
}};
let requests=0,messages=0,renders=0,toasts=[];
function productionUnfilledFields(){return []}
function rememberProduction(record){productions[record.id]=record;return record}
function refreshProductionMessages(){messages+=1}
function renderProductionPanel(){renders+=1}
function toast(message){toasts.push(message)}
function newTurnRequestId(){return 'confirm-1'}
async function productionRequest(){requests+=1;return{}}
function updateProductionFromPayload(){}
async function refreshProduction(){}
global.document={
  querySelectorAll:()=>[],
  getElementById:()=>({classList:{contains:()=>true}})
};
(async()=>{
  await confirmProduction('production-1');
  console.log(JSON.stringify({
    status:productions['production-1'].status,
    quote:productions['production-1'].quote,
    requests,messages,renders,toasts
  }));
})();
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got["status"], "stale")
        self.assertEqual(got["quote"], {})
        self.assertEqual(got["requests"], 0)
        self.assertEqual(got["messages"], 1)
        self.assertEqual(got["renders"], 1)
        self.assertEqual(got["toasts"], ["报价已失效，请重新获取后再确认。"])

    def test_missing_and_schema_render_typed_controls_and_gate_quote(self):
        controls = self.html[self.html.index("function productionParameterSchema"):self.html.index("function productionQuote")]
        self.assertIn("record.parameter_schema||record.schema||record.input_schema", controls)
        self.assertIn("record.missing_prerequisites||record.missing", controls)
        self.assertIn('data-production-option="true"', controls)
        self.assertIn('type="number"', controls)
        self.assertIn("rememberProductionOption(event)", controls)
        panel_start = self.html.index("function renderProductionPanel")
        panel = self.html[panel_start:self.html.index("function restoreProductionPanel()", panel_start)]
        self.assertIn('data-production-quote="true"', panel)
        self.assertIn("unfilled.length?' disabled'", panel)
        self.assertNotIn("missing.map", panel)

    def test_prepare_and_quote_submit_typed_options(self):
        prepare = self.html[self.html.index("async function prepareProduction"):self.html.index("async function requestProductionQuote")]
        self.assertIn("options=typedProductionOptions(item||{},item&&item.options||{})", prepare)
        self.assertIn("preferred_action:item&&item.preferred_action,specialist_agent:item&&item.specialist_agent,reuse_production_id:item&&item.reuse_production_id,allow_system_media:item&&item.allow_system_media===true", prepare)
        self.assertIn("options:options", prepare)
        quote = self.html[self.html.index("async function requestProductionQuote"):self.html.index("async function confirmProduction")]
        self.assertIn("var collected=collectProductionOptions(record)", quote)
        self.assertIn("if(collected.missing.length)", quote)
        self.assertIn("options:collected.options", quote)
        self.assertIn("detail.code==='missing_prerequisite'", quote)

    def test_voice_choices_preload_duration_metadata(self):
        controls = self.html[
            self.html.index("function productionChoiceCards"):
            self.html.index("function productionFieldControl")
        ]
        self.assertIn('preload="metadata"', controls)
        self.assertNotIn('preload="none"', controls)

    def test_agent_messages_and_top_bar_can_reopen_the_current_production(self):
        self.assertIn('id="productionEntryBtn"', self.html)
        self.assertIn('onclick="openCurrentProduction()"', self.html)
        self.assertIn('class="top-action production-entry"', self.html)
        message = self.html[
            self.html.index("function productionMessageHtml"):
            self.html.index("function isSafeMarkdownUrl")
        ]
        self.assertIn('class="production-inline"', message)
        self.assertIn("productionInlineHtml(record,messageId)", message)
        self.assertIn("refreshProductionMessages", message)
        self.assertIn("openProductionRecord(this.dataset.productionId)", self.html)
        lifecycle = self.html[
            self.html.index("function updateProductionEntry"):
            self.html.index("function productionError")
        ]
        self.assertIn("button.hidden=!record", lifecycle)
        self.assertIn("updateProductionEntry()", lifecycle)
        self.assertIn("function openProductionRecord", self.html)
        self.assertIn("function openCurrentProduction", self.html)

    def test_four_current_missing_fields_are_typed_without_action_specific_flows(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function productionParameterSchema")
        end = self.html.index("function productionDraftKey", start)
        functions = self.html[start:end]
        script = functions + r"""
const cases = [
  {kind:'image', field:'prompt', type:'string', raw:'品牌封面插画'},
  {kind:'audio', field:'text', type:'string', raw:'欢迎来到黄雀'},
  {kind:'video', field:'avatar_id', type:'integer', raw:'42'},
  {kind:'canvas', field:'prompt', type:'string', raw:'整理为内容画布'}
];
const result = cases.map(item => {
  const record = {
    missing_prerequisites:[item.field],
    parameter_schema:{
      type:'object',
      properties:{[item.field]:{type:item.type}},
      required:[item.field]
    },
    options:{}
  };
  const spec = productionFieldSpecs(record)[0];
  const before = productionUnfilledFields(record, record.options);
  const options = typedProductionOptions(record, {[item.field]:item.raw});
  const after = productionUnfilledFields(record, options);
  return {kind:item.kind, field:spec.name, type:spec.type, value:options[item.field], before, after};
});
console.log(JSON.stringify(result));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual([item["field"] for item in got], ["prompt", "text", "avatar_id", "prompt"])
        self.assertEqual([item["type"] for item in got], ["string", "string", "integer", "string"])
        self.assertEqual(got[2]["value"], 42)
        self.assertTrue(all(item["before"] for item in got))
        self.assertTrue(all(item["after"] == [] for item in got))

    def test_voice_name_preview_key_and_quote_value_stay_bound(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        helpers_start = self.html.index("function productionParameterSchema")
        helpers_end = self.html.index("function productionDraftKey", helpers_start)
        collect_start = self.html.index("function collectProductionOptions")
        collect_end = self.html.index("function updateProductionQuoteGate", collect_start)
        functions = self.html[helpers_start:helpers_end] + self.html[collect_start:collect_end]
        script = functions + r"""
const record={
  options:{}, missing_prerequisites:['voice'],
  parameter_schema:{type:'object',required:['voice'],properties:{voice:{type:'string',oneOf:[
    {const:'S_pa0E8OR62',title:'沉稳男声（知识口播）',preview_url:'https://media.example/calm.mp3',preview_kind:'audio',source:'public'},
    {const:'S_xaUB8OR62',title:'亲和女声（本地生活）',preview_url:'https://media.example/friendly.mp3',preview_kind:'audio',source:'public'}
  ]}}}
};
const calm=productionFieldSpec(record,'voice').choices[0];
record.options=typedProductionOptions(record,{voice:calm.value});
global.document={querySelectorAll:()=>[{dataset:{field:'voice'},value:record.options.voice}]};
console.log(JSON.stringify({
  selectedKey:record.options.voice,
  selectedLabel:productionDisplayValue(record,'voice',record.options.voice),
  selectedPreview:calm.previewUrl,
  quoteOptions:collectProductionOptions(record).options
}));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got, {
            "selectedKey": "S_pa0E8OR62",
            "selectedLabel": "沉稳男声（知识口播）",
            "selectedPreview": "https://media.example/calm.mp3",
            "quoteOptions": {"voice": "S_pa0E8OR62"},
        })

    def test_prepare_and_quote_runtime_bodies_keep_typed_options_and_missing_gate(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        helpers_start = self.html.index("function productionParameterSchema")
        helpers_end = self.html.index("function productionDraftKey", helpers_start)
        collect_start = self.html.index("function collectProductionOptions")
        collect_end = self.html.index("function updateProductionQuoteGate", collect_start)
        request_start = self.html.index("async function productionRequest")
        request_end = self.html.index("async function confirmProduction", request_start)
        functions = "\n".join((
            self.html[helpers_start:helpers_end],
            self.html[collect_start:collect_end],
            self.html[request_start:request_end],
        ))
        script = functions + r"""
let cid='conversation-1', state={revision:9}, activeContentTarget=null;
let activeProductionId='production-1';
let productions={
  'production-1':{
    id:'production-1', status:'blocked_prerequisite', options:{},
    missing_prerequisites:['avatar_id'],
    parameter_schema:{type:'object',properties:{avatar_id:{type:'integer'}},required:['avatar_id']}
  }
};
let fieldNodes=[{dataset:{field:'avatar_id'},value:'42'}], calls=[];
global.document={
  querySelectorAll:(selector)=>selector.includes('.harness-actions')?[]:fieldNodes,
  getElementById:()=>({innerHTML:''})
};
global.fetch=async (url,init)=>{
  calls.push({url,body:init.body?JSON.parse(init.body):null});
  const data=url.endsWith('/quote')
    ? {ok:true,production_id:'production-1',status:'quoted',cost:1,points:1}
    : {ok:true,production_id:'production-2',status:'blocked_prerequisite',options:{avatar_id:7}};
  return {ok:true,status:200,json:async()=>data};
};
function rememberProduction(record){productions[record.id]=record;return record}
function refreshProductionMessages(){}
function updateProductionQuoteGate(){}
function updateProductionFromPayload(data){
  const id=data.id||data.production_id;
  const record=Object.assign({},productions[id]||{},data,{id});
  productions[record.id]=record;activeProductionId=record.id;return record;
}
function openPanel(){}
function toast(){}
function newTurnRequestId(){return 'turn-fixture-1'}
(async()=>{
  await requestProductionQuote();
  const quoteCall=calls[calls.length-1];
  await prepareProduction({
    content_target:{topic_id:'topic-1'}, requested_result:'video',
    options:{avatar_id:'7'},
    parameter_schema:{type:'object',properties:{avatar_id:{type:'integer'}},required:['avatar_id']}
  });
  const prepareCall=calls[calls.length-1];
  activeProductionId='production-1';
  const beforeBlocked=calls.length;
  productions['production-1'].options={};
  fieldNodes=[{dataset:{field:'avatar_id'},value:''}];
  await requestProductionQuote();
  console.log(JSON.stringify({
    quote:quoteCall.body.options,
    prepare:prepareCall.body.options,
    missingPreventedFetch:calls.length===beforeBlocked
  }));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got["quote"], {"avatar_id": 42})
        self.assertEqual(got["prepare"], {"avatar_id": 7})
        self.assertTrue(got["missingPreventedFetch"])

    def test_agent_auto_runs_prepare_quotes_then_polls_one_job_and_delivers_in_chat(self):
        prepare = self.html[self.html.index("async function prepareProduction"):self.html.index("async function requestProductionQuote")]
        self.assertIn("await requestProductionQuote(record.id,false)", prepare)
        self.assertNotIn("openPanel('生产画布')", prepare)
        send = self.html[self.html.index("async function sendTurn"):self.html.index("async function sendJumpMsg")]
        self.assertIn("item.type==='prepare_production'", send)
        self.assertIn("else if(autoAction)await runStateAction(autoAction)", send)
        polling = self.html[self.html.index("function stopProductionPoll"):self.html.index("function productionRoute")]
        self.assertIn("['submitting','queued','running','verifying','refund_pending']", polling)
        self.assertIn("if(!runId||!ongoing.includes(status))", polling)
        self.assertIn("latest&&ongoing.includes(String(latest.status||''))", polling)
        self.assertIn("refreshProduction(true,record.id)", polling)
        self.assertNotIn("confirmProduction(record.id)", polling)
        chat = self.html[self.html.index("function productionMessageHtml"):self.html.index("function isSafeMarkdownUrl")]
        self.assertIn("data-production-message", chat)
        self.assertIn("production-inline", chat)
        result = self.html[self.html.index("function productionResultHtml"):self.html.index("function productionFieldControl")]
        self.assertIn("continueProductionRevision(this.dataset.productionId)", result)

    def test_prepare_and_quote_update_the_same_chat_reply(self):
        chat = self.html[
            self.html.index("function appendProductionMessage"):
            self.html.index("function isSafeMarkdownUrl")
        ]
        self.assertIn(".find(function(node)", chat)
        self.assertIn("existing.innerHTML", chat)
        payload = self.html[
            self.html.index("function updateProductionFromPayload"):
            self.html.index("function collectProductionOptions")
        ]
        self.assertIn("payload.material_request_message", payload)
        prepare = self.html[
            self.html.index("async function prepareProduction"):
            self.html.index("async function requestProductionQuote")
        ]
        self.assertIn("reply_message_id:item&&item.reply_message_id", prepare)
        self.assertIn("requested_avatar_name:item&&item.requested_avatar_name", prepare)
        send = self.html[
            self.html.index("async function sendTurn"):
            self.html.index("async function sendJumpMsg")
        ]
        self.assertIn("data.assistant_message_id", send)

    def test_restore_and_direct_navigation_keep_conversation_context(self):
        select = self.html[self.html.index("async function selectConvo"):self.html.index("async function jumpModule")]
        self.assertIn("productions=productionMap(c.productions)", select)
        self.assertIn("activeProductionId=restoreProductionId()", select)
        self.assertIn("restoreProductionPanel()", select)
        restore = self.html[self.html.index("function productionDraftKey"):self.html.index("function productionError")]
        self.assertIn("ip12-production-draft:", restore)
        self.assertIn("parameter_schema:productionParameterSchema(record)", restore)
        self.assertIn("missing_prerequisites:productionMissing(record)", restore)
        self.assertIn("if(Array.isArray(value))", restore)
        navigation = self.html[self.html.index("function navigateToProductionRoute"):self.html.index("function openProductionCanvas")]
        self.assertIn("sessionStorage.setItem('ip12-production-return'", navigation)
        self.assertIn("url.searchParams.set('conversation_id',cid||'')", navigation)
        self.assertIn("url.searchParams.set('project_id',cid||'')", navigation)
        self.assertIn("url.searchParams.set('return_to',location.pathname+location.search)", navigation)
        route = self.html[self.html.index("function productionRoute"):self.html.index("function continueProductionRevision")]
        self.assertIn("if(!value)return null", route)
        self.assertIn("'/workbench/canvas?collab='+encodeURIComponent(canvas.board_id)", route)

    def test_unquoted_local_field_draft_restores_but_cannot_override_server_quote(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function productionParameterSchema")
        end = self.html.index("function saveProductionDraft", start)
        functions = self.html[start:end]
        script = functions + r"""
var cid='conversation-1';
const saved=JSON.stringify({
  options:{prompt:'本地未报价草稿'},
  parameter_schema:{type:'object',properties:{prompt:{type:'string'}},required:['prompt']},
  missing_prerequisites:['prompt']
});
global.localStorage={getItem:()=>saved};
const blocked=restoreProductionDraft({id:'production-1',status:'blocked_prerequisite',options:{prompt:''}});
const quoted=restoreProductionDraft({id:'production-1',status:'quoted',options:{prompt:'服务端已报价版本'}});
console.log(JSON.stringify({
  blockedValue:blocked.options.prompt,
  blockedMissing:blocked.missing_prerequisites,
  quotedValue:quoted.options.prompt,
  quotedMissing:quoted.missing_prerequisites||[]
}));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got["blockedValue"], "本地未报价草稿")
        self.assertEqual(got["blockedMissing"], ["prompt"])
        self.assertEqual(got["quotedValue"], "服务端已报价版本")
        self.assertEqual(got["quotedMissing"], [])

    def test_production_errors_are_safe_user_messages_not_server_details(self):
        source = self.html[self.html.index("function productionError"):self.html.index("function productionRoute")]
        self.assertIn("暂时无法读取生产状态，请稍后再试。", source)
        self.assertNotIn("data.error", source)
        self.assertNotIn("e.message", source)
        self.assertNotIn("detail.error", source)
        self.assertNotIn("安全整理", self.html)

    def test_generic_array_and_object_fields_require_valid_json(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function productionParameterSchema")
        end = self.html.index("function productionDraftKey", start)
        quote_start = self.html.index("function productionQuote")
        quote_end = self.html.index("function renderProductionPanel", quote_start)
        functions = self.html[start:end] + self.html[quote_start:quote_end]
        script = functions + r"""
const record = {
  missing_prerequisites:['upload_ids', 'settings'], options:{},
  parameter_schema:{type:'object', properties:{
    upload_ids:{type:'array'}, settings:{type:'object'}
  }, required:['upload_ids', 'settings']}
};
const specs = productionFieldSpecs(record);
const invalid = typedProductionOptions(record, {upload_ids:'[not json]', settings:'{"quality":"high"}'});
const valid = typedProductionOptions(record, {upload_ids:'["up_1","up_2"]', settings:'{"quality":"high"}'});
console.log(JSON.stringify({
  types:specs.map(spec => spec.type), invalid:productionUnfilledFields(record, invalid),
  valid:productionUnfilledFields(record, valid), values:valid,
  confirmBlocked:productionHasValidQuote(Object.assign({}, record, {status:'quoted', cost:1, options:invalid}))
}));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got["types"], ["array", "object"])
        self.assertEqual(got["invalid"], ["upload_ids"])
        self.assertEqual(got["valid"], [])
        self.assertEqual(got["values"], {"upload_ids": ["up_1", "up_2"], "settings": {"quality": "high"}})
        self.assertFalse(got["confirmBlocked"])
        self.assertIn("JSON.parse", functions)
        self.assertNotIn("eval(", functions)

    def test_generic_action_results_are_escaped_and_upload_prerequisites_use_chat_upload(self):
        result = self.html[self.html.index("function productionActionResultHtml"):self.html.index("function productionFieldControl")]
        self.assertIn("record&&record.action_result", result)
        self.assertIn("<pre>", result)
        self.assertIn("eHtml(text.slice(0,4000))", result)
        controls = self.html[self.html.index("function productionUiRoute"):self.html.index("function productionOptionsHtml")]
        self.assertIn("productionUploadPrerequisite", controls)
        self.assertIn("spec.uploadKind", controls)
        self.assertIn("material-upload-btn", controls)
        self.assertIn("openProductionUpload(this.dataset.productionId,this.dataset.uploadField)", controls)
        self.assertNotIn("前往对应功能页准备素材", controls)
        navigation = self.html[self.html.index("function navigateToProductionRoute"):self.html.index("function openProductionCanvas")]
        self.assertIn("conversation_id", navigation)
        self.assertIn("project_id", navigation)
        self.assertIn("return_to", navigation)

    def test_chat_material_upload_is_explicit_bound_and_never_confirms_a_paid_job(self):
        self.assertIn('id="materialInput" type="file" hidden', self.html)
        self.assertIn('id="attachBtn"', self.html)
        self.assertIn('aria-label="上传 Agent 请求的图片、视频或音频素材"', self.html)
        upload = self.html[
            self.html.index("function pendingProductionUpload"):
            self.html.index("async function productionRequest")
        ]
        self.assertIn("new FormData()", upload)
        self.assertIn("/api/ip12/productions/upload", upload)
        self.assertIn("conversation_id", upload)
        self.assertIn("production_id", upload)
        self.assertIn("expected_revision", upload)
        self.assertIn("field", upload)
        self.assertIn("appendProductionMessage(data.material_message)", upload)
        self.assertNotIn("confirmProduction", upload)
        prepare = self.html[
            self.html.index("async function prepareProduction"):
            self.html.index("async function requestProductionQuote")
        ]
        self.assertIn("appendProductionMessage(data.material_request_message)", prepare)
        self.assertIn("^aud_", self.html)
        self.assertIn("audio/mpeg,audio/wav", self.html)
        self.assertIn("当前没有等待上传的图片、视频或音频", self.html)

    def test_digital_human_materials_stay_inside_ip12(self):
        self.assertIn('"x-hq-inline-upload-field": "image_upload_id"', self.server)
        self.assertIn('"x-hq-inline-upload-field": "audio_upload_id"', self.server)
        self.assertIn('"x-hq-switch-action": "digital-ip-audio-generate"', self.server)
        self.assertNotIn('"x-hq-upload-route": "/workbench/digital-ip"', self.server)
        self.assertNotIn('"x-hq-upload-route": "/workbench/audio"', self.server)

    def test_user_media_choices_render_preview_and_audition_cards(self):
        controls = self.html[
            self.html.index("function productionFieldSpec"):
            self.html.index("function productionOptionsHtml")
        ]
        self.assertIn("preview_url", controls)
        self.assertIn("preview_kind", controls)
        self.assertIn("productionChoiceCards", controls)
        self.assertIn('role="radiogroup"', controls)
        self.assertIn("<img", controls)
        self.assertIn("<audio controls", controls)
        self.assertIn("我的素材", controls)
        self.assertIn("公共素材", controls)
        self.assertIn("x-hq-upload-route", self.html)
        self.assertIn("rememberProductionChoice(event)", self.html)

    def test_agent_recommends_media_and_quotes_inside_the_conversation(self):
        self.assertIn("def _production_recommended_options", self.server)
        self.assertIn('choice["recommended"] = True', self.server)
        inline = self.html[
            self.html.index("function productionInlineHtml"):
            self.html.index("function productionUiRoute")
        ]
        self.assertNotIn("Object.assign({},spec,{inlineUploadField:''})", inline)
        self.assertIn("productionFieldControl(record,spec,false,3)", inline)
        self.assertIn("录一段现在的声音", self.html)
        self.assertIn("上传已有样音", self.html)
        self.assertIn("navigator.mediaDevices.getUserMedia", self.html)
        self.assertIn("encodeVoiceCloneWav", self.html)
        self.assertIn("使用这段录音生成克隆声音", self.html)
        self.assertIn("/api/ip12/productions/clone-voice", self.html)
        self.assertIn(",false,3)", inline)
        self.assertIn("实时报价", inline)
        self.assertIn("确认并提交这次生产", inline)
        self.assertIn("查看全部素材", inline)

    def test_voice_recorder_encodes_mono_pcm_as_wav(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function writeVoiceCloneWavString")
        end = self.html.index("function releaseVoiceRecorderCapture", start)
        script = self.html[start:end] + r"""
(async function(){
  const assert=require('assert');
  const blob=encodeVoiceCloneWav([new Float32Array([-1,0,1])],16000);
  const data=Buffer.from(await blob.arrayBuffer());
  assert.equal(blob.type,'audio/wav');
  assert.equal(data.subarray(0,4).toString(),'RIFF');
  assert.equal(data.subarray(8,12).toString(),'WAVE');
  assert.equal(data.readUInt16LE(22),1);
  assert.equal(data.readUInt32LE(24),16000);
  assert.equal(data.length,50);
  console.log('VOICE_WAV_OK');
})().catch(function(error){console.error(error);process.exit(1)});
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VOICE_WAV_OK", result.stdout)
        cards = self.html[
            self.html.index("function productionChoiceCards"):
            self.html.index("function productionOptionsHtml")
        ]
        self.assertIn("Agent 推荐", cards)
        self.assertIn("<audio controls", cards)
        self.assertIn("limit&&choices.length>limit", cards)
        clone_poll = self.html[
            self.html.index("function stopVoiceClonePoll"):
            self.html.index("async function uploadSelectedMaterial")
        ]
        self.assertIn("voiceClonePolls", clone_poll)
        self.assertIn("poll.inFlight||poll.timer", clone_poll)
        self.assertIn("error.status!==429&&error.status<500", clone_poll)
        self.assertIn("stopVoiceClonePoll(productionId)", clone_poll)
        self.assertIn("'POST',{conversation_id:cid}", clone_poll)
        restore = self.html[
            self.html.index("function restoreProductionPanel(){"):
            self.html.index("function openProductionRecord")
        ]
        self.assertNotIn("renderProductionPanel(record)", restore)
        self.assertIn("requestProductionQuote(record.id,false)", restore)

    def test_voice_clone_poll_deduplicates_retries_and_stops_terminal_states(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        start = self.html.index("function stopVoiceClonePoll")
        end = self.html.index("async function uploadSelectedMaterial", start)
        functions = self.html[start:end]
        script = functions + r"""
const assert = require('assert');
var voiceClonePolls={},cid='project-a',productions={p:{id:'p',options:{}}};
var timers=[],requests=0,quotes=0,mode='ready';
global.setTimeout=function(fn){timers.push(fn);return timers.length};
global.clearTimeout=function(){};
function updateProductionFromPayload(data){productions.p=data.production||productions.p;return productions.p}
function appendProductionMessage(){}
function toast(){}
function productionUnfilledFields(){return []}
function productionUnmappedMissing(){return []}
async function requestProductionQuote(){quotes+=1}
async function productionRequest(){requests+=1;if(mode==='429'){var e=new Error('limited');e.status=429;throw e}if(mode==='400'){var bad=new Error('bad');bad.status=400;throw bad}return{status:mode,production:{id:'p',options:{}}}}
(async function(){
  await Promise.all([pollVoiceClone('p'),pollVoiceClone('p')]);
  assert.equal(requests,1);assert.equal(quotes,1);assert.equal(voiceClonePolls.p,undefined);
  requests=0;quotes=0;mode='429';await pollVoiceClone('p');
  assert.equal(requests,1);assert.equal(timers.length,1);assert.ok(voiceClonePolls.p);
  mode='failed';timers.shift()();await new Promise(setImmediate);
  assert.equal(requests,2);assert.equal(voiceClonePolls.p,undefined);
  mode='400';await pollVoiceClone('p');
  assert.equal(voiceClonePolls.p,undefined);
  console.log('VOICE_CLONE_POLL_OK');
})().catch(function(error){console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VOICE_CLONE_POLL_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
