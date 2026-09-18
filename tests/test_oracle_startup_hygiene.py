from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import chatgpt_oracle_compat as compat


def test_install_includes_all_current_and_migration_patches() -> None:
    import codexpro_lifecycle as lifecycle
    shipped = set(lifecycle.manifest_files(ROOT))
    patch_root = ROOT / 'bin/oracle-compat/0.18.0'
    for contract in compat.PATCHES.values():
        references = [contract.get('patch'), contract.get('legacy_patch'), *contract.get('legacy_patches', {}).values()]
        for reference in references:
            if reference:
                relative = (patch_root / reference).resolve().relative_to(ROOT).as_posix()
                assert relative in shipped, relative


@pytest.mark.parametrize('has_preferences', [False, True])
def test_no_submit_receipt_collision_still_cleans_profile(tmp_path: Path, has_preferences: bool) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    package = tmp_path / 'package'
    browser = package / 'dist/src/browser'
    (browser / 'actions').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': '@steipete/oracle', 'version': '0.20.0', 'type': 'module',
    }), encoding='utf-8')
    seed = tmp_path / 'seed'
    seed.mkdir()
    (seed / 'Cookies').write_bytes(b'private fixture')
    if has_preferences:
        (seed / 'Default').mkdir()
        (seed / 'Default/Preferences').write_text('{}', encoding='utf-8')
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    receipt = tmp_path / 'receipt.json'
    receipt.write_text('preserve original', encoding='utf-8')
    environment = {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary), 'TMPDIR': str(temporary)}
    result = subprocess.run([node, str(ROOT / 'scripts/verify_oracle_browser_startup.mjs'),
                             str(package), str(seed), str(receipt)],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'EEXIST' in result.stderr
    assert receipt.read_text(encoding='utf-8') == 'preserve original'
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize('failure', ['none', 'ready', 'personalization', 'deadline'])
@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_single_startup_tab_cleanup_precedes_failing_checks(failure: str, platform: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {startPersonalizedBrowser} = await import(MODULE);
const url='https://chatgpt.com/?temporary-chat=true';
let pages=[{id:'owned',type:'page',url},{id:'startup-blank',type:'page',url:'about:blank'}];
let kills=0, closes=0, opts, promptChecks=0, personalized=false, hidden=0;
const client={Page:{enable:async()=>{}},Runtime:{enable:async()=>{},evaluate:async()=>({result:{value:url}})},
 close:async()=>{closes++},Emulation:{setFocusEmulationEnabled:async()=>{}}};
class Launcher {
 constructor(options){opts=options;this.port=12345;this.pid=321;}
 async launch(){}
 kill(){kills++;}
}
const deps={Launcher,pause:async()=>{},
 jsonAt:async(port,resource)=>resource==='list'?pages:{webSocketDebuggerUrl:'ws://127.0.0.1:12345/devtools/browser/exact'},
 lifecycle:{buildChromeFlagsForTest:()=>[],resolveChromeLaunchOptionsForTest:flags=>({chromeFlags:flags,ignoreDefaultFlags:true}),
 positionChromeWindowOffscreen:async()=>{hidden++;},
 connectToRemoteChromeTarget:async(host,port,log,options)=>{assert.equal(options.targetId,'owned');return {client,targetId:'owned'};},
 closeBlankChromeTabs:async(port,log,host,options)=>{
  assert.equal(options.preserveOneBlank,false);assert.deepEqual(options.excludeTargetIds,['owned']);pages=pages.filter(t=>t.id!=='startup-blank');
 }},
 ensurePromptReady:async()=>{assert.equal(pages.length,1);promptChecks++;if(FAILURE==='deadline')await new Promise(()=>{});if(FAILURE==='ready')throw Error('ready failed');},
 ensureChatMode:async()=>{},
 ensureTemporaryChatPersonalization:async()=>{assert.equal(pages.length,1);if(FAILURE==='personalization')throw Error('personalization failed');personalized=true;}
};
try {
 const session=await startPersonalizedBrowser({port:12345,url,profilePath:'owned-copy',startupTimeoutMs:100,platform:PLATFORM},deps);
 assert.equal(FAILURE,'none');assert.equal(session.evidence.target_id,'owned');
 assert.equal(session.evidence.page_count,1);assert.equal(session.evidence.startup_blank_tabs,0);
 assert.equal(personalized,true);assert.equal(kills,0);
} catch(error){assert.notEqual(FAILURE,'none',error.stack);assert.match(error.message,/failed|deadline/);assert.equal(kills,1);assert.equal(closes,1);}
assert.equal(opts.startingUrl,url);assert.ok(opts.chromeFlags.includes('--hide-crash-restore-bubble'));
assert.equal(pages.length,1);assert.ok(promptChecks>0);
assert.equal(hidden,PLATFORM==='darwin'?1:0);
""".replace('MODULE', json.dumps(module)).replace('FAILURE', json.dumps(failure)).replace('PLATFORM', json.dumps(platform))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_workspace_project_create_selector_is_scoped_unique_and_fail_closed() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {selectWorkspaceProjectCreateTrigger} = await import(MODULE);
class Element {
  constructor(tagName, {text='', attrs={}, visible=true, disabled=false}={}) {
    this.tagName=tagName.toUpperCase();this.innerText=text;this.textContent=text;this.attrs={...attrs};
    this.visible=visible;this.disabled=disabled;this.isConnected=true;this.parentElement=null;this.children=[];
  }
  append(...children){for(const child of children){child.parentElement=this;this.children.push(child);}return this;}
  getClientRects(){return this.visible?[1]:[];}
  getAttribute(name){return Object.prototype.hasOwnProperty.call(this.attrs,name)?this.attrs[name]:null;}
  querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
  querySelectorAll(selector){
    const selectors=selector.split(',').map(value=>value.trim());
    const matches=(element, part)=>part==='button'?element.tagName==='BUTTON':
      part==='div'?element.tagName==='DIV':part==='span'?element.tagName==='SPAN':part==='p'?element.tagName==='P':
      part==='h2'?element.tagName==='H2':part==='h3'?element.tagName==='H3':part==='svg'?element.tagName==='SVG':
      part==='[role="button"]'?element.getAttribute('role')==='button':
      part==='[role="menuitem"]'?element.getAttribute('role')==='menuitem':
      part==='[data-icon]'?element.getAttribute('data-icon')!==null:
      part==='[class*="icon"]'?String(element.getAttribute('class')??'').includes('icon'):false;
    const found=[];
    const visit=element=>{for(const child of element.children){if(selectors.some(part=>matches(child,part)))found.push(child);visit(child);}};
    visit(this);return found;
  }
}
const el=(tag,options)=>new Element(tag,options);
const projectsTree=({icons=1, exact=[], hiddenIcon=false, outsideIcon=false, sidebar=true, newChat=false}={})=>{
  const root=el('main');
  for(const label of exact) root.append(el('button',{text:label}));
  const side=el(sidebar?'nav':'section',{attrs:sidebar?{'aria-label':'Sidebar'}:{}});
  const section=el('section'), header=el('div'), heading=el('h2',{text:'Projects'});
  header.append(heading);
  for(let index=0;index<icons;index+=1) header.append(el('button').append(el('svg')));
  if(hiddenIcon) header.append(el('button',{visible:false}).append(el('svg')));
  if(newChat) header.append(el('button',{text:'New chat'}));
  section.append(header,el('a',{text:'Existing Project',attrs:{role:'button'}}));side.append(section);
  if(outsideIcon) side.append(el('button').append(el('svg')));
  root.append(side);
  return {root, header};
};
let fixture=projectsTree({icons:1,exact:['Create project']});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root).innerText,'Create project','exact label must win over icon fallback');
fixture=projectsTree({icons:1,hiddenIcon:true,outsideIcon:true});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root),fixture.header.children[1],'unique visible Projects icon should be selected');
fixture=projectsTree({icons:2});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root),null,'multiple visible icon candidates must fail closed');
fixture=projectsTree({icons:1,sidebar:false});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root),null,'icon fallback must stay inside the sidebar');
fixture=projectsTree({icons:0,newChat:true,outsideIcon:true});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root),null,'new-chat and unrelated sidebar icons must not be Project create controls');
fixture=projectsTree({icons:1,exact:['New project','Create project']});
assert.equal(selectWorkspaceProjectCreateTrigger(fixture.root),null,'ambiguous exact controls must not fall back to an icon');
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_workspace_project_bootstrap_waits_for_hydration_and_fails_closed() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {ensureWorkspaceProject} = await import(MODULE);
const project='https://chatgpt.com/g/g-p-hydrated123/project';
const realSetTimeout=globalThis.setTimeout;
globalThis.setTimeout=resolve=>{queueMicrotask(resolve);return 1;};
class Element {
  constructor(tagName,{text='',attrs={},visible=true,href=null,disabled=false}={}) {
    this.tagName=tagName.toUpperCase();this.innerText=text;this.textContent=text;this.attrs={...attrs};
    this.visible=visible;this.href=href;this.disabled=disabled;this.isConnected=true;this.parentElement=null;
    this.children=[];this.placeholder='';this.onClick=null;
  }
  append(...children){for(const child of children){child.parentElement=this;this.children.push(child);}return this;}
  getClientRects(){return this.visible?[1]:[];}
  getAttribute(name){return Object.prototype.hasOwnProperty.call(this.attrs,name)?this.attrs[name]:null;}
  querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
  querySelectorAll(){return [];}
  scrollIntoView(){}
  click(){this.onClick?.();}
  focus(){}
  dispatchEvent(){return true;}
}
class TestInput extends Element {
  constructor(){super('input',{attrs:{'aria-label':'Project name'}});this.type='text';this._value='';}
}
Object.defineProperty(TestInput.prototype,'value',{get(){return this._value;},set(value){this._value=String(value);}});
globalThis.HTMLInputElement=TestInput;
globalThis.HTMLTextAreaElement=class extends Element {};
globalThis.InputEvent=globalThis.Event;
const Runtime={evaluate:async({expression})=>({result:{value:await eval(expression)}})};
const bootstrap={name:'Hydrated Project',instructions:'keep exact workspace instructions'};
const setLocation=href=>{const url=new URL(href);globalThis.location={origin:url.origin,pathname:url.pathname,search:url.search,hash:url.hash,href:url.href};};
const resetLocation=()=>setLocation('https://chatgpt.com/');
const existingProjectDocument=()=>({querySelectorAll(selector){
  if(selector==='a,button') return [];
  if(selector==='a[href]') return [new Element('a',{text:bootstrap.name,href:project})];
  return [];
}});

for(const invalidHome of [project,'https://chatgpt.com/c/not-home?temporary-chat=true']){
  setLocation(invalidHome);globalThis.document=existingProjectDocument();
  await assert.rejects(
    ensureWorkspaceProject(Runtime,bootstrap),
    error=>error.code==='WORKSPACE_PROJECT_URL_UNCONFIRMED',
    `non-home bootstrap URL must fail closed: ${invalidHome}`,
  );
}

resetLocation();
let triggerScans=0,dialogVisible=false;
const trigger=new Element('button',{text:'New project'});
const input=new TestInput();
const create=new Element('button',{text:'Create'});
const dialog=new Element('div');
dialog.querySelectorAll=selector=>selector==='input'?[input]:selector==='button,[role="button"],[role="menuitem"]'?[create]:[];
trigger.onClick=()=>{dialogVisible=true;};
create.onClick=()=>{location.pathname='/g/g-p-hydrated123/project';location.href=project;};
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button'||selector==='a[href]') return [];
  if(selector==='button,[role="button"],[role="menuitem"]') return ++triggerScans>=3?[trigger]:[];
  if(selector==='div,span,p,h2,h3') return [];
  if(selector==='[role="dialog"]') return dialogVisible?[dialog]:[];
  return [];
}};
const hydrated=await ensureWorkspaceProject(Runtime,bootstrap);
assert.deepEqual(hydrated,{url:project,created:true});
assert.ok(triggerScans>=3,'create trigger must be re-discovered after hydration');
assert.equal(input.value,bootstrap.name);

resetLocation();
let popoverVisible=false;
const popoverTrigger=new Element('button',{text:'New project'});
const popoverInput=new TestInput();
const popoverCreate=new Element('button',{text:'Create project'});
const popover=new Element('div',{attrs:{popover:''}});
popover.querySelectorAll=selector=>selector==='input'?[popoverInput]:selector==='button,[role="button"],[role="menuitem"]'?[popoverCreate]:[];
popoverTrigger.onClick=()=>{popoverVisible=true;};
popoverCreate.onClick=()=>{location.pathname='/g/g-p-hydrated123/project';location.href=project;};
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button'||selector==='a[href]'||selector==='label'||selector==='[role="dialog"]') return [];
  if(selector==='button,[role="button"],[role="menuitem"]') return [popoverTrigger];
  if(selector==='div,span,p,h2,h3') return [];
  if(selector.includes('[popover]')) return popoverVisible?[popover]:[];
  return [];
}};
const popoverResult=await ensureWorkspaceProject(Runtime,bootstrap);
assert.deepEqual(popoverResult,{url:project,created:true},'non-dialog popover must be supported');
assert.equal(popoverInput.value,bootstrap.name);

resetLocation();
let ambiguousVisible=false;
const ambiguousTrigger=new Element('button',{text:'New project'});
const ambiguousCreate=new Element('button',{text:'Create'});
const ambiguousOne=new TestInput();
const ambiguousTwo=new TestInput();
ambiguousTwo.attrs['aria-label']='Name';
const ambiguousPopover=new Element('div',{attrs:{popover:''}});
ambiguousPopover.querySelectorAll=selector=>selector==='input'?[ambiguousOne,ambiguousTwo]:selector==='button,[role="button"],[role="menuitem"]'?[ambiguousCreate]:[];
ambiguousTrigger.onClick=()=>{ambiguousVisible=true;};
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button'||selector==='a[href]'||selector==='label'||selector==='[role="dialog"]') return [];
  if(selector==='button,[role="button"],[role="menuitem"]') return [ambiguousTrigger];
  if(selector==='div,span,p,h2,h3') return [];
  if(selector.includes('[popover]')) return ambiguousVisible?[ambiguousPopover]:[];
  return [];
}};
await assert.rejects(
  ensureWorkspaceProject(Runtime,bootstrap),
  error=>error.code==='WORKSPACE_PROJECT_CREATE_FAILED'&&error.message==='Project name input is missing or ambiguous',
  'multiple Project/name inputs must fail closed',
);

resetLocation();
const timeoutTrigger=new Element('button',{text:'New project'});
const unrelatedBodyInput=new TestInput();
unrelatedBodyInput.attrs['aria-label']='Name';
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button'||selector==='a[href]'||selector==='label'||selector==='[role="dialog"]') return [];
  if(selector==='button,[role="button"],[role="menuitem"]') return [timeoutTrigger];
  if(selector==='div,span,p,h2,h3') return [];
  if(selector==='input') return [unrelatedBodyInput];
  return [];
}};
await assert.rejects(
  ensureWorkspaceProject(Runtime,bootstrap),
  error=>error.code==='WORKSPACE_PROJECT_CREATE_FAILED'&&error.message==='Project creation surface did not appear',
  'missing creation surface must time out without using an unrelated body input',
);

resetLocation();
globalThis.document={querySelectorAll:()=>[]};
await assert.rejects(
  ensureWorkspaceProject(Runtime,bootstrap),
  error=>error.code==='WORKSPACE_PROJECT_CREATE_FAILED'&&error.message==='New Project control is missing or ambiguous',
);

resetLocation();
const duplicate=()=>new Element('a',{text:bootstrap.name,href:project});
const duplicateProject='https://chatgpt.com/g/g-p-duplicate-new/project';
let duplicateDialogVisible=false,duplicateTriggerClicks=0;
const duplicateTrigger=new Element('button',{text:'New project'});
const duplicateInput=new TestInput();
const duplicateCreate=new Element('button',{text:'Create'});
const duplicateDialog=new Element('div');
duplicateDialog.querySelectorAll=selector=>selector==='input'?[duplicateInput]:selector==='button,[role="button"],[role="menuitem"]'?[duplicateCreate]:[];
duplicateTrigger.onClick=()=>{duplicateTriggerClicks++;duplicateDialogVisible=true;};
duplicateCreate.onClick=()=>setLocation(duplicateProject);
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button') return [];
  if(selector==='a[href]') return [duplicate(),duplicate()];
  if(selector==='button,[role="button"],[role="menuitem"]') return [duplicateTrigger];
  if(selector==='div,span,p,h2,h3'||selector==='label') return [];
  if(selector==='[role="dialog"]') return duplicateDialogVisible?[duplicateDialog]:[];
  return [];
}};
assert.deepEqual(
  await ensureWorkspaceProject(Runtime,bootstrap),
  {url:duplicateProject,created:true},
  'same-name existing Projects must not block creation of a new session Project',
);
assert.equal(duplicateTriggerClicks,1);
assert.equal(duplicateInput.value,bootstrap.name);
globalThis.setTimeout=realSetTimeout;
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_workspace_project_bootstrap_ignores_existing_rows_and_captures_new_project_url() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {ensureWorkspaceProject} = await import(MODULE);
const bootstrap={name:'complex-services-api',instructions:'preserve exact project instructions'};
const oldA='https://chatgpt.com/g/g-p-old-a/project';
const oldB='https://chatgpt.com/g/g-p-old-b/project';
const created='https://chatgpt.com/g/g-p-new-session/project';
const realSetTimeout=globalThis.setTimeout;
globalThis.setTimeout=resolve=>{queueMicrotask(resolve);return 1;};
class Element {
  constructor(tagName,{text='',attrs={},visible=true,href=null,disabled=false}={}) {
    this.tagName=tagName.toUpperCase();this.innerText=text;this.textContent=text;this.attrs={...attrs};
    this.visible=visible;this.href=href;this.disabled=disabled;this.isConnected=true;this.parentElement=null;
    this.children=[];this.placeholder='';this.onClick=null;
  }
  append(...children){for(const child of children){child.parentElement=this;this.children.push(child);}return this;}
  getClientRects(){return this.visible?[1]:[];}
  getAttribute(name){return Object.prototype.hasOwnProperty.call(this.attrs,name)?this.attrs[name]:null;}
  querySelectorAll(){return [];}
  scrollIntoView(){}
  click(){this.onClick?.();}
  focus(){}
  dispatchEvent(){return true;}
}
class TestInput extends Element {
  constructor(){super('input',{attrs:{'aria-label':'Project name'}});this.type='text';this._value='';}
}
Object.defineProperty(TestInput.prototype,'value',{get(){return this._value;},set(value){this._value=String(value);}});
globalThis.HTMLInputElement=TestInput;
globalThis.HTMLTextAreaElement=class extends Element {};
globalThis.InputEvent=globalThis.Event;
const setLocation=href=>{const url=new URL(href);globalThis.location={origin:url.origin,pathname:url.pathname,search:url.search,hash:url.hash,href:url.href};};
setLocation('https://chatgpt.com/');
const Runtime={evaluate:async({expression})=>({result:{value:await eval(expression)}})};
const existing=[
  new Element('a',{text:bootstrap.name,href:oldA}),
  new Element('a',{text:bootstrap.name,href:oldB}),
];
const createdLink=new Element('a',{text:bootstrap.name,href:created});
let dialogVisible=false,createdVisible=false,triggerClicks=0;
const trigger=new Element('button',{text:'New project'});
const input=new TestInput();
const create=new Element('button',{text:'Create'});
const dialog=new Element('div');
dialog.querySelectorAll=selector=>selector==='input'?[input]:selector==='button,[role="button"],[role="menuitem"]'?[create]:[];
trigger.onClick=()=>{triggerClicks++;dialogVisible=true;};
create.onClick=()=>{createdVisible=true;};
globalThis.document={querySelectorAll(selector){
  if(selector==='a,button') return [];
  if(selector==='a[href]') return createdVisible?[...existing,createdLink]:existing;
  if(selector==='button,[role="button"],[role="menuitem"]') return [trigger];
  if(selector==='div,span,p,h2,h3'||selector==='label') return [];
  if(selector==='[role="dialog"]') return dialogVisible?[dialog]:[];
  return [];
}};
const result=await ensureWorkspaceProject(Runtime,bootstrap);
assert.deepEqual(result,{url:created,created:true});
assert.equal(triggerClicks,1,'existing same-name Projects must not be reused');
assert.equal(input.value,bootstrap.name);
assert.equal(location.href,'https://chatgpt.com/','new Project URL fallback should not require navigation when one new Project ID appears');
globalThis.setTimeout=realSetTimeout;
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_workspace_project_bootstrap_waits_for_runtime_document_readiness() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {startPersonalizedBrowser} = await import(MODULE);
const start='https://chatgpt.com/';
const project='https://chatgpt.com/g/g-p-ready123/project';
let current=start,pages=[{id:'owned',type:'page',url:start}],hrefReads=0,readyReads=0,resolverCalls=0;
const client={
 Page:{enable:async()=>{},navigate:async({url})=>{current=url;pages[0].url=url;}},
 Runtime:{enable:async()=>{},evaluate:async({expression})=>{
   if(expression==='location.href'){
     hrefReads++;
     return {result:{value:current===start&&hrefReads<=2?'about:blank':current}};
   }
   if(expression==='document.readyState') return {result:{value:++readyReads>=2?'complete':'loading'}};
   return {result:{value:current}};
 }},
 close:async()=>{},Input:{},Emulation:{setFocusEmulationEnabled:async()=>{}}
};
class Launcher {constructor(){this.port=12345;this.pid=321;} async launch(){} kill(){}}
const deps={Launcher,pause:async()=>{},
 jsonAt:async(port,resource)=>resource==='list'?pages:{webSocketDebuggerUrl:'ws://127.0.0.1:12345/devtools/browser/exact'},
 lifecycle:{buildChromeFlagsForTest:()=>[],resolveChromeLaunchOptionsForTest:flags=>({chromeFlags:flags,ignoreDefaultFlags:true}),
  connectToRemoteChromeTarget:async()=>({client,targetId:'owned'}),closeBlankChromeTabs:async()=>{}},
 ensureWorkspaceProject:async()=>{resolverCalls++;assert.ok(hrefReads>=3);assert.ok(readyReads>=2);return {url:project,created:false};},
 ensureWorkspaceProjectInstructions:async()=>({ok:true,changed:false,verified:true}),
 ensurePromptReady:async()=>{},ensureChatMode:async()=>{},ensureTemporaryChatPersonalization:async()=>{}
};
const bootstrap={name:'complex-services-api',instructions:'instructions'};
const session=await startPersonalizedBrowser({port:12345,url:start,profilePath:'owned-copy',platform:'linux',projectBootstrap:bootstrap},deps);
assert.equal(resolverCalls,1);assert.equal(session.evidence.project_url,project);

current=start;pages=[{id:'owned',type:'page',url:start}];resolverCalls=0;
const neverReadyClient={...client,Runtime:{enable:async()=>{},evaluate:async({expression})=>({result:{value:expression==='location.href'?'about:blank':'loading'}})}};
const neverReadyDeps={...deps,lifecycle:{...deps.lifecycle,connectToRemoteChromeTarget:async()=>({client:neverReadyClient,targetId:'owned'})}};
await assert.rejects(
  startPersonalizedBrowser({port:12345,url:start,profilePath:'owned-copy',platform:'linux',projectBootstrap:bootstrap,startupTimeoutMs:1000},neverReadyDeps),
  error=>error.code==='WORKSPACE_PROJECT_URL_UNCONFIRMED',
  'runtime document wait must be bounded and fail before Project resolution',
);
assert.equal(resolverCalls,0);
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_workspace_project_instructions_are_best_effort_but_project_url_is_not() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {ensureWorkspaceProjectInstructions} = await import(MODULE);
const bootstrap={name:'project',instructions:'required project instructions'};
const logs=[];
const success=await ensureWorkspaceProjectInstructions({evaluate:async()=>({result:{value:{ok:true,changed:true,verified:true}}})},bootstrap,message=>logs.push(message));
assert.deepEqual(success,{ok:true,changed:true,verified:true});
assert.match(logs.at(-1),/updated and verified/);
const unavailable=await ensureWorkspaceProjectInstructions({evaluate:async()=>({result:{value:{
 ok:false,code:'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED',error:'Project instructions editor is missing or ambiguous'
}}})},bootstrap,message=>logs.push(message));
assert.equal(unavailable.ok,true);assert.equal(unavailable.verified,false);assert.equal(unavailable.changed,false);
assert.deepEqual(unavailable.warning,{
 code:'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED',error:'Project instructions editor is missing or ambiguous'
});
assert.match(logs.at(-1),/continuing with the exact Project URL/);
await assert.rejects(
 ensureWorkspaceProjectInstructions({evaluate:async()=>({result:{value:{
  ok:false,code:'WORKSPACE_PROJECT_URL_UNCONFIRMED',error:'Not on the confirmed ChatGPT Project page'
 }}})},bootstrap),
 error=>error.code==='WORKSPACE_PROJECT_URL_UNCONFIRMED',
);
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('platform', ['darwin', 'win32', 'linux'])
def test_workspace_project_bootstrap_stays_headed_until_owned_cleanup(platform: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {startPersonalizedBrowser} = await import(MODULE);
const start='https://chatgpt.com/?temporary-chat=true#bootstrap';
const project='https://chatgpt.com/g/g-p-owned123/project';
let current=start, hideFlag, hidden=0, wroteInstructions=0, personalized=0;
let pages=[{id:'owned',type:'page',url:start}];
const client={
 Page:{enable:async()=>{},navigate:async({url})=>{current=url;pages[0].url=url;}},
 Runtime:{enable:async()=>{},evaluate:async({expression})=>({result:{value:expression==='document.readyState'?'complete':current}})},
 close:async()=>{},Input:{},Emulation:{setFocusEmulationEnabled:async()=>{}}
};
class Launcher {constructor(options){this.options=options;this.port=12345;this.pid=321;} async launch(){} kill(){}}
const deps={Launcher,pause:async()=>{},
 jsonAt:async(port,resource)=>resource==='list'?pages:{webSocketDebuggerUrl:'ws://127.0.0.1:12345/devtools/browser/exact'},
 lifecycle:{
  buildChromeFlagsForTest:(headless,debug,hide)=>{hideFlag=hide;return [];},
  resolveChromeLaunchOptionsForTest:flags=>({chromeFlags:flags,ignoreDefaultFlags:true}),
  positionChromeWindowOffscreen:async()=>{hidden++;},
  connectToRemoteChromeTarget:async()=>({client,targetId:'owned'}),closeBlankChromeTabs:async()=>{}
 },
 ensureWorkspaceProject:async(Runtime,bootstrap)=>{assert.equal(bootstrap.name,'project');return {url:project,created:true};},
 ensureWorkspaceProjectInstructions:async(Runtime,bootstrap)=>{assert.match(bootstrap.instructions,/AGENTS\.md/);wroteInstructions++;return {ok:true,changed:true,verified:true};},
 ensurePromptReady:async()=>{},ensureChatMode:async()=>{},ensureTemporaryChatPersonalization:async()=>{personalized++;}
};
const projectBootstrap={name:'project',instructions:'한국어 AGENTS.md DevSpace /workspace'};
await assert.rejects(
  startPersonalizedBrowser({port:12345,url:project,profilePath:'owned-copy',platform:PLATFORM,projectBootstrap},deps),
  error=>error.code==='WORKSPACE_PROJECT_BOOTSTRAP_INVALID',
  'Project bootstrap must reject a non-home Project path instead of falling back quietly',
);
const session=await startPersonalizedBrowser({port:12345,url:start,profilePath:'owned-copy',platform:PLATFORM,projectBootstrap},deps);
assert.equal(hideFlag,false);assert.equal(hidden,0);assert.equal(wroteInstructions,1);assert.equal(personalized,0);
assert.equal(session.evidence.project_url,project);assert.equal(session.evidence.project_created,true);assert.equal(session.evidence.instructions_verified,true);
assert.equal(session.evidence.conversation_url,project);assert.equal(session.evidence.personalization,'not-applicable');assert.equal(current,project);
current=start;pages[0].url=start;
deps.ensureWorkspaceProjectInstructions=async(Runtime,bootstrap)=>{assert.match(bootstrap.instructions,/AGENTS\.md/);wroteInstructions++;return {
 ok:true,changed:false,verified:false,warning:{code:'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED',error:'Project instructions save action is unavailable'}
};};
const unavailable=await startPersonalizedBrowser({port:12345,url:start,profilePath:'owned-copy',platform:PLATFORM,projectBootstrap},deps);
assert.equal(wroteInstructions,2);assert.equal(personalized,0);assert.equal(unavailable.evidence.instructions_verified,false);
assert.deepEqual(unavailable.evidence.instructions_warning,{code:'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED',error:'Project instructions save action is unavailable'});
assert.equal(unavailable.evidence.project_url,project);assert.equal(unavailable.evidence.conversation_url,project);assert.equal(current,project);
""".replace('MODULE', json.dumps(module)).replace('PLATFORM', json.dumps(platform))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('platform', ['darwin', 'win32', 'linux'])
def test_workspace_project_run_uses_exact_url_without_temporary_personalization(platform: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {startPersonalizedBrowser} = await import(MODULE);
const project='https://chatgpt.com/g/g-p-owned123/project';
let pages=[{id:'owned',type:'page',url:project}], personalized=0, promptChecks=0, hidden=0, hideFlag;
const client={Page:{enable:async()=>{}},Runtime:{enable:async()=>{},evaluate:async()=>({result:{value:project}})},
 close:async()=>{},Input:{},Emulation:{setFocusEmulationEnabled:async()=>{}}};
class Launcher {constructor(options){this.options=options;this.port=12345;this.pid=321;} async launch(){} kill(){}}
const deps={Launcher,pause:async()=>{},
 jsonAt:async(port,resource)=>resource==='list'?pages:{webSocketDebuggerUrl:'ws://127.0.0.1:12345/devtools/browser/exact'},
 lifecycle:{
  buildChromeFlagsForTest:(headless,debug,hide)=>{hideFlag=hide;return [];},
  resolveChromeLaunchOptionsForTest:flags=>({chromeFlags:flags,ignoreDefaultFlags:true}),
  positionChromeWindowOffscreen:async()=>{hidden++;},
  connectToRemoteChromeTarget:async()=>({client,targetId:'owned'}),closeBlankChromeTabs:async()=>{}
 },
 ensurePromptReady:async()=>{promptChecks++;},ensureChatMode:async()=>{},ensureTemporaryChatPersonalization:async()=>{personalized++;}
};
const session=await startPersonalizedBrowser({port:12345,url:project,profilePath:'owned-copy',platform:PLATFORM},deps);
assert.equal(hideFlag,true);assert.equal(hidden,PLATFORM==='darwin'?1:0);assert.ok(promptChecks>=2);assert.equal(personalized,0);
assert.equal(session.evidence.project_url,project);assert.equal(session.evidence.conversation_url,project);
assert.equal(session.evidence.personalization,'not-applicable');assert.equal(session.evidence.target_id,'owned');
""".replace('MODULE', json.dumps(module)).replace('PLATFORM', json.dumps(platform))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('mismatch', ['none', 'browser', 'target', 'url', 'extra-page'])
def test_browser_close_checks_exact_identity_and_tabs(mismatch: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = (ROOT / 'bin/oracle_temporary_personalization_preflight.mjs').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {closePersonalizedBrowser} = await import(MODULE);
let sent=0;
const ws='ws://127.0.0.1:12345/devtools/browser/exact',url='https://chatgpt.com/?temporary-chat=true';
const page={targetId:MISMATCH==='target'?'foreign':'owned',type:'page',url:MISMATCH==='url'?'https://example.test':url};
globalThis.fetch=async resource=>({ok:true,json:async()=>String(resource).endsWith('/version')?
 {webSocketDebuggerUrl:MISMATCH==='browser'?'ws://foreign':ws}:
 (MISMATCH==='extra-page'?[page,{id:'extra',type:'page',url:'about:blank'}]:[page])});
globalThis.WebSocket=class extends EventTarget {
 constructor(){super();queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
 send(value){const request=JSON.parse(value);let result={};if(request.id===1){assert.equal(request.method,'Target.getTargets');result={targetInfos:MISMATCH==='extra-page'?[page,{targetId:'extra',type:'page',url:'about:blank'}]:[page]};}else{assert.equal(request.method,'Browser.close');sent++;}queueMicrotask(()=>this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({id:request.id,result})})));}
 close(){}
};
if(MISMATCH==='none'){assert.equal((await closePersonalizedBrowser(12345,ws,'owned',url)).closed,true);assert.equal(sent,1);}
else {await assert.rejects(closePersonalizedBrowser(12345,ws,'owned',url),/refusing/);assert.equal(sent,0);}
""".replace('MODULE', json.dumps(module)).replace('MISMATCH', json.dumps(mismatch))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_owned_browser_startup_preserves_seed_and_unrelated_tabs(tmp_path: Path) -> None:
    source = Path(os.environ.get('ORACLE_018_PACKAGE_ROOT', '__unset__'))
    if not source.is_dir():
        if os.environ.get('CI'):
            pytest.fail('Exact Oracle 0.18.0 package required')
        pytest.skip('Exact Oracle package unavailable')
    package = tmp_path / 'package'
    shutil.copytree(source, package)
    compat.ensure_oracle_compatibility('oracle 0.18.0', package_root=package, backup_root=tmp_path / 'backup')
    text = (package / 'dist/src/browser/chromeLifecycle.js').read_text(encoding='utf-8')
    replacements = {
        'import CDP from "chrome-remote-interface";': 'const CDP = globalThis.testCDP;',
        'import { launch, Launcher } from "chrome-launcher";': 'const launch=async()=>({port:12345,pid:123}); class Launcher {static defaultFlags(){return [];}}',
        'import { cleanupStaleProfileState } from "./profileState.js";': 'const cleanupStaleProfileState=async()=>{};',
        'import { delay } from "./utils.js";': 'const delay=async()=>{};',
        'import { isWsl, resolveWslChromeLaunchRoute } from "./wslHost.js";': 'const isWsl=()=>false; const resolveWslChromeLaunchRoute=()=>({});',
    }
    for before, after in replacements.items():
        assert before in text
        text = text.replace(before, after)
    module = tmp_path / 'lifecycle.mjs'
    module.write_text(text, encoding='utf-8')
    seed, copied = tmp_path / 'seed', tmp_path / 'copy'
    original = {'profile': {'exit_type': 'Crashed', 'exited_cleanly': False, 'unrelated': 7},
                'session': {'restore_on_startup': 1, 'startup_urls': ['https://example.test']},
                'unrelated': {'keep': True}}
    for directory in (seed, copied):
        (directory / 'Default').mkdir(parents=True)
        (directory / 'Default/Preferences').write_text(json.dumps(original), encoding='utf-8')
        (directory / 'Default/Cookies').write_bytes(b'opaque-cookie-fixture')
    script = """
const closed=[];
let targets=[{id:'startup',type:'page',url:'about:blank'},
 {id:'existing-chat',type:'page',url:'https://chatgpt.com/c/keep'},
 {id:'changed',type:'page',url:'about:blank'}];
globalThis.testCDP=Object.assign(async()=>({close:async()=>{}}),{
 List:async()=>targets,
 New:async()=>{targets.push({id:'active',type:'page',url:'about:blank'});return {id:'active'};},
 Close:async({id})=>{closed.push(id);targets=targets.filter(t=>t.id!==id);}
});
const m=await import(MODULE);
await m.launchChrome({copyProfileSource:SEED},COPY,()=>{});
targets.find(t=>t.id==='changed').url='https://example.test/navigated';
targets.push({id:'later-blank',type:'page',url:'about:blank'});
await m.connectWithNewTab(12345,()=>{},'about:blank');
if(JSON.stringify(closed)!=='["startup"]') throw Error('Wrong owned startup tab cleanup '+JSON.stringify(closed));
await m.connectWithNewTab(9999,()=>{},'about:blank');
if(closed.length!==1) throw Error('Reused browser tabs closed');
let sourceRejected=false;
try {await m.prepareCopiedProfileStartup({copyProfileSource:SEED},SEED);} catch {sourceRejected=true;}
if(!sourceRejected) throw Error('Source profile accepted');
await m.prepareCopiedProfileStartup({},SEED);
console.log('PASS');
""".replace('MODULE', json.dumps(module.as_uri())).replace('SEED', json.dumps(str(seed))).replace('COPY', json.dumps(str(copied)))
    node = shutil.which('node')
    assert node
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads((seed / 'Default/Preferences').read_text(encoding='utf-8')) == original
    changed = json.loads((copied / 'Default/Preferences').read_text(encoding='utf-8'))
    assert changed['profile'] == {'exit_type': 'Normal', 'exited_cleanly': True, 'unrelated': 7}
    assert changed['session'] == {'restore_on_startup': 5, 'startup_urls': []}
    assert changed['unrelated'] == original['unrelated']
    assert (copied / 'Default/Cookies').read_bytes() == b'opaque-cookie-fixture'
