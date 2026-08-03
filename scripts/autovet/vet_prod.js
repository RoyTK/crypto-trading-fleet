// Production Birdeye wallet vetter — fresh browser per wallet (defeats CF session challenge) + stealth,
// full field extraction, conservative verdict, resumable (skips addresses already in output file).
const { chromium } = require("playwright");
const fs = require("fs");
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

function parseNum(s){ if(s==null)return null; s=String(s).trim(); if(s==="--"||s===""||s==="N/A")return null;
  let neg=s.includes("-"); let m=s.replace(/[,$%+()\-]/g,"").trim(); let mult=1; const suf=m.slice(-1).toUpperCase();
  if(suf==="K")mult=1e3;else if(suf==="M")mult=1e6;else if(suf==="B")mult=1e9;else if(suf==="T")mult=1e12; if(mult>1)m=m.slice(0,-1);
  let v=parseFloat(m); if(isNaN(v))return null; return neg?-v*mult:v*mult; }
function ageDays(s){ if(!s)return null; const m=s.match(/^(\d+(?:\.\d+)?)\s*(mo|yr|y|d|h|w)$/i); if(!m)return null;
  const n=parseFloat(m[1]),u=m[2].toLowerCase(); if(u==="h")return n/24;if(u==="d")return n;if(u==="w")return n*7;if(u==="mo")return n*30;if(u==="y"||u==="yr")return n*365;return null; }
function verdict(f){
  if(f.realized==null||f.txns==null)return["ERROR","no metrics rendered"];
  if(f.age_days!=null&&f.age_days<7)return["REJECT",`brand new (${f.age_days}d)`];
  if(f.realized<=0)return["REJECT","realized not positive"];
  if(f.realized<5000)return["REJECT",`realized below floor ($${Math.round(f.realized)})`];
  if(f.unrealized!=null&&f.unrealized<0&&Math.abs(f.unrealized)>f.realized)return["REJECT",`bag-holder (unreal -$${Math.round(Math.abs(f.unrealized))} > real $${Math.round(f.realized)})`];
  if(f.multi_x<3)return["REJECT",`only ${f.multi_x} multi-x wins`];
  if(f.instant_sell!=null&&f.instant_sell>=50)return["TOO_FAST",`instant-sell ${f.instant_sell}%`];
  return["KEEP",`real $${Math.round(f.realized)}, ${f.multi_x} multi-x, unreal ${f.unrealized==null?"?":Math.round(f.unrealized)}, rug ${f.scam_rug==null?"?":f.scam_rug}%`];
}
async function vetOnce(addr){
  const browser=await chromium.launch({headless:true,args:["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"]});
  const ctx=await browser.newContext({userAgent:UA,viewport:{width:1400,height:2000},locale:"en-US",timezoneId:"Europe/Berlin",extraHTTPHeaders:{"Accept-Language":"en-US,en;q=0.9"}});
  await ctx.addInitScript(()=>{Object.defineProperty(navigator,"webdriver",{get:()=>undefined});window.chrome={runtime:{}};Object.defineProperty(navigator,"languages",{get:()=>["en-US","en"]});Object.defineProperty(navigator,"plugins",{get:()=>[1,2,3,4,5]});});
  const page=await ctx.newPage();
  try{
    await page.goto("https://birdeye.so/solana/wallet-analyzer/"+addr,{waitUntil:"domcontentloaded",timeout:60000});
    for(let i=0;i<14;i++){ const has=await page.evaluate(()=>{const t=document.body?document.body.innerText:"";const i=t.indexOf("Realized PnL");return i>=0&&/[-+]?\$[\d.]+[KMB]?/.test(t.slice(i,i+40));}).catch(()=>false); if(has)break; await page.waitForTimeout(2000); }
    for(let y=0;y<1800;y+=600){await page.evaluate(_y=>window.scrollTo(0,_y),y).catch(()=>{});await page.waitForTimeout(300);}
    await page.waitForTimeout(700);
    const t=await page.evaluate(()=>document.body?document.body.innerText:"");
    const lines=t.split("\n").map(x=>x.trim()).filter(x=>x.length>0);
    const after=(label,within=3)=>{for(let i=0;i<lines.length;i++){if(lines[i]===label){for(let j=i+1;j<=i+within&&j<lines.length;j++){if(lines[j]!==label)return lines[j];}}}return null;};
    let ageStr=null;for(let i=0;i<Math.min(lines.length,45);i++){if(/^\d+(\.\d+)?\s*(mo|yr|y|d|w)$/i.test(lines[i])){ageStr=lines[i];break;}}
    const cf=/just a moment|performing security|checking your browser|verify you are human/i.test(t);
    const f={realized:parseNum(after("Realized PnL")),unrealized:parseNum(after("Unrealized PnL")),total:parseNum(after("Total PnL")),
      winrate:parseNum(after("Winrate")),txns:parseNum(after("Txns")),age_days:ageDays(ageStr),age_str:ageStr,
      d_gt500:parseNum(after(">500%")),d_500_100:parseNum(after("500% ~ 100%")),
      instant_sell:parseNum(after("Instant Sell")),scam_rug:parseNum(after("Scam/Rug tokens"))};
    f.multi_x=(f.d_gt500||0)+(f.d_500_100||0);
    const [v,reason]=verdict(f); const out={address:addr,verdict:v,reason,cf,...f};
    return out;
  } finally { await browser.close(); }
}
async function vetOne(addr){
  let r=await vetOnce(addr);
  if(r.verdict==="ERROR"){ await new Promise(x=>setTimeout(x,4000)); try{ r=await vetOnce(addr); }catch(e){} } // one retry
  return r;
}
(async()=>{
  const inFile=process.argv[2], outFile=process.argv[3];
  const addrs=fs.readFileSync(inFile,"utf-8").split("\n").map(s=>s.trim()).filter(s=>/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(s));
  const done=new Set();
  if(fs.existsSync(outFile)){ for(const l of fs.readFileSync(outFile,"utf-8").split("\n")){ try{const o=JSON.parse(l); if(o.address&&o.verdict!=="ERROR")done.add(o.address);}catch(e){} } }
  let n=0; const total=addrs.length;
  for(const addr of addrs){
    n++;
    if(done.has(addr)) continue;
    let row; try{row=await vetOne(addr);}catch(e){row={address:addr,verdict:"ERROR",reason:e.message.slice(0,80)};}
    fs.appendFileSync(outFile, JSON.stringify(row)+"\n");
    console.error(`[${n}/${total}] ${row.verdict.padEnd(8)} ${addr.slice(0,10)} ${row.reason||""}`);
    await new Promise(r=>setTimeout(r,2500));
  }
  console.error("VET RUN COMPLETE");
})().catch(e=>{console.error("FATAL",e);process.exit(1);});
