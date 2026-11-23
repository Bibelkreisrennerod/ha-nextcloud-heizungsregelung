"""Minimale Nachbildung der benoetigten Home-Assistant-Schnittstellen fuer Tests."""
import sys, types
from datetime import datetime, timezone, timedelta
NOW=[datetime(2026,11,22,0,0,tzinfo=timezone.utc)]
def mod(name):
    m=types.ModuleType(name); sys.modules[name]=m; return m
ha=mod("homeassistant")
for n in ["components","helpers","util"]: mod("homeassistant."+n)
sys.modules["homeassistant.util"].slugify=lambda t: "".join(ch if ch.isalnum() else "_" for ch in t.lower()).strip("_")
pn=mod("homeassistant.components.persistent_notification")
NOTES={}
def async_create(hass,message,title=None,notification_id=None): NOTES[notification_id]=(title,message)
def async_dismiss(hass,nid): NOTES.pop(nid,None)
pn.async_create=async_create; pn.async_dismiss=async_dismiss
sys.modules["homeassistant.components"].persistent_notification=pn
ce=mod("homeassistant.config_entries"); 
class ConfigEntry: pass
ce.ConfigEntry=ConfigEntry
c=mod("homeassistant.const"); c.STATE_UNAVAILABLE="unavailable"; c.STATE_UNKNOWN="unknown"
core=mod("homeassistant.core"); core.HomeAssistant=object; core.callback=lambda f:f
class Area:
    def __init__(s,i,n): s.id=i; s.name=n
class AR:
    def __init__(s,areas): s.areas=areas
    def async_get_area_by_name(s,n): return next((a for a in s.areas if a.name.lower()==n.lower()),None)
class Ent:
    def __init__(s,eid,area,platform="homematicip_cloud"): s.entity_id=eid; s.domain=eid.split(".")[0]; s.area_id=area; s.device_id=None; s.disabled_by=None; s.platform=platform; s.translation_key=None; s.original_name=None; s.unique_id=eid
class ER:
    def __init__(s,ents): s.entities={e.entity_id:e for e in ents}
    def async_get(s,eid): return s.entities.get(eid)
    def async_get_entity_id(s,domain,platform,unique_id): return None
class DR:
    def async_get(s,i): return None
REG={}
arm=mod("homeassistant.helpers.area_registry"); arm.async_get=lambda h: REG["ar"]
erm=mod("homeassistant.helpers.entity_registry"); erm.async_get=lambda h: REG["er"]
drm=mod("homeassistant.helpers.device_registry"); drm.async_get=lambda h: DR()
h=sys.modules["homeassistant.helpers"]; h.area_registry=arm; h.entity_registry=erm; h.device_registry=drm
dm=mod("homeassistant.helpers.dispatcher"); dm.async_dispatcher_send=lambda *a:None
ev=mod("homeassistant.helpers.event"); ev.async_track_time_interval=lambda h,cb,iv: (lambda:None)
CALL_LATER=[]
def _call_later(h,delay,cb):
    CALL_LATER.append(cb); return lambda: None
ev.async_call_later=_call_later
st=mod("homeassistant.helpers.storage")
class Store:
    DATA={}
    def __init__(s,h,v,k): s.k=k
    async def async_load(s): return Store.DATA.get(s.k)
    async def async_save(s,d): Store.DATA[s.k]=d
    def async_delay_save(s,f,d): Store.DATA[s.k]=f()
st.Store=Store
udt=mod("homeassistant.util.dt")
udt.utcnow=lambda: NOW[0]; udt.now=lambda: NOW[0]; udt.UTC=timezone.utc
udt.as_local=lambda d: d.astimezone(timezone(timedelta(hours=1)))
udt.as_utc=lambda d: d.astimezone(timezone.utc)
udt.parse_datetime=lambda s: datetime.fromisoformat(s)
sys.modules["homeassistant.util"].dt=udt
class State:
    def __init__(s,state,attrs,when=None): s.state=state; s.attributes=attrs; s.last_updated=when or NOW[0]
class States(dict):
    def get(s,k): return dict.get(s,k)
class Services:
    def __init__(s,hass): s.hass=hass; s.calls=[]
    async def async_call(s,domain,service,data=None,blocking=False,target=None,return_response=False,**kw):
        s.calls.append((NOW[0],domain,service,data))
        if domain=="climate" and service=="set_temperature":
            for e in data["entity_id"]: s.hass.setpoints[e]=data["temperature"]
        if domain=="climate" and service=="set_hvac_mode":
            for e in data["entity_id"]: s.hass.modes[e]=data["hvac_mode"]
        if domain=="weather": return {"weather.home":{"forecast":[{"datetime":(NOW[0]+timedelta(hours=i)).isoformat(),"temperature":s.hass.outside} for i in range(48)]}}
class Hass:
    def __init__(s):
        s.states=States(); s.services=Services(s); s.setpoints={}; s.modes={}; s.outside=0.0
    def async_create_task(s,c):
        import asyncio
        return asyncio.ensure_future(c)
    async def async_add_executor_job(s,f,*a): return f(*a)
ac=mod("homeassistant.helpers.aiohttp_client"); ac.async_get_clientsession=lambda h: None
udt.get_default_time_zone=lambda: timezone(timedelta(hours=1))

exc=mod("homeassistant.exceptions")
class HomeAssistantError(Exception): pass
exc.HomeAssistantError=HomeAssistantError

# Recorder: Historie aus RECORDED (entity_id -> [State]) liefern
RECORDED={}
rec=mod("homeassistant.components.recorder")
class _Rec:
    async def async_add_executor_job(s,f,*a): return f(*a)
rec.get_instance=lambda h: _Rec()
hist=mod("homeassistant.components.recorder.history")
def get_significant_states(hass,start,end=None,entity_ids=None,**kw):
    out={}
    for e in entity_ids or []:
        states=[st for st in RECORDED.get(e,[]) if st.last_updated<=end]
        before=[st for st in states if st.last_updated<start]
        inside=[st for st in states if st.last_updated>=start]
        out[e]=(before[-1:] if kw.get("include_start_time_state") else [])+inside
    return out
hist.get_significant_states=get_significant_states
rec.history=hist
