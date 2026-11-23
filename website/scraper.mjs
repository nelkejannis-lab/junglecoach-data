import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { normalize, csvCell } from './extract.mjs';

const args = process.argv.slice(2);
const option = (key, fallback) => {const i=args.indexOf('--'+key); return i<0?fallback:args[i+1];};
if (args.includes('--help')) {
  console.log('node scraper.mjs --champions ahri,jinx --sources mobalytics,opgg,lolalytics --out data [--headed]');
  process.exit(0);
}
const champions = option('champions','gwen,graves').split(',').map(x=>x.trim().toLowerCase());
const sources = option('sources','mobalytics,opgg,lolalytics').split(',').map(x=>x.trim());
const routes = {
  mobalytics: c => [`https://mobalytics.gg/lol/champions/${c}/build/jungle`,`https://mobalytics.gg/lol/champions/${c}/counters/jungle`],
  opgg: c => [`https://op.gg/lol/champions/${c}/build/jungle`,`https://op.gg/lol/champions/${c}/runes/jungle`,`https://op.gg/lol/champions/${c}/counters/jungle`],
  lolalytics: c => [`https://lolalytics.com/lol/${c}/build/?lane=jungle`]
};
if(champions.some(c=>! /^[a-z0-9-]+$/.test(c)) || sources.some(s=>!routes[s])) throw new Error('Ungueltiger Champion-Slug oder Quelle. --help zeigt Beispiele.');
const started = new Date();
const day = new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Berlin',year:'numeric',month:'2-digit',day:'2-digit'}).format(started);
const directory=resolve(option('out','data'),day,started.toISOString().replaceAll(':','-'));
await mkdir(directory,{recursive:true});
const browser=await chromium.launch({headless:!args.includes('--headed')});
const results=[];
try {
  for(const champion of champions) for(const source of sources) for(const url of routes[source](champion)) {
    const row={champion,source,url,role:'jungle',data_scope:'patch',fallback_reason:'Separat verifizierte Tages-Matchdaten fehlen.',fetched_at:null,status:'error',scope:'Live-Seitenabruf; Statistiken koennen den gesamten Patch umfassen. Keine Beschraenkung auf heute gespielte Matches.'};
    const context=await browser.newContext({locale:'en-US',serviceWorkers:'block'});
    const page=await context.newPage();
    // Routing disables the HTTP cache. Each URL also receives a fresh browser context.
    await page.route('**/*',route=>route.continue());
    try {
      const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000});
      if(!response?.ok()) throw new Error(`HTTP ${response?.status() ?? 'unbekannt'}`);
      await page.waitForFunction(()=> /\b(build|runes|counters)\b/i.test(document.querySelector('h1')?.textContent??''),{},{timeout:20000});
      await page.evaluate(async()=>{for(let y=0;y<Math.min(document.body.scrollHeight,24000);y+=800){window.scrollTo(0,y); await new Promise(r=>setTimeout(r,100));} window.scrollTo(0,0);});
      await page.waitForTimeout(2000);
      const snapshot=await page.evaluate(()=>{
        const root=document.querySelector('main')??document.body;
        const headingNodes=[...root.querySelectorAll('h1,h2,h3,h4,h5,h6')];
        const sections=headingNodes.map((h,i)=>{
          const range=document.createRange(); range.setStartAfter(h);
          if(headingNodes[i+1]) range.setEndBefore(headingNodes[i+1]); else range.setEndAfter(root.lastChild??h);
          const box=document.createElement('div'); box.append(range.cloneContents());
          box.querySelectorAll('script,style,noscript,nav').forEach(e=>e.remove());
          return {heading:h.textContent.trim(),text:box.textContent.replace(/\s+/g,' ').trim(),
            images:[...box.querySelectorAll('img[alt]')].map(e=>({label:e.alt,url:e.getAttribute('src')})).filter(e=>e.label),
            links:[...box.querySelectorAll('a[href]')].map(e=>({label:e.textContent.trim()||e.querySelector('img')?.alt||'',url:new URL(e.getAttribute('href'),location.href).href})),
            tables:[...box.querySelectorAll('table')].map(t=>[...t.rows].map(r=>[...r.cells].map(c=>c.textContent.trim())))};
        });
        return {title:document.title,text:root.innerText,sections};
      });
      const parsed=normalize(snapshot);
      if(!parsed.sections.some(s=>s.text||s.images.length||s.tables.length)) throw new Error('Keine verwertbaren Bereiche erkannt; Seitenstruktur geaendert oder Zugriff blockiert.');
      Object.assign(row,parsed,{fetched_at:new Date().toISOString(),status:'ok',title:snapshot.title,raw_text:snapshot.text});
      console.log(`${source}: ${champion} geladen (${parsed.patch??'Patch unbekannt'})`);
    } catch(error) {row.error=error.message; console.error(`${source}: ${champion}: ${error.message}`);}
    finally {await context.close();}
    results.push(row);
    await new Promise(r=>setTimeout(r,2000));
  }
} finally {await browser.close();}
const payload={schema_version:1,started_at:started.toISOString(),finished_at:new Date().toISOString(),timezone:'Europe/Berlin',cache_used:false,results};
await writeFile(resolve(directory,'results.json'),JSON.stringify(payload,null,2));
await writeFile(resolve(option('out','data'),'website-meta.json'),JSON.stringify(payload,null,2));
const header=['champion','source','url','fetched_at','patch','status','category','heading','text','images_json','tables_json','error'];
const rows=results.flatMap(r=>(r.sections?.length?r.sections:[{}]).map(s=>[r.champion,r.source,r.url,r.fetched_at,r.patch,r.status,s.category,s.heading,s.text,JSON.stringify(s.images??[]),JSON.stringify(s.tables??[]),r.error]));
await writeFile(resolve(directory,'results.csv'),'\uFEFF'+[header,...rows].map(r=>r.map(csvCell).join(';')).join('\r\n'));
console.log(`Export: ${directory}`);
if(results.some(r=>r.status==='error')) process.exitCode=2;