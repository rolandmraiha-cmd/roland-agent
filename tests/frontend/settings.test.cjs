const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tagName=tag.toUpperCase(); this.children=[]; this.value=''; this.textContent=''; this.disabled=false; this.attributes={}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(key,value) { this.attributes[key]=value; }
  querySelector(selector) { return this.children.find(child => '.'+child.className===selector); }
  set innerHTML(_) { throw new Error('Dynamic settings must use textContent'); }
}
function fixture() {
  const nodes=new Map(), calls=[];
  const get=id=>{ if(!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id); };
  let now=10000;
  const context=vm.createContext({
    window:{}, $:get, el:(tag,cls,text)=>{const element=new Element(tag);element.className=cls;element.textContent=text||'';return element;},
    fmtTime:()=> 'time', URLSearchParams, csrf:'test', currentChat:1, loadJobs:async()=>{}, loadStatus:async()=>{}, showView:()=>{},
    Date:{now:()=>now},
    api:async(url,options={})=>{calls.push({url,options});return {json:async()=>({captured:false})};},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../agent/web/static/settings.js'),'utf8'),context);
  return {context,get,calls,advance:ms=>now+=ms, element:()=>new Element()};
}
test('off feedback says not captured and sends the exact human rating',async()=>{
  const f=fixture(), node=f.element(); f.context.window.m8.feedback(node,42,null);
  const row=node.children[0]; await row.children[0].onclick();
  assert.equal(row.children[3].textContent,'Not captured');
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{rating:1});
  assert.equal(f.calls[0].url,'/api/messages/42/feedback');
});
test('used feedback is visibly locked',()=>{
  const f=fixture(),node=f.element();f.context.window.m8.feedback(node,42,{used_in_dataset:'data-1',rating:-1});
  const row=node.children[0];assert.match(row.children[3].textContent,/locked/);
  assert.ok(row.children.slice(0,3).every(button=>button.disabled));
  assert.equal(row.children[1].attributes['aria-pressed'],'true');
});
test('a dataset locking a vote during editing keeps buttons disabled',async()=>{
  const f=fixture(),node=f.element();f.context.api=async()=>{const error=new Error('Used in dataset');error.status=409;throw error;};
  f.context.window.m8.feedback(node,42,null);await node.children[0].children[0].onclick();
  assert.ok(node.children[0].children.slice(0,3).every(button=>button.disabled));
});
test('clearing a thumbs-up unpresses both thumbs without a refresh',async()=>{
  const f=fixture(),node=f.element();f.context.window.m8.feedback(node,42,null);
  const [up,down,clear,state]=node.children[0].children;await up.onclick();
  assert.equal(up.attributes['aria-pressed'],'true');
  await clear.onclick();
  assert.equal(f.calls.at(-1).options.method,'DELETE');assert.equal(f.calls.at(-1).url,'/api/messages/42/feedback');
  assert.equal(up.attributes['aria-pressed'],'false');assert.equal(down.attributes['aria-pressed'],'false');
  assert.equal(state.textContent,'Vote cleared');
});
test('clearing a saved thumbs-down unpresses both thumbs and hides the editor',async()=>{
  const f=fixture(),node=f.element();f.context.window.m8.feedback(node,42,{rating:-1,correction:'Four.'});
  const [up,down,clear,state,editor,error]=node.children[0].children;
  assert.equal(down.attributes['aria-pressed'],'true');editor.hidden=false;error.textContent='Earlier error';
  await clear.onclick();
  assert.equal(up.attributes['aria-pressed'],'false');assert.equal(down.attributes['aria-pressed'],'false');
  assert.equal(editor.hidden,true);assert.equal(state.textContent,'Vote cleared');assert.equal(error.textContent,'');
});
test('a failed clear keeps the saved vote pressed and shows the error',async()=>{
  const f=fixture(),node=f.element();f.context.window.m8.feedback(node,42,{rating:-1,correction:'Four.'});
  const [up,down,clear,state,editor,error]=node.children[0].children;editor.hidden=false;
  f.context.api=async()=>{throw new Error('Could not clear the vote');};
  await clear.onclick();
  assert.equal(down.attributes['aria-pressed'],'true');assert.equal(up.attributes['aria-pressed'],'false');
  assert.equal(error.textContent,'Could not clear the vote');
  assert.equal(editor.hidden,false);assert.notEqual(state.textContent,'Vote cleared');
});
test('a pressed thumb has its own visible style',()=>{
  const css=fs.readFileSync(path.join(__dirname,'../../agent/web/static/style.css'),'utf8');
  const rule=css.match(/\.feedback button\[aria-pressed="true"\]\s*\{([^}]*)\}/);
  assert.ok(rule,'style.css must style .feedback button[aria-pressed="true"]');
  assert.match(rule[1],/background:/);assert.match(rule[1],/border-color:/);
});
test('a saved vote keeps its capture label after a reload',()=>{
  const f=fixture(),label=saved=>{const node=f.element();f.context.window.m8.feedback(node,42,saved);return node.children[0].children[3].textContent;};
  assert.equal(label({rating:1,captured:true}),'Included for training review');
  assert.equal(label({rating:-1,captured:false}),'Not captured');
  assert.equal(label({rating:-1,captured:true,used_in_dataset:'data-1'}),'Used for training; vote locked');
  assert.equal(label(null),'Not captured');
});
test('the feedback row goes under the answer, not beside it',()=>{
  const css=fs.readFileSync(path.join(__dirname,'../../agent/web/static/style.css'),'utf8');
  assert.match(css,/\.msg\.assistant\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(css,/\.feedback\s*\{[^}]*flex-basis:\s*100%/);
});
test('restoring a persona version clears the diff picked before it',async()=>{
  const f=fixture();
  f.context.api=async(url,options={})=>{
    f.calls.push({url,options});
    const data=url==='/api/settings/persona'?{active:{agent_name:'Agent',persona:'tone',instructions:''},preview:'prompt',token_count:815,budget:1500,over_budget:false}:
      url==='/api/settings/persona/versions'?[{id:7,created:1,active:1},{id:2,created:1,active:0}]:
      url==='/api/settings/persona/versions/2'?{diff:'--- version 2'}:{};
    return {json:async()=>data};
  };
  const history=f.get('persona-history'),diff=f.get('persona-diff');
  history.value='2';await history.onchange();
  assert.equal(diff.textContent,'--- version 2');
  await f.get('persona-restore').onclick();
  assert.ok(f.calls.some(call=>call.url==='/api/settings/persona/versions/2/restore'&&call.options.method==='POST'));
  assert.equal(diff.textContent,'');
  assert.equal(history.children.length,2);
  assert.equal(f.get('settings-message').textContent,'Restored as a new version.');
});
test('model promotion requires exact typed id and a second delayed click',async()=>{
  const f=fixture();f.context.api=async(url,options={})=>{
    f.calls.push({url,options});
    const data=url==='/api/models'?{current:'base',previous:null,versions:{candidate:{status:'candidate',sha256:'a'.repeat(64),model_card:'<script>bad</script>',comparison:{passed:true}}}}:
      url==='/api/models/promotions'?[{id:'mp_test',version_id:'candidate'}]:url==='/api/training/runs'?[]:{};
    return {json:async()=>data};
  };
  const container=f.element();await f.context.window.m8.promotions(container);
  const card=container.children[0], typed=card.children[4], button=card.children[5];
  await button.onclick();assert.equal(f.calls.filter(row=>row.options.method==='POST').length,0);
  typed.value='candidate';await button.onclick();
  assert.equal(JSON.parse(f.calls.at(-1).options.body).confirm,false);
  await button.onclick();assert.equal(f.calls.filter(row=>row.options.method==='POST').length,1);
  f.advance(1500);await button.onclick();
  const posts=f.calls.filter(row=>row.options.method==='POST');assert.equal(posts.length,2);
  assert.equal(JSON.parse(posts[1].options.body).confirm,true);
  assert.equal(card.children[3].textContent,'<script>bad</script>');
});
test('system training job exposes only its human toggle',async()=>{
  const f=fixture();f.context.api=async()=>({json:async()=>({loop_enabled:false,schedule:'0 3 * * 0',mode:'manual'})});
  const container=f.element();await f.context.window.m8.systemJob(container);
  assert.equal(container.children[0].children.filter(node=>node.tagName==='BUTTON').length,1);
  assert.match(container.children[0].children[0].textContent,/system job/);
});
