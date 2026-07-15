const COOKIE_NAMES=["NID_AUT","NID_SES"];
async function config(){return chrome.storage.local.get(["server","token","lastSync","lastError"])}
async function sync(){const c=await config();if(!c.server||!c.token)return;try{const cookies={};for(const name of COOKIE_NAMES){const found=await chrome.cookies.getAll({domain:"naver.com",name});if(found[0])cookies[name]=found[0].value}const r=await fetch(`${c.server}/api/extension/cookies`,{method:"PUT",headers:{"Content-Type":"application/json","Authorization":`Bearer ${c.token}`},body:JSON.stringify({cookies})});if(!r.ok)throw new Error((await r.json()).detail||`HTTP ${r.status}`);await chrome.storage.local.set({lastSync:new Date().toISOString(),lastError:null})}catch(e){await chrome.storage.local.set({lastError:String(e.message||e)})}}
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create("cookie-sync",{periodInMinutes:15}));
chrome.alarms.onAlarm.addListener(a=>{if(a.name==="cookie-sync")sync()});
chrome.cookies.onChanged.addListener(info=>{if(COOKIE_NAMES.includes(info.cookie.name)&&info.cookie.domain.endsWith("naver.com"))sync()});
chrome.runtime.onMessage.addListener((message,_sender,send)=>{if(message.type==="sync")sync().then(()=>send({ok:true}));return true});

