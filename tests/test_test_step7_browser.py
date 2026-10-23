"""Exercise /test Step 7 against an in-browser API double; never edit real runs."""

import base64
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII="
)

HARNESS = r"""
import {state} from '/minimal/state.js';
import {renderControls} from '/minimal/controls.js';
import {patternChoiceBlock,patternChoiceWarnings,patternFamily} from '/test/pattern-policy.js';
import {enterPipeline,bindFlowEvents,syncPipelineChrome,testPatternUpdating} from '/test/main.js';
const fixture=__FIXTURE__;
const records=[];let active=0,maxActive=0,hybrid=false,checks=0,failAssignment=false,holdPreview=false,releasePreview=null,failPreview=false;
const nativeFetch=window.fetch.bind(window);
const clone=x=>structuredClone(x);
const server={review:clone(fixture.step7Review),patterns:clone(fixture.patterns),colors:{colors:{}}};
window.fetch=async (url,options={})=>{
 const path=new URL(url,location.href).pathname;
 if(path.includes('/pattern-preview/')||path.includes('/pattern-library-preview/'))return nativeFetch(url,options);
 if(path.includes('/api/artifact/')) {
  records.push({path});
  if(holdPreview){holdPreview=false;await new Promise(resolve=>{releasePreview=resolve;});}
  if(failPreview)return new Response('',{status:404});
  if(path.includes('hybrid')&&!hybrid)return new Response('',{status:404});
  return nativeFetch(url,options);
 }
 if(!path.startsWith('/api/'))return nativeFetch(url,options);
 const body=options.body?JSON.parse(options.body):{};records.push({path,body});
 if(path==='/api/run')throw Error('Step 7 must not restart while editing');
 if(options.method==='POST'){
  active++;maxActive=Math.max(maxActive,active);
  // Check the watchdog while symbols/cleanup would be temporarily inconsistent.
  const previous=state.maps[0].steps['7'];state.maps[0].steps['7']=false;
  syncPipelineChrome();state.maps[0].steps['7']=previous;
  await new Promise(resolve=>setTimeout(resolve,30));active--;
 }
 let result={};
 if(path.includes('/step7-review/')){
  Object.assign(server.review,body);server.review.approved=false;result=server.review;
 }else if(path.includes('/api/test/step7-edit/')){
  if(body.action==='assign'){
   if(failAssignment){failAssignment=false;return new Response(JSON.stringify({error:'Simulated save failure'}),{status:500});}
   const id=body.group_id;const old=server.patterns.groups[id].pattern;
   if(body.preserve_haptic_distances){
    const family=patternFamily(body.pattern,server.patterns.library);
    for(const group of server.patterns.groups)if(group.group_id!==id&&patternFamily(group.pattern,server.patterns.library)===family)group.pattern=old;
   }
   server.patterns.groups[id].pattern=body.pattern;
   server.review.preserve_haptic_distances=body.preserve_haptic_distances;
  }else if(body.action==='transform')server.patterns.groups[body.group_id].transform=body.transform;
  else if(body.action==='reset'){
   for(const saved of body.groups)Object.assign(server.patterns.groups[saved.group_id],{pattern:saved.pattern,transform:clone(saved.transform)});
   server.review.preserve_haptic_distances=true;
  }
  server.review.approved=false;
  for(const group of server.patterns.groups)group.editable=!['plain','solid_black'].includes(group.pattern);
  result={pattern_data:server.patterns,review:server.review,rendered:true};
 }else if(path.includes('/category-colors/')){
  hybrid=true;server.colors={colors:body.colors};result=server.colors;
 }else if(path==='/api/maps')result={contract:4,maps:state.maps};
 else if(path.includes('/api/job/'))result={status:'done',steps:[7]};
 return new Response(JSON.stringify(clone(result)),{headers:{'Content-Type':'application/json'}});
};
function check(value,message){if(!value)throw Error(message);checks++;}
async function settled(){
 for(let i=0;i<500;i++){
  await new Promise(resolve=>setTimeout(resolve,10));
  if(!testPatternUpdating()) {await Promise.resolve();syncPipelineChrome();return;}
 }
 throw Error('Step 7 did not settle');
}
async function openChange(id){
 const suggested=state.data.step7Review.preserve_haptic_distances;
 const modeSaves=records.filter(r=>r.path.includes('/step7-review/')&&Object.keys(r.body||{}).length).length;
 document.querySelector(`[data-change-pattern="${id}"]`).click();
 check(state.data.step7Review.preserve_haptic_distances===suggested,'Opening Change must keep suggested mode enabled');
 await Promise.resolve();await Promise.resolve();
 check(Boolean(document.querySelector('.pattern-library-grid')),'Change opens the library');
 check(records.filter(r=>r.path.includes('/step7-review/')&&Object.keys(r.body||{}).length).length===modeSaves,'Opening Change must not save a different mode');
}
function assignments(){return state.data.patterns.groups.map(g=>g.pattern);}
const count=part=>records.filter(r=>part==='pattern-assignments'?r.body?.action==='assign':part==='pattern-transforms'?r.body?.action==='transform':part==='resets'?r.body?.action==='reset':r.path.includes(part)).length;
try{
 const map={stem:'sample',name:'sample.png',in_scope:true,steps:Object.fromEntries(Array.from({length:7},(_,i)=>[String(i+1),true])),step5_review_ready:true,step6_review_ready:true,step7_review_ready:false,job:{status:'done',steps:[7]}};
 state.maps=[map];state.selected='sample';state.job={status:'done',steps:[7]};state.data=clone(fixture);state.activeStep=7;
 bindFlowEvents();await enterPipeline('sample');
 const baseline=assignments();
 check([...document.querySelectorAll('[data-edit-pattern]')].every(b=>b.disabled),'Suggested patterns cannot be edited');
 check([...document.querySelectorAll('[data-change-pattern]')].every(b=>!b.disabled),'Suggested patterns can be changed');
 await openChange(1);
 check(document.querySelector('[data-choose-pattern="04_waves_sine"]').disabled,'Sine is unavailable without water');
 check(!document.querySelector('[data-choose-pattern="04_waves_triangle"]').disabled,'Triangle waves are available without water');
 check(!document.querySelector('[data-choose-pattern="plain"]').disabled&&!document.querySelector('[data-choose-pattern="solid_black"]').disabled,'Black and white can both be chosen');
 holdPreview=true;
 document.querySelector('[data-choose-pattern="01_noise_splash"]').click();
 check(document.getElementById('approve-patterns').disabled,'Approval locks immediately when a change starts');
 check(!document.getElementById('test-pattern-render-status').hidden,'Rendering feedback appears immediately');
 for(let i=0;i<200&&!releasePreview;i++)await new Promise(resolve=>setTimeout(resolve,10));
 check(Boolean(releasePreview),'Rendering reaches preview loading');
 check(document.getElementById('approve-patterns').disabled,'Approval stays locked until the new image loads');
 check(document.getElementById('test-pattern-render-status').textContent.includes('Loading'),'Image loading has visible feedback');
 releasePreview();await settled();
 check(!document.getElementById('approve-patterns').disabled,'Approval unlocks after preview decoding');
 check(records.find(r=>r.body?.action==='assign').body.preserve_haptic_distances===true,'Suggested change uses existing global optimizer');
 check([...document.querySelectorAll('[data-edit-pattern]')].every(b=>b.disabled),'Edit stays disabled after optimization');
 document.getElementById('preserve-haptic-distances').click();await settled();
 check(state.data.step7Review.preserve_haptic_distances===false,'Suggested mode can be disabled');
 check(!document.querySelector('[data-edit-pattern="0"]').disabled,'Manual repeating patterns can be edited');
 const before=assignments();const calls=count('pattern-assignments');
 await openChange(1);document.querySelector(`[data-choose-pattern="${before[0]}"]`).click();
 check(document.querySelector('.test-pattern-warning p').textContent==='Minimal distinguishability! Are you sure you want to continue?','Duplicate family warns');
 document.querySelector('.test-pattern-warning [value="cancel"]').click();await settled();
 check(count('pattern-assignments')===calls,'Cancel submits no assignment');
 await openChange(1);document.querySelector(`[data-choose-pattern="${before[0]}"]`).click();document.querySelector('.test-pattern-warning [value="yes"]').click();await settled();
 check(records.filter(r=>r.body?.action==='assign').at(-1).body.preserve_haptic_distances===false,'Manual change disables optimization');
 check(assignments().every((p,i)=>i===1||p===before[i]),'Manual change keeps every other pattern');
 const resetsBefore=count('resets');
 document.getElementById('preserve-haptic-distances').click();
 check(document.getElementById('approve-patterns').disabled,'Reset locks approval immediately');
 await settled();
 check(count('resets')===resetsBefore+1,'Default reset uses one request for all categories');
 check(JSON.stringify(assignments())===JSON.stringify(baseline),'Switching suggestions on restores initial assignments');
 document.getElementById('preserve-haptic-distances').click();
 document.getElementById('preserve-haptic-distances').click();await settled();
 check(state.data.step7Review.preserve_haptic_distances===true,'Rapid toggles retain the latest intent');
 document.getElementById('create-hybrid-map').click();await settled();
 check(count('category-colors')===1,'Enabling hybrid initializes its missing artifact once');
 check([...document.querySelectorAll('[data-pattern-colour]')].every(p=>!p.disabled),'Hybrid enables all colour pickers');
 check(document.querySelector('[data-colour-view]').checked,'Hybrid display automatically turns on');
 check(state.colourView===true,'Hybrid preview selected');
 const picker=document.querySelector('[data-pattern-colour="0"]');picker.value='#123456';picker.dispatchEvent(new Event('change',{bubbles:true}));await settled();
 check(server.colors.colors.Forest==='#123456','Selected colour is saved');
 check(document.querySelector('#step-image-0').src.startsWith('blob:'),'Hybrid image is loaded before display');
 document.getElementById('preserve-haptic-distances').click();await settled();
 document.querySelector('[data-edit-pattern="0"]').click();
 const slider=document.querySelector('[data-key="rotate_deg"]');check(Boolean(slider),'Manual edit dialog opens');
 const transformCount=count('pattern-transforms');
 for(const value of [10,20,30]){slider.value=value;slider.dispatchEvent(new Event('change',{bubbles:true}));}
 check(document.getElementById('approve-patterns').disabled,'Approval is locked during transform debounce');
 await settled();check(count('pattern-transforms')===transformCount+1,'Rapid edits produce one rendering request');
 check(server.patterns.groups[0].transform.rotate_deg===30,'Final slider value is saved');
 document.getElementById('preserve-haptic-distances').click();await settled();
 check(state.data.step7Review.preserve_haptic_distances,'Suggestions can be restored after transforms');
 check(server.patterns.groups[0].transform.rotate_deg===0,'Default transforms are restored');
 const swatch=document.querySelector('[data-pattern-preview="1"]');const previousSwatch=swatch.src;
 failAssignment=true;await openChange(1);document.querySelector('[data-choose-pattern="01_noise_splash"]').click();await settled();
 check(state.data.step7Review.preserve_haptic_distances===true,'Failed optimized assignment restores suggested mode');
 check(swatch.src===previousSwatch,'Failed assignment restores the previous swatch');
 check(document.getElementById('approve-patterns').disabled,'Failed update cannot be approved');
 check(document.getElementById('test-pattern-render-status').classList.contains('is-error'),'Failure has persistent visible feedback');
 document.querySelector('.pattern-dialog [data-close-pattern-dialog]').click();
 const lastImage=document.getElementById('step-image-0').src;
 failPreview=true;await openChange(1);document.querySelector('[data-choose-pattern="01_noise_splash"]').click();await settled();
 check(document.getElementById('approve-patterns').disabled,'Missing updated preview keeps approval locked');
 check(document.getElementById('step-image-0').src===lastImage,'Missing preview retains the last valid map');
 failPreview=false;await openChange(1);document.querySelector('[data-choose-pattern="01_noise_splash"]').click();await settled();
 check(!document.getElementById('approve-patterns').disabled,'Retrying the same pattern recovers a failed preview');
 document.getElementById('preserve-haptic-distances').click();await settled();
 Object.assign(state.data.patterns.groups[0],{is_water:true,pattern:'04_waves_sine'});
 Object.assign(server.patterns.groups[0],{is_water:true,pattern:'04_waves_sine'});
 renderControls();syncPipelineChrome();
 const waterRequests=count('pattern-assignments');await openChange(0);document.querySelector('[data-choose-pattern="plain"]').click();
 check(document.querySelector('.test-pattern-warning p').textContent==='You are choosing a non-water pattern for a water area! Are you sure you want to continue?','Water warning appears in the actual dialog');
 document.querySelector('.test-pattern-warning [value="cancel"]').click();await settled();
 check(count('pattern-assignments')===waterRequests,'Cancelling the water warning saves nothing');
 await openChange(0);document.querySelector('[data-choose-pattern="plain"]').click();document.querySelector('.test-pattern-warning [value="yes"]').click();await settled();
 check(state.data.patterns.groups[0].pattern==='plain','Confirmed manual water override is applied');
 Object.assign(state.data.patterns.groups[0],{pattern:'04_waves_sine'});
 state.data.step7Review.preserve_haptic_distances=true;renderControls();syncPipelineChrome();
 await openChange(0);
 check([...document.querySelectorAll('[data-choose-pattern]')].every(b=>b.disabled===(b.dataset.choosePattern!=='04_waves_sine')),'Suggested water picker permits only sinusoidal waves');
 document.querySelector('.pattern-dialog [data-close-pattern-dialog]').click();
 check(!document.querySelector('.pattern-dialog')&&state.patternDialog===null,'Close dismisses the test picker');
 await openChange(1);
 check(document.querySelector('[data-choose-pattern="04_waves_triangle"]').disabled&&document.querySelector('[data-choose-pattern="04_waves_sine"]').disabled,'A water area reserves the entire wave family');
 document.querySelector('.pattern-dialog [data-close-pattern-dialog]').click();
 check(maxActive===1,'Rendering requests never overlap');
 check(count('/api/run')===0,'Editing never starts another pipeline run');
 check(count('/api/maps')===0,'Watchdog never reads transient incomplete artifacts');
 const water={group_id:0,is_water:true,pattern:'04_waves_sine'};
 const land={group_id:1,is_water:false,pattern:'01_noise_dots'};
 check(Boolean(patternChoiceBlock(water,'04_waves_triangle',[water,land],fixture.patterns.library,true)),'Optimized water is fixed to sine');
 check(Boolean(patternChoiceBlock(land,'04_waves_triangle',[water,land],fixture.patterns.library,true)),'Water excludes triangular waves elsewhere');
 check(!patternChoiceBlock(land,'04_waves_triangle',[land],fixture.patterns.library,true),'No-water maps allow triangle waves');
 check(patternChoiceWarnings(water,'plain',[water,land],fixture.patterns.library,false)[0]==='You are choosing a non-water pattern for a water area! Are you sure you want to continue?','Manual water override warns');
 check(patternChoiceWarnings({group_id:2,pattern:'01_noise_dots'},'plain',[{group_id:0,pattern:'solid_black'}],fixture.patterns.library,false).length===0,'Black and white are different groups');
 document.getElementById('results').textContent=checks+' checks passed';
}catch(error){document.getElementById('results').textContent='FAIL: '+error.stack+'; state='+JSON.stringify({dialogs:[...document.querySelectorAll(".test-pattern-warning")].map(d=>({open:d.open,value:d.returnValue,text:d.textContent})),step:state.activeStep,review:state.data.step7Review,maps:state.maps,requests:records,panel:document.getElementById('control-content').innerHTML.slice(0,700)});}
"""


class TestStep7BrowserTests(unittest.TestCase):
    def test_step7_modes_choices_colours_and_rendering_queue(self):
        from mapgen.patterns import DEFAULT_PATTERN_TRANSFORM, PATTERNS

        browser = next((Path(os.environ.get(env, "")) / suffix
                        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                        for suffix in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe")
                        if (Path(os.environ.get(env, "")) / suffix).is_file()), None)
        if not browser:
            self.skipTest("No installed Chromium browser")
        groups = [{"group_id": i, "label": label, "pattern": pattern,
                   "pattern_desc": PATTERNS[pattern]["desc"], "pattern_family": PATTERNS[pattern]["group"],
                   "is_water": False, "editable": pattern not in ("plain", "solid_black"),
                   "transform": dict(DEFAULT_PATTERN_TRANSFORM)}
                  for i, (label, pattern) in enumerate(zip(
                      ("Forest", "Crops", "Grass", "Other"),
                      ("01_noise_dots", "03_lines_vertical", "02_grid_dots", "solid_black")))]
        fixture = {"mask": {"approved": True}, "legendReview": {"approved": True},
                   "aggregation": {"review": {"approved": True}},
                   "step7Review": {"preserve_haptic_distances": True, "create_hybrid_map": False, "approved": False},
                   "patterns": {"groups": groups, "defaults": DEFAULT_PATTERN_TRANSFORM,
                                "library": [{"pattern": pid, "pattern_family": p["group"], "pattern_desc": p["desc"]}
                                            for pid, p in PATTERNS.items()]}}
        html = (STATIC / "test.html").read_text(encoding="utf-8")
        html = re.sub(r'<script type="module" src="/test/main.js[^\"]*"></script>', "", html)
        html = html.replace("</body>", '<pre id="results">PENDING</pre><script type="module">'
                            + HARNESS.replace("__FIXTURE__", json.dumps(fixture)) + "</script></body>")
        main = (STATIC / "test/main.js").read_text(encoding="utf-8").replace("\ninit();", "\n")
        main += "\nexport {enterPipeline,bindFlowEvents,syncPipelineChrome,testPatternUpdating};"

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                if path == "/":
                    body, mime = html.encode(), "text/html"
                elif path == "/test/main.js":
                    body, mime = main.encode(), "text/javascript"
                elif path.startswith("/api/"):
                    body, mime = PIXEL, "image/png"
                else:
                    p = (STATIC / path.lstrip("/")).resolve()
                    if not p.is_relative_to(STATIC.resolve()) or not p.is_file():
                        self.send_error(404)
                        return
                    body, mime = p.read_bytes(), "text/javascript" if p.suffix == ".js" else "text/css"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(prefix="test-step7-browser-") as profile:
                    result = subprocess.run([
                        str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--user-data-dir=" + profile,
                        "--dump-dom", "--virtual-time-budget=20000", f"http://127.0.0.1:{server.server_port}/",
                    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    summary = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
                    self.assertIsNotNone(summary, result.stderr[-1000:])
                    self.assertRegex(summary.group(1), r"^\d+ checks passed$", summary.group(1))
            finally:
                server.shutdown()
                thread.join(timeout=5)
