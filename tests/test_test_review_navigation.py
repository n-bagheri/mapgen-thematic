"""/test completed-project reviews and immutable historical outputs."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from PIL import Image

from webui.test_review_sessions import Checkpoints, allowed_api


class ReviewCheckpointTests(unittest.TestCase):
    def test_project_names_cannot_escape_the_review_workspace(self):
        with TemporaryDirectory() as directory:
            for stem in ("..", "../outside", "a/b", "a\\b"):
                with self.assertRaises(ValueError):
                    Checkpoints(directory, stem)

    def test_later_results_and_drafts_survive_an_earlier_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original"
            source.mkdir()
            (source / "early.txt").write_text("original input")
            (source / "later.txt").write_text("original successful result")
            store = Checkpoints(root / "review", "sample")
            store.capture("initial", source)
            store.restore("initial")
            store.checkout(2)
            (store.run / "early.txt").write_text("edited input")
            (store.run / "later.txt").unlink()
            store.checkpoint(2, invalidate=True)
            store.checkout(9)
            self.assertEqual((store.run / "later.txt").read_text(), "original successful result")
            (store.run / "later.txt").write_text("unsaved later draft")
            store.checkout(3, keep=True)
            store.checkout(9)
            self.assertEqual((store.run / "later.txt").read_text(), "unsaved later draft")
            store.checkout(2)
            self.assertEqual((store.run / "early.txt").read_text(), "edited input")
            self.assertFalse((store.run / "later.txt").exists())
            self.assertEqual((source / "early.txt").read_text(), "original input")
            self.assertEqual(store.state["stale"], list(range(3, 10)))

    def test_unchanged_artifacts_are_stored_once_and_timestamps_survive(self):
        with TemporaryDirectory() as directory:
            store = Checkpoints(directory, "sample")
            store.run.mkdir(parents=True)
            file = store.run / "result.png"
            file.write_bytes(b"unchanged image")
            before = file.stat().st_mtime_ns
            store.capture("initial")
            store.checkpoint(7)
            self.assertEqual(len(list(store.blobs.iterdir())), 1)
            store.restore("initial")
            self.assertEqual(file.stat().st_mtime_ns, before)

    def test_proxy_is_limited_to_the_selected_project(self):
        self.assertTrue(allowed_api("/api/step6preset/sample", "sample", "POST"))
        self.assertTrue(allowed_api("/api/test/step7-edit/sample", "sample", "POST"))
        self.assertTrue(allowed_api("/api/labelcrop/sample/1", "sample", "GET"))
        for path, method in [("/api/maps/sample", "DELETE"), ("/api/spec", "POST"),
                             ("/api/session-key", "POST"), ("/api/legend/other", "POST"),
                             ("/api/artifact/sample/../secret", "GET")]:
            self.assertFalse(allowed_api(path, "sample", method))


class ReviewSessionApiTests(unittest.TestCase):
    def test_isolated_api_and_approved_stage_commit_preserve_later_outputs(self):
        from webui import server, test_review_sessions
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps, runs = root / "maps", root / "runs"
            maps.mkdir()
            run = runs / "sample"
            run.mkdir(parents=True)
            Image.new("RGB", (10, 10), "white").save(maps / "sample.png")
            Image.new("L", (10, 10), 255).save(run / "step9_legend.png")
            symbols = {"area_assignments": [{"label": "Forest", "pattern": "03_lines_vertical", "members": [0]}]}
            (run / "symbols.json").write_text(json.dumps(symbols))
            original_legend = (run / "step9_legend.png").read_bytes()
            key = None
            try:
                with patch.object(server, "MAPS_DIR", maps), patch.object(server, "RUNS_DIR", runs):
                    client = server.app.test_client()
                    response = client.post("/api/test/review-session/sample")
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    key = response.get_json()["session"]
                    base = f"/api/test/review-session/{key}"
                    self.assertEqual(client.post(base + "/checkpoint", json={"operation": "checkout", "step": 7}).status_code, 200)
                    result = client.get(base + "/api/pattern-transforms/sample")
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.get_json()["groups"][0]["pattern"], "03_lines_vertical")
                    # Writes in this workspace do not touch the live project's results.
                    session = test_review_sessions.get_session(key)
                    symbols["area_assignments"][0]["pattern"] = "01_noise_dots"
                    (session.directory / "runs" / "sample" / "symbols.json").write_text(json.dumps(symbols))
                    (session.directory / "runs" / "sample" / "category_colors.json").write_text('{"colors":{"Forest":"#123456"}}')
                    self.assertEqual(json.loads((run / "symbols.json").read_text())["area_assignments"][0]["pattern"], "03_lines_vertical")
                    result = client.post(base + "/checkpoint", json={"operation": "checkpoint", "step": 7, "invalidate": True})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(json.loads((run / "symbols.json").read_text())["area_assignments"][0]["pattern"], "01_noise_dots")
                    self.assertEqual(json.loads((run / "category_colors.json").read_text())["colors"]["Forest"], "#123456")
                    self.assertEqual((run / "step9_legend.png").read_bytes(), original_legend)
                    with client.get(base + "/result/9/step9_legend.png") as image:
                        self.assertEqual(image.data, original_legend)
                    self.assertEqual(client.post(base + "/checkpoint", json={"operation": "checkout", "step": 9}).status_code, 200)
                    old = client.get(base + "/api/pattern-transforms/sample").get_json()
                    self.assertEqual(old["groups"][0]["pattern"], "03_lines_vertical")
                    self.assertEqual(client.get(base + "/api/artifact/sample/step9_legend.png").data, original_legend)
                    self.assertEqual(client.post(base + "/api/spec", json={}).status_code, 400)
                    self.assertEqual(client.get("/api/test/review-status/sample").get_json(), {"available": True})
            finally:
                if key:
                    test_review_sessions._sessions.pop(key).close()


HARNESS = r"""
import {state} from '/minimal/state.js';
import {enterPipeline,bindFlowEvents,syncPipelineChrome} from '/test/main.js';
import {completedReview} from '/test/review-navigation.js';
const fixture=__FIXTURE__, clone=value=>structuredClone(value);let checks=0, current=9;
const records=[], branches=new Map(),nativeFetch=fetch.bind(window);
const data=clone(fixture);
Object.assign(data,{
 semantics:{subject:'Land use',title:'Example map',map_type:'area_class_chorochromatic',in_scope:true,legend_present:true,map_language:'English',data_ordering:'nominal',legend_entries:[],thematic_classes:[]},
 mask:{approved:true,reviewed:true,width:10,height:10},legendReview:{approved:true,status:'ready',box:[0,0,10,10]},
 labels:{occurrences:[{id:'text1',index:0,original_text:'Paris',review_text:'Paris',include:true}]},
 lines:{automatic_rivers:[]},classesFinal:{classes:[]},classesGen:{classes:[]},lineGeo:{features:[]},
 aggregation:{review:{approved:true},proposal:{slots:4,source_classes:[{index:0,label:'Forest',rgb:[0,100,0]},{index:1,label:'Crops',rgb:[200,200,0]}],groups:[{label:'Forest',members:[0]},{label:'Crops',members:[1]}]}},
 presets:{active_level:3,variants:{}},step6:{params:{simplification_level:3},classes:[]},
 pageLayout:{canvas_px:[500,700],map_size_px:[200,160],map_origin_px:[50,100],render_px_per_mm:5,furniture:{}},
 braille:{version:4,canvas_px:[200,160],render_px_per_mm:5,geometry:{padding_mm:2},toolbox:{},page:{canvas_px:[500,700],map_origin_px:[50,100],furniture:{}},title:{text:'Example',braille_text:'example',enabled:true,position_page_px:[30,30],render_metrics:{box_size_px:[400,40]}},labels:[{id:'a',text:'Paris',braille_text:'paris',callout:false,enabled:true,position_px:[40,60],render_metrics:{box_offset_px:[-30,-10],box_size_px:[60,20]}}]},
 step8Review:{approved:true},step9Review:{approved:true},
 legend:{page:{canvas_px:[500,700],orientation:'portrait'},render_px_per_mm:5,swatch:{size_px:[200,100]},title:{id:'title',text:'Classes',braille_text:'classes',enabled:true,position_page_px:[30,30],box_width_px:200,render_metrics:{height_px:40}},entries:[]}
});data.step7Review.approved=true;
for(let step=1;step<=9;step++)branches.set(step,clone(data));
let backend=clone(data);
const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
window.fetch=async(url,options={})=>{
 let path=new URL(url,location.href).pathname, body=options.body?JSON.parse(options.body):{};
 if(!path.startsWith('/api/'))return nativeFetch(url,options);
 records.push({path,body,method:options.method||'GET'});
 if(path==='/api/test/review-status/sample')return json({available:true});
 if(path==='/api/test/review-session/sample')return json({session:'test-session',stale:[],drafts:[]});
 if(path.endsWith('/test-session/checkpoint')){
  if(body.operation==='checkout'){if(body.keep)branches.set(current,clone(backend));current=body.step;backend=clone(branches.get(current));}
  else{branches.set(body.step,clone(backend));current=body.step;}
  return json({ok:true});
 }
 path=path.replace('/api/test/review-session/test-session/api/','/api/');
 if(path.endsWith('/step1_semantics.json'))return json(backend.semantics);
 if(path.endsWith('/classes_final.json'))return json(backend.classesFinal);
 if(path.endsWith('/classes_gen.json'))return json(backend.classesGen);
 if(path.endsWith('/lines_gen.geojson'))return json(backend.lineGeo);
 if(path.includes('/artifact/')||path.includes('/result/')||path.includes('/pattern-preview/')||path.includes('/pattern-library-preview/')||path.includes('/labelcrop/'))return nativeFetch('/api/pixel.png');
 if(path==='/api/maps')return json({contract:4,maps:state.maps});
 if(path.startsWith('/api/job/'))return json({status:'done',steps:[]});
 if(path==='/api/run')return json({ok:true});
 if(options.method==='POST'){
  if(path.includes('/labelreview/'))backend.labels.occurrences=body.decisions.map((item,index)=>({id:item.id,index,original_text:item.text,review_text:item.text,include:item.include}));
  if(path.includes('/step7-review/'))Object.assign(backend.step7Review,{approved:body.approve??false},body);
  if(path.includes('/step8-review/'))backend.step8Review.approved=body.approve;
  if(path.includes('/step9-review/'))backend.step9Review.approved=body.approve;
  if(path.includes('/step6preset/'))backend.presets.active_level=body.level;
  if(path.includes('/braille-labels/')&&path.split('/').length===5){
   const label=backend.braille.labels.find(item=>item.id===path.split('/').at(-1));Object.assign(label,body);
   return json({label,enabled_labels:backend.braille.labels.length});
  }
 }
 const services={'maskreview':'mask','legendreview':'legendReview','labelreview':'labels','linereview':'lines','aggregation-review':'aggregation','step6params':'step6','step6presets':'presets','pattern-transforms':'patterns','category-colors':'colors','page-layout':'pageLayout','step7-review':'step7Review','braille-labels':'braille','step8-review':'step8Review','legend':'legend','step9-review':'step9Review'};
 return json(backend[services[path.split('/')[2]]]||{});
};
function check(value,message){if(!value)throw Error(message);checks++;}
async function settled(){for(let i=0;i<500;i++){await new Promise(resolve=>setTimeout(resolve,10));if(!completedReview.busy&&!state.busy){syncPipelineChrome();return;}}throw Error('Review did not settle');}
async function select(step){document.querySelector(`[data-test-review-step="${step}"]`).click();await settled();check(completedReview.step===step,'Top bar selects Step '+step);}
function runs(){return records.filter(record=>record.path.endsWith('/api/run'));}
try{
 const map={stem:'sample',name:'sample.png',in_scope:true,steps:Object.fromEntries(Array.from({length:9},(_,i)=>[String(i+1),true])),step5_review_ready:true,step6_review_ready:true,step7_review_ready:true,step8_review_ready:true,step9_review_ready:true,job:{status:'idle',steps:[]}};
 state.maps=[map];state.selected='sample';state.job=map.job;state.data=clone(data);state.activeStep=9;
 bindFlowEvents();await enterPipeline('sample');
 check(document.querySelectorAll('button[data-test-review-step]').length===9,'All nine finished steps are selectable');
 await select(1);check(document.querySelector('.reading-summary').textContent.includes('Land use'),'Step 1 shows model interpretation');
 check(document.querySelector('[data-test-review-next]').textContent==='Continue','Step 1 has Continue');
 document.querySelector('[data-test-review-next]').click();await settled();check(completedReview.step===2,'Continue advances only to Step 2');
 check(Boolean(document.querySelector('.mask-decision')),'Step 2 restores editable mask review');
 document.querySelector('[data-test-step2-part="legend"]').click();await settled();check(Boolean(document.querySelector('.legend-review')),'Step 2 exposes the legend review');
 document.querySelector('[data-test-step2-part="mask"]').click();await settled();
 document.getElementById('test-mask-continue').click();await settled();check(completedReview.step===3,'Step 2 approval advances only to Step 3');
 check(Boolean(document.querySelector('.label-text')),'Step 3 shows detected text');
 document.querySelector('[data-test-review-next]').click();await settled();check(completedReview.step===4,'Step 3 approval advances only to Step 4');
 check(Boolean(document.querySelector('.test-segmentation-review [data-test-review-next]')),'Step 4 has Continue');
 check(!document.querySelector('.test-review-shell input'),'Step 4 exposes no unrelated editor');
 document.querySelector('[data-test-review-next]').click();await settled();check(completedReview.step===5,'Step 4 Continue advances only to Step 5');
 check(Boolean(document.querySelector('#approve-groups')),'Step 5 restores category review');
 document.getElementById('approve-groups').click();await settled();check(completedReview.step===6,'Unchanged categories advance only to Step 6');
 check(Boolean(document.getElementById('preset-slider')),'Step 6 restores simplification review');
 document.getElementById('apply-preset').click();await settled();check(completedReview.step===7,'Unchanged level advances only to Step 7');
 check(Boolean(document.getElementById('approve-patterns')),'Step 7 restores pattern review');
 document.getElementById('approve-patterns').click();await settled();check(completedReview.step===8,'Unchanged patterns advance only to Step 8');
 check(Boolean(document.querySelector('.braille-text')),'Step 8 restores editable labels');
 document.getElementById('approve-braille').click();await settled();check(completedReview.step===9,'Unchanged labels advance only to Step 9');
 check(Boolean(document.getElementById('approve-legend')),'Step 9 restores legend review');
 check(runs().length===0,'Reviewing unchanged results performs no pipeline reruns');
 await select(3);const text=document.querySelector('.label-text');text.value='Revised Paris';text.dispatchEvent(new Event('input',{bubbles:true}));
 const oldCount=runs().length;document.querySelector('[data-test-review-next]').click();await settled();
 check(completedReview.step===4,'Changed text approval lands at Step 4');
 check(runs().length===oldCount+1&&JSON.stringify(runs().at(-1).body.steps)==='[4]','Changed text regenerates exactly the immediate next step');
 await select(9);check(state.data.legend.title.text==='Classes','Later step keeps last successful legend');
 check(Boolean(document.querySelector('#legend-title-text')),'Later saved result remains editable');
 await select(3);check(document.querySelector('.label-text').value==='Revised Paris','Approved text is saved when reopening Step 3');
 await select(7);document.getElementById('preserve-haptic-distances').click();await settled();
 const patternRuns=runs().length;document.getElementById('approve-patterns').click();await settled();
 check(completedReview.step===8&&runs().length===patternRuns+1&&JSON.stringify(runs().at(-1).body.steps)==='[8]','Native pattern approval regenerates only Step 8');
 const label=document.querySelector('.braille-text');label.value='Revised Braille';label.dispatchEvent(new Event('input',{bubbles:true}));
 const labelRuns=runs().length;document.getElementById('approve-braille').click();await settled();
 check(completedReview.step===9&&runs().length===labelRuns+1&&JSON.stringify(runs().at(-1).body.steps)==='[9]','Braille approval flushes pending text and regenerates only Step 9');
 await select(8);check(document.querySelector('.braille-text').value==='Revised Braille','Pending Braille edits were saved before approval');
 await select(5);const category=document.querySelector('.group-label');category.value='Revised category';category.dispatchEvent(new Event('input',{bubbles:true}));
 const categoryRuns=runs().length;document.getElementById('approve-groups').click();await settled();
 check(completedReview.step===6&&runs().length===categoryRuns+1&&JSON.stringify(runs().at(-1).body.steps)==='[6]','Category approval regenerates only Step 6');
 const slider=document.getElementById('preset-slider');slider.value='4';slider.dispatchEvent(new Event('input',{bubbles:true}));
 const levelRuns=runs().length;document.getElementById('apply-preset').click();await settled();
 check(completedReview.step===7&&runs().length===levelRuns+1&&JSON.stringify(runs().at(-1).body.steps)==='[7]','Simplification approval regenerates only Step 7');
 await select(2);state.maskBrush.strokes=[{mode:'erase',radius:1,points:[[1,1]]}];completedReview.markDirty();
 await select(9);await select(2);check(state.maskBrush.strokes.length===1,'Unapproved mask strokes survive switching to later results and back');
 const maskRuns=runs().length;document.getElementById('test-mask-continue').click();await settled();
 check(completedReview.step===3&&runs().length===maskRuns+1&&JSON.stringify(runs().at(-1).body.steps)==='[3]','Changed mask approval regenerates only Step 3');
 check(!records.some(record=>record.method==='POST'&&record.path==='/api/run'),'Post-run jobs are isolated in /test workspace');
 document.getElementById('results').textContent=checks+' checks passed';
}catch(error){document.getElementById('results').textContent='FAIL: '+error.stack+'; step='+completedReview.step+'; controls='+document.getElementById('control-content').innerHTML.slice(0,2000);}
"""


class CompletedReviewBrowserTests(unittest.TestCase):
    def test_completed_step_selection_and_one_step_approval(self):
        from tests import test_test_step7_browser as browser
        with patch.object(browser, "HARNESS", HARNESS):
            browser.TestStep7BrowserTests().test_step7_modes_choices_colours_and_rendering_queue()

    def test_saved_review_history_remains_selectable_after_partial_regeneration(self):
        from tests import test_test_step7_browser as browser
        restored = HARNESS.replace("state.maps=[map];", "map.steps['7']=false;map.steps['8']=false;map.steps['9']=false;state.maps=[map];")
        with patch.object(browser, "HARNESS", restored):
            browser.TestStep7BrowserTests().test_step7_modes_choices_colours_and_rendering_queue()

    def test_completed_legacy_legend_can_continue_without_a_crop_box(self):
        from tests import test_test_step7_browser as browser
        legacy = HARNESS.replace("legendReview:{approved:true,status:'ready',box:[0,0,10,10]}", "legendReview:{approved:true,status:'legacy'}")
        with patch.object(browser, "HARNESS", legacy):
            browser.TestStep7BrowserTests().test_step7_modes_choices_colours_and_rendering_queue()


if __name__ == "__main__":
    unittest.main()
