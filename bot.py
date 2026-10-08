import asyncio, base64, json, os, re, secrets, string
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

BOT_TOKEN=os.environ["BOT_TOKEN"]
ADMIN_USER_ID=int(os.environ["ADMIN_USER_ID"])
GITHUB_TOKEN=os.environ.get("PAT_TOKEN") or os.environ.get("GITHUB_TOKEN","")
GITHUB_OWNER=os.environ.get("GITHUB_OWNER","kalausr8")
GITHUB_REPO=os.environ.get("GITHUB_REPO","telegram-hls-recorder")
GITHUB_BRANCH=os.environ.get("GITHUB_BRANCH","main")
WATCHLIST_PATH=".recorder/config/watchlist.json"
IDENTITY_PATH=".recorder/config/identities.json"
ACTIVE_DIR=".recorder/active"
STOP_DIR=".recorder/stop"
MAX_RECORDINGS=5
state_lock=asyncio.Lock()

def now_iso(): return datetime.now(timezone.utc).isoformat()
def norm(v): return (v or "").strip().lstrip("@")
def valid(v): return bool(v and len(v)<=80 and re.fullmatch(r"[A-Za-z0-9._-]+",v))
def rid(): return "".join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6))
def auth(u): return bool(u.effective_user and u.effective_user.id==ADMIN_USER_ID)
async def deny(u):
    if u.message: await u.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
def headers(): return {"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"telegram-hls-recorder-bot"}
def ghurl(p): return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{p}"
async def gh_get(p):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(ghurl(p),headers=headers(),params={"ref":GITHUB_BRANCH}) as r:
            if r.status==404:return None,None
            if r.status!=200: raise RuntimeError(f"GitHub GET {p}: HTTP {r.status}")
            d=await r.json(); c=d.get("content")
            if not c: raise RuntimeError(f"GitHub file {p} has no content")
            return json.loads(base64.b64decode(c.replace("\n","")).decode("utf-8","replace")),d.get("sha")
async def gh_put(p,obj,msg,retries=3):
    raw=base64.b64encode((json.dumps(obj,ensure_ascii=False,indent=2)+"\n").encode()).decode()
    for attempt in range(retries):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(ghurl(p),headers=headers(),params={"ref":GITHUB_BRANCH}) as r:
                if r.status==200: sha=(await r.json()).get("sha")
                elif r.status==404: sha=None
                else: raise RuntimeError(f"GitHub GET before PUT: HTTP {r.status}")
            body={"message":msg,"content":raw,"branch":GITHUB_BRANCH}
            if sha: body["sha"]=sha
            async with s.put(ghurl(p),headers=headers(),json=body) as r:
                if r.status in (200,201): return True
                if r.status==409 and attempt<retries-1:
                    await asyncio.sleep(.7*(attempt+1)); continue
                raise RuntimeError(f"GitHub PUT {p}: HTTP {r.status}")
    return False
async def gh_delete(p,msg):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(ghurl(p),headers=headers(),params={"ref":GITHUB_BRANCH}) as r:
            if r.status==404:return False
            if r.status!=200: raise RuntimeError(f"GitHub GET delete: HTTP {r.status}")
            sha=(await r.json()).get("sha")
        if not sha:return False
        async with s.delete(ghurl(p),headers=headers(),json={"message":msg,"sha":sha,"branch":GITHUB_BRANCH}) as r:
            if r.status in (200,204,404):return r.status!=404
            raise RuntimeError(f"GitHub DELETE {p}: HTTP {r.status}")
async def gh_tree():
    u=f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(u,headers=headers()) as r:
            if r.status!=200:raise RuntimeError(f"GitHub tree: HTTP {r.status}")
            return (await r.json()).get("tree",[])
def aliases(x):
    if not isinstance(x,dict):return []
    vals=[x.get("username"),*(x.get("aliases") or []),x.get("previous_username")]; out=[]
    for v in vals:
        v=str(v or "").strip().lower()
        if v and v not in out:out.append(v)
    return out
def label(x,fallback):
    return str(x.get("display_name") or fallback).strip() if isinstance(x,dict) else fallback
def username_from_url(u):
    try:
        p=[x for x in unquote(urlsplit(u).path).split("/") if x]
        return p[0] if len(p)==1 else ""
    except:return ""
def username_from_title(t):
    m=re.search(r"\(@([A-Za-z0-9._-]+)\)",t or ""); return m.group(1) if m else ""
def display_from_title(t,u):
    t=(t or "").strip(); m=f"(@{u})"; i=t.lower().find(m.lower())
    if i>=0:
        v=t[:i].strip().rstrip("-").strip()
        if v:return v
    if " - Tango Live" in t:
        v=t.split(" - Tango Live",1)[0].strip()
        if v:return v
    return u
async def resolve(username):
    try:
        async with async_playwright() as p:
            b=await p.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
            c=await b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36")
            page=await c.new_page(); r=await page.goto(f"https://www.tango.me/{username}",wait_until="domcontentloaded",timeout=30000)
            t=await page.title(); u=username_from_url(page.url) or username_from_title(t) or username
            await c.close(); await b.close()
            return u,display_from_title(t,u),""
    except Exception:return username,username,""
async def load_watch():
    d,_=await gh_get(WATCHLIST_PATH); out=[];seen=set()
    for v in d if isinstance(d,list) else []:
        v=norm(str(v))
        if valid(v) and v.lower() not in seen:out.append(v);seen.add(v.lower())
    return out
async def load_ids():
    d,_=await gh_get(IDENTITY_PATH); return d if isinstance(d,dict) else {}
async def save_identity(u,name,account=""):
    ids=await load_ids(); old=ids.get(u.lower()) if isinstance(ids.get(u.lower()),dict) else {}; a=aliases(old)
    if u.lower() not in a:a.append(u.lower())
    ids[u.lower()]={"username":u,"display_name":name or u,"account_id":account or old.get("account_id","") ,"aliases":a,"first_seen_at":old.get("first_seen_at",now_iso()),"last_seen_at":now_iso(),"username_changed_at":old.get("username_changed_at"),"username_change_window_days":90}
    await gh_put(IDENTITY_PATH,ids,f"Save identity {u}")
async def active():
    out=[]
    for x in await gh_tree():
        p=str(x.get("path",""))
        if p.startswith(ACTIVE_DIR+"/") and p.endswith(".json"):
            d,_=await gh_get(p)
            if isinstance(d,dict):out.append(d)
    return out
async def dispatch(url,u,name,account=""):
    async with state_lock:
        a=await active()
        if len(a)>=MAX_RECORDINGS:return None,"limit"
        ids=await load_ids(); wanted=set(aliases(ids.get(u.lower(),{})))|{u.lower()}
        for r in a:
            au=str(r.get("username") or "").lower(); ra=set(aliases(ids.get(au,{})))|{au}
            if wanted & ra:return None,"already_recording"
        record=rid(); lease={"record_id":record,"url":url,"username":u,"display_name":name or u,"account_id":account or "","state":"starting","started_at":now_iso(),"heartbeat_at":now_iso()}; lp=f"{ACTIVE_DIR}/{record}.json"
        await gh_put(lp,lease,f"Reserve recording {record}")
        body={"event_type":"telegram_record","client_payload":{"url":url,"record_id":record,"username":u,"display_name":name or u,"account_id":account or ""}}
        du=f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/dispatches"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post(du,headers=headers(),json=body) as r:
                    if r.status==204:return record,"started"
                    await gh_delete(lp,f"Remove failed reservation {record}"); return None,f"github_error:{r.status}"
        except Exception as e:
            try:await gh_delete(lp,f"Remove failed reservation {record}")
            except Exception:pass
            return None,f"dispatch_exception:{type(e).__name__}"
async def start_cmd(u,c):
    if not auth(u):return await deny(u)
    await u.message.reply_text("🎥 HLS Auto Recorder جاهز.\n\n/Addwatch username\n/Removewatch username\n/Watchlist\n/List\n/Stop id|username\n/Stopall\n/Record رابط_البث")
async def add_cmd(u,c):
    if not auth(u):return await deny(u)
    if not c.args:return await u.message.reply_text("الاستخدام:\n/Addwatch username")
    x=norm(c.args[0])
    if not valid(x):return await u.message.reply_text("❌ username غير صالح.")
    try:
        async with state_lock:
            w=await load_watch(); nu,name,acc=await resolve(x)
            w=[v for v in w if v.lower()!=x.lower()]
            if nu.lower() not in {v.lower() for v in w}:w.append(nu)
            await gh_put(WATCHLIST_PATH,w,f"Add watchlist {nu}"); await save_identity(nu,name,acc)
        await u.message.reply_text(f"✅ تمت الإضافة.\n\n👤 {name}\n🔗 @{nu}\n🧠 سيتم تتبع تغيّر الـusername تلقائياً.")
    except Exception as e:await u.message.reply_text(f"❌ تعذر تحديث القائمة.\n{type(e).__name__}")
async def remove_cmd(u,c):
    if not auth(u):return await deny(u)
    if not c.args:return await u.message.reply_text("الاستخدام:\n/Removewatch username")
    q=" ".join(c.args).strip().lower()
    try:
        async with state_lock:
            w=await load_watch(); ids=await load_ids(); target=None
            for x in w:
                z=ids.get(x.lower(),{})
                if q==x.lower() or q in aliases(z) or q==str(z.get("display_name") or "").lower():target=x;break
            if not target:return await u.message.reply_text("❌ لم أجد هذا المستخدم.")
            await gh_put(WATCHLIST_PATH,[x for x in w if x.lower()!=target.lower()],f"Remove watchlist {target}")
        await u.message.reply_text(f"🗑️ تمت إزالة {target} من قائمة المراقبة.")
    except Exception as e:await u.message.reply_text(f"❌ تعذر تحديث القائمة.\n{type(e).__name__}")
async def watch_cmd(u,c):
    if not auth(u):return await deny(u)
    try:
        w=await load_watch(); ids=await load_ids()
        if not w:return await u.message.reply_text("📭 قائمة المراقبة فارغة.")
        lines=[f"📋 قائمة المراقبة ({len(w)}):\n"]
        for i,x in enumerate(w,1):
            z=ids.get(x.lower(),{}); n=label(z,x); prev=str(z.get("previous_username") or "").strip(); extra=f"\n   ↳ سابقاً: @{prev}" if prev and prev.lower()!=x.lower() else ""
            lines.append(f"{i}. {n}\n   @{x}{extra}")
        text="\n\n".join(lines)
        for i in range(0,len(text),3900):await u.message.reply_text(text[i:i+3900])
    except Exception as e:await u.message.reply_text(f"❌ تعذر قراءة القائمة.\n{type(e).__name__}")
async def list_cmd(u,c):
    if not auth(u):return await deny(u)
    try:
        a=await active();ids=await load_ids()
        if not a:return await u.message.reply_text("📭 لا توجد تسجيلات نشطة.")
        lines=[f"🔴 التسجيلات النشطة ({len(a)}/{MAX_RECORDINGS}):\n"]
        for r in a:
            un=str(r.get("username") or ""); n=label(r, label(ids.get(un.lower(),{}),un or "غير معروف")); lines.append(f"👤 {n}\n🔴 الحالة: {r.get('state','unknown')}\n🆔 المعرّف الداخلي: {r.get('record_id','')}\n🔗 @{un}")
        await u.message.reply_text("\n\n".join(lines))
    except Exception as e:await u.message.reply_text(f"❌ تعذر قراءة التسجيلات.\n{type(e).__name__}")
def match(r,q,ids):
    q=q.lower(); un=str(r.get("username") or "").lower(); return q in {str(r.get("record_id") or "").lower(),un,str(r.get("display_name") or "").lower()} or q in aliases(ids.get(un,{}))
async def stop_cmd(u,c):
    if not auth(u):return await deny(u)
    if not c.args:return await u.message.reply_text("الاستخدام:\n/Stop id\nأو\n/Stop username")
    try:
        a=await active();ids=await load_ids();m=[r for r in a if match(r," ".join(c.args).strip(),ids)]
        if not m:return await u.message.reply_text("❌ لم أجد تسجيلًا نشطًا مطابقًا.")
        if len(m)>1:return await u.message.reply_text("⚠️ يوجد أكثر من تسجيل مطابق. استخدم المعرّف الداخلي الظاهر في /List.")
        r=m[0];record=str(r.get("record_id"));un=str(r.get("username") or "");name=label(r,label(ids.get(un.lower(),{}),un)); sig={"record_id":record,"username":un,"stop":True,"requested_at":now_iso()}
        await gh_put(f"{STOP_DIR}/{record}",sig,f"Request stop {record}"); await u.message.reply_text(f"⏹️ تم إرسال إشارة إيقاف {name} بنجاح.")
    except Exception as e:await u.message.reply_text(f"❌ تعذر إرسال إشارة الإيقاف.\n{type(e).__name__}")
async def stopall_cmd(u,c):
    if not auth(u):return await deny(u)
    try:
        a=await active()
        if not a:return await u.message.reply_text("📭 لا توجد تسجيلات نشطة.")
        n=0
        for r in a:
            record=str(r.get("record_id") or "")
            if not record:continue
            try:await gh_put(f"{STOP_DIR}/{record}",{"record_id":record,"username":r.get("username",""),"stop":True,"requested_at":now_iso()},f"Request stop {record}");n+=1
            except Exception:pass
        await u.message.reply_text(f"⏹️ تم إرسال إشارات الإيقاف لـ {n} تسجيلات.")
    except Exception as e:await u.message.reply_text(f"❌ تعذر إيقاف الجميع.\n{type(e).__name__}")
async def record_cmd(u,c):
    if not auth(u):return await deny(u)
    if not c.args:return await u.message.reply_text("الاستخدام:\n/Record https://www.tango.me/username")
    url=c.args[0].strip()
    if not re.match(r"^https?://",url,re.I):return await u.message.reply_text("❌ الرابط غير صالح.")
    un=username_from_url(url) or "manual"; ids=await load_ids(); z=ids.get(un.lower(),{}); name=label(z,un)
    try:
        record,result=await dispatch(url,un,name,str(z.get("account_id") or ""))
        if result=="limit":return await u.message.reply_text(f"⛔ وصلت التسجيلات إلى الحد الأقصى ({MAX_RECORDINGS}).")
        if result=="already_recording":return await u.message.reply_text(f"⚠️ {name} لديه تسجيل نشط بالفعل.")
        if not record:return await u.message.reply_text(f"❌ تعذر تشغيل التسجيل.\n{result}")
        await u.message.reply_text(f"🔴 بدأ طلب تسجيل {name}.\nسيتم تشغيل المسجل عبر GitHub Actions.")
    except Exception as e:await u.message.reply_text(f"❌ تعذر بدء التسجيل.\n{type(e).__name__}")
def main():
    if not GITHUB_TOKEN:raise RuntimeError("PAT_TOKEN أو GITHUB_TOKEN مطلوب لتشغيل البوت.")
    app=Application.builder().token(BOT_TOKEN).build()
    cmds={"start":start_cmd,"Addwatch":add_cmd,"addwatch":add_cmd,"Removewatch":remove_cmd,"removewatch":remove_cmd,"Watchlist":watch_cmd,"watchlist":watch_cmd,"record":record_cmd,"Record":record_cmd,"stop":stop_cmd,"Stop":stop_cmd,"stopall":stopall_cmd,"Stopall":stopall_cmd,"list":list_cmd,"List":list_cmd}
    for name,fn in cmds.items():app.add_handler(CommandHandler(name,fn))
    print("HLS Telegram Recorder Bot started.");app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__":main()
