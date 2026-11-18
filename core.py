"""Bellhaven reconciliation: reads never modify CRM; writes require explicit review."""
import os, re, json, hashlib, sqlite3, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv('BELLHAVEN_BASE_URL','https://analyst-assessment-production.up.railway.app').rstrip('/')
API = os.getenv('CRM_ACCOUNTS_PATH','/api/v1/accounts')
DB = os.path.join(os.path.dirname(__file__), 'decisions.sqlite3')
HEADERS = {'User-Agent':'BellhavenAssessment/1.0 (manual-review-only)'}
FIELDS = {
 'id':['id','account_id'], 'name':['name','account_name','facility_name'],
 'address':['address','street_address','street','address_line1','address1'],
 'city':['city'], 'state':['state'], 'zip':['zip','zip_code','postal_code','zipcode'],
 'parent_id':['parent_id'], 'status':['status'], 'note':['note','notes'],
 'revenue':['lifetime_revenue'], 'ar':['outstanding_ar'], 'dup':['duplicate_of_account'],
 'chow':['chow_current_account'], 'care':['care_offerings','care_types','services']}

def now(): return datetime.now(timezone.utc).isoformat()
def norm(s): return re.sub(r'[^a-z0-9]','', str(s or '').lower())
def normaddr(s):
 s=str(s or '').lower().replace('street','st').replace('avenue','ave').replace('road','rd').replace('boulevard','blvd').replace('drive','dr').replace('suite','ste')
 return norm(s)
def val(record, field, default=None):
 for key in FIELDS.get(field,[field]):
  if key in record and record[key] is not None: return record[key]
 return default
def key_for(record,field):
 return next((k for k in FIELDS.get(field,[field]) if k in record),FIELDS.get(field,[field])[0])
def number(x):
 try: return float(str(x or 0).replace('$','').replace(',',''))
 except (ValueError,TypeError): return None

def client(token):
 s=requests.Session();s.headers.update({'Authorization':'Bearer '+token.strip(),'Accept':'application/json'});return s

def crm_request(session,method,path,**kwargs):
 url=BASE + (path if path.startswith('/') else '/'+path)
 r=session.request(method,url,timeout=25,**kwargs)
 if not r.ok: raise RuntimeError(f'{method} {path}: HTTP {r.status_code}: {r.text[:500]}')
 try: return r.json()
 except ValueError:return {'raw_response':r.text[:200]}

def unpack(raw):
 if isinstance(raw,list):return raw
 if isinstance(raw,dict):
  for k in ('data','accounts','results','items','records'):
   item=raw.get(k)
   if isinstance(item,list):return item
   if isinstance(item,dict):
    for v in item.values():
     if isinstance(v,list):return v
 return []

def fetch_accounts(session):
 """Tries documented common paging styles. Never sends writes."""
 rows=[];seen=set();raw_sample=None
 for page in range(1,31):
  raw=crm_request(session,'GET',API,params={'page':page,'limit':100,'per_page':100})
  if raw_sample is None:raw_sample=raw
  batch=unpack(raw)
  if not batch:break
  new=0
  for record in batch:
   if not isinstance(record,dict):continue
   ident=str(val(record,'id') or json.dumps(record,sort_keys=True))
   if ident not in seen: rows.append(record);seen.add(ident);new+=1
  if new==0 or len(batch)<100:break
 # A server that uses offset pagination may ignore page; flag suspected incompleteness in UI.
 return rows,raw_sample

def fetch_single(session,identifier):
 return crm_request(session,'GET',API.rstrip('/')+'/'+str(identifier))

def urls_from_directory(soup,current):
 links=set()
 for a in soup.select('a[href]'):
  href=urljoin(current,a['href']);u=urlparse(href)
  if u.netloc!=urlparse(BASE).netloc:continue
  path=u.path.rstrip('/')
  if '/communities/' in path and not re.search(r'/communities/?$',path) and not any(x in path.lower() for x in ('/filter','/search')):
   links.add(href.split('#')[0])
 return links

def directory_pages(session):
 start=BASE+'/communities'; todo=[start]; visited=set(); pages=[];details=set()
 while todo and len(visited)<35:
  url=todo.pop(0)
  if url in visited:continue
  visited.add(url)
  try:
   r=session.get(url,headers=HEADERS,timeout=20);r.raise_for_status()
  except requests.RequestException:continue
  soup=BeautifulSoup(r.text,'html.parser');pages.append((url,soup))
  details|=urls_from_directory(soup,url)
  for a in soup.select('a[href]'):
   href=urljoin(url,a.get('href',''));parsed=urlparse(href)
   if parsed.netloc!=urlparse(BASE).netloc:continue
   if parsed.path.rstrip('/')!='/communities':continue
   if href not in visited and href not in todo and (parsed.query or href!=start):todo.append(href)
 # Also try typical pagination routes, which are harmless GETs.
 for n in range(2,12):
  url=BASE+f'/communities?page={n}'
  if url in visited:continue
  try:
   r=session.get(url,headers=HEADERS,timeout=15)
   if not r.ok:break
   soup=BeautifulSoup(r.text,'html.parser');found=urls_from_directory(soup,url)
   if not found or found.issubset(details):break
   details|=found;pages.append((url,soup))
  except requests.RequestException:break
 return sorted(details),pages

def pick_jsonld(soup):
 for node in soup.select('script[type="application/ld+json"]'):
  try:
   obj=json.loads(node.get_text()); objs=obj if isinstance(obj,list) else [obj]
   for x in objs:
    if isinstance(x,dict):
     objs2=x.get('@graph',[]) if '@graph' in x else [x]
     for v in objs2:
      if isinstance(v,dict) and isinstance(v.get('address'),(dict,str)):
       return v
  except (ValueError,TypeError):pass
 return {}

def scrape_one(session,url):
 r=session.get(url,headers=HEADERS,timeout=20);r.raise_for_status()
 soup=BeautifulSoup(r.text,'html.parser');data=pick_jsonld(soup)
 h=soup.select_one('h1');name=(data.get('name') or (h.get_text(' ',strip=True) if h else '') or (soup.title.get_text(' ',strip=True) if soup.title else ''))
 name=re.split(r'\s+[|–]\s+',name)[0].strip()
 addr=data.get('address') or {}; addr=addr if isinstance(addr,dict) else {'streetAddress':addr}
 street=addr.get('streetAddress','');city=addr.get('addressLocality','');state=addr.get('addressRegion','');zipc=addr.get('postalCode','')
 address_tag=soup.select_one('address') or soup.select_one('[itemprop="address"]')
 if address_tag:
  parts=[x.strip() for x in address_tag.stripped_strings]
  if not street and parts:street=parts[0]
  if not city and len(parts)>1:
   m=re.search(r'([A-Za-z .-]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', ' '.join(parts[1:]))
   if m:city,state,zipc=m.groups()
 # supplement with visible full text; no invented values
 text=soup.get_text(' ',strip=True)
 if not zipc:
  matches=re.findall(r'([\w .#-]{5,80}?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',text)
  if matches:city,state,zipc=matches[0]
 care=[]
 for label in ('Assisted Living','Independent Living','Memory Care','Memory Support','Skilled Nursing','Long-Term Care','Respite Care','Rehabilitation'):
  if re.search(r'\b'+re.escape(label)+r'\b',text,re.I):care.append(label)
 return dict(name=name,address=street,city=city,state=state,zip=zipc,care_offerings=care,url=url)

def scrape():
 s=requests.Session();urls,pages=directory_pages(s);records=[];errors=[]
 for url in urls:
  try:records.append(scrape_one(s,url))
  except Exception as exc: errors.append(f'{url}: {exc}')
  time.sleep(.04)
 return records,dict(detail_urls=len(urls),directory_pages=len(pages),errors=errors)

def evidence_score(site,account):
 n=ratio(norm(site.get('name')),norm(val(account,'name'))) if site.get('name') and val(account,'name') else 0
 a=bool(site.get('address') and val(account,'address') and normaddr(site['address'])==normaddr(val(account,'address')))
 z=bool(site.get('zip') and val(account,'zip') and norm(site['zip'])==norm(val(account,'zip')))
 c=bool(site.get('city') and val(account,'city') and norm(site['city'])==norm(val(account,'city')))
 state=bool(site.get('state') and val(account,'state') and norm(site['state'])==norm(val(account,'state')))
 # Address is critical; name-only never yields a high-confidence classification.
 score=min(100,round(n*.25+(45 if a else 0)+(15 if z else 0)+(8 if c else 0)+(7 if state else 0)))
 if not a and not z: score=min(score,49)
 if not a and n<80:score=min(score,59)
 return score,{'name_similarity':round(n),'street_exact':a,'zip_exact':z,'city_exact':c,'state_exact':state}

def proposal_id(action,site,account,changes):
 material=json.dumps([action,site.get('url',''),val(account,'id') if account else '',changes],sort_keys=True,default=str)
 return hashlib.sha256(material.encode()).hexdigest()[:20]

def make_proposal(action,site,account,changes,score,signals,reason):
 p={'action':action,'site':site or {},'account':account or {},'changes':changes,'score':score,'signals':signals,'reason':reason}
 p['id']=proposal_id(action,p['site'],account,changes)
 return p

def reconcile(sites,accounts,parent_id):
 proposals=[];linked=set(); ranked=[];parent_id=str(parent_id)
 for site in sites:
  cand=sorted([(evidence_score(site,a)[0],a,evidence_score(site,a)[1]) for a in accounts if str(val(a,'id'))!=parent_id],key=lambda x:x[0],reverse=True)
  if not cand:ranked.append((site,None,0,{},0));continue
  score,a,signals=cand[0];second=cand[1][0] if len(cand)>1 else 0
  ranked.append((site,a,score,signals,second))
 for site,a,score,signals,second in ranked:
  if not a or score<65 or (score-second<7 and second>=65):
   action='manual_match' if score>=45 else 'create'
   proposals.append(make_proposal(action,site,a if action=='manual_match' else None,{'parent_id':parent_id},score,signals,'No sufficiently unambiguous CRM match; manually check before creating.'))
   continue
  aid=str(val(a,'id'))
  if aid in linked:
   proposals.append(make_proposal('manual_match',site,a,{},score,signals,'Multiple website facilities match one CRM account; do not auto-merge.'));continue
  linked.add(aid)
  changes={}
  if str(val(a,'parent_id') or '') != parent_id: changes['parent_id']=parent_id
  if norm(site.get('name')) != norm(val(a,'name')): changes['name']=site.get('name')
  if not changes:continue
  if 'parent_id' in changes:
   revenue=number(val(a,'revenue')); ar=number(val(a,'ar'))
   if revenue is None or ar is None:
    action='manual_match';reason='Missing or invalid billing figures; cannot safely decide CHOW.'
   elif revenue>0 and ar>0:
    action='chow';reason='Revenue history AND outstanding AR are positive: preserve old account and create new account.'
   else:
    action='update';reason='Parent correction permitted: revenue history or AR is zero.'
  else:action='update';reason='Matched facility appears to have an outdated name.'
  proposals.append(make_proposal(action,site,a,changes,score,signals,reason))
 for a in accounts:
  aid=str(val(a,'id'))
  if aid==parent_id or str(val(a,'parent_id') or '')!=parent_id or aid in linked:continue
  proposals.append(make_proposal('missing_from_site',{},a,{'status':'Needs Review'},0,{},'Currently assigned to Bellhaven but not confidently matched to website; do not assume closure or sale.'))
 return proposals

def setup_db():
 with sqlite3.connect(DB) as con:
  con.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, decision TEXT NOT NULL, updated_at TEXT, details TEXT)')
  con.execute('CREATE TABLE IF NOT EXISTS steps (proposal_id TEXT, step TEXT, result TEXT, PRIMARY KEY(proposal_id,step))')

def get_decisions():
 setup_db()
 with sqlite3.connect(DB) as con:return {i:d for i,d in con.execute('SELECT id,decision FROM decisions').fetchall()}
def store_decision(pid,decision,details=''):
 setup_db()
 with sqlite3.connect(DB) as con:con.execute('INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)',(pid,decision,now(),details))
def get_step(pid,step):
 setup_db()
 with sqlite3.connect(DB) as con:
  row=con.execute('SELECT result FROM steps WHERE proposal_id=? AND step=?',(pid,step)).fetchone()
 return json.loads(row[0]) if row else None
def save_step(pid,step,result):
 setup_db()
 with sqlite3.connect(DB) as con:con.execute('INSERT OR REPLACE INTO steps VALUES (?,?,?)',(pid,step,json.dumps(result,default=str)))

def detect_created_id(result):
 if not isinstance(result,dict):return None
 if result.get('id'):return result['id']
 for k in ('data','account','result'):
  inner=result.get(k)
  if isinstance(inner,dict) and inner.get('id'):return inner['id']
 return None

def write_patch(session,aid,fields):
 route=os.getenv('CRM_UPDATE_PATH','/api/v1/accounts/{id}').replace('{id}',str(aid))
 method=os.getenv('CRM_UPDATE_METHOD','PATCH').upper()
 return crm_request(session,method,route,json=fields)
def write_create(session,fields):
 return crm_request(session,'POST',os.getenv('CRM_CREATE_PATH','/api/v1/accounts'),json=fields)

def apply_approved(session,p):
 """Call only from explicit Approve button; errors are surfaced, not swallowed."""
 action=p['action'];a=p['account'];site=p['site'];pid=p['id'];changes=p['changes']
 if action in ('manual_match',):raise ValueError('Ambiguous match: resolve manually; no automatic write is safe.')
 if action=='missing_from_site':
  aid=val(a,'id');fresh=fetch_single(session,aid)
  if isinstance(fresh,dict) and isinstance(fresh.get('data'),dict):fresh=fresh['data']
  if str(val(fresh,'parent_id') or '')!=str(val(a,'parent_id') or ''):raise ValueError('Parent changed since proposal. Re-review required.')
  return write_patch(session,aid,{'status':'Needs Review','note':f'Not found on Bellhaven website during {now()[:10]} scrape; verify current ownership before action.'})
 if action=='create':
  if get_step(pid,'create'):return get_step(pid,'create')
  payload={'name':site['name'],'parent_id':changes['parent_id'],'address':site.get('address',''),'city':site.get('city',''),'state':site.get('state',''),'zip':site.get('zip',''),'status':'Active','note':f'Created from operator website: {site.get("url","")}'}
  result=write_create(session,payload);save_step(pid,'create',result);return result
 aid=val(a,'id');fresh=fetch_single(session,aid)
 if isinstance(fresh,dict) and isinstance(fresh.get('data'),dict):fresh=fresh['data']
 if not isinstance(fresh,dict):raise ValueError('Could not verify latest CRM account.')
 if str(val(fresh,'parent_id') or '') != str(val(a,'parent_id') or ''):raise ValueError('Parent changed since proposal; regenerate proposal.')
 if action=='chow':
  rev=number(val(fresh,'revenue'));ar=number(val(fresh,'ar'))
  if rev is None or ar is None or rev<=0 or ar<=0:raise ValueError('CHOW conditions changed; regenerate proposal.')
  prev=get_step(pid,'new_account')
  if prev:new_id=prev['new_id']
  else:
   payload={'name':site['name'],'parent_id':changes['parent_id'],'address':site.get('address') or val(fresh,'address') or '', 'city':site.get('city') or val(fresh,'city') or '', 'state':site.get('state') or val(fresh,'state') or '', 'zip':site.get('zip') or val(fresh,'zip') or '', 'status':'Active','note':f'CHOW successor of account {aid}; source: {site.get("url","")}'}
   created=write_create(session,payload);new_id=detect_created_id(created)
   if not new_id:raise RuntimeError('Create may have succeeded but returned no recognizable ID. Locate successor manually before retrying to avoid duplicates.')
   save_step(pid,'new_account',{'new_id':new_id,'response':created})
  if str(val(fresh,'chow') or '')==str(new_id):return {'new_id':new_id,'already_linked':True}
  if val(fresh,'chow') and str(val(fresh,'chow'))!=str(new_id):raise ValueError('Old account has a different CHOW link; stop.')
  result=write_patch(session,aid,{'chow_current_account':new_id})
  return {'new_id':new_id,'link_result':result}
 if action=='update':
  if 'parent_id' in changes:
   rev=number(val(fresh,'revenue'));ar=number(val(fresh,'ar'))
   if rev is None or ar is None or (rev>0 and ar>0):raise ValueError('Billing rule now requires CHOW or values are missing. Regenerate proposal.')
  patch={key_for(fresh,k):v for k,v in changes.items()}
  return write_patch(session,aid,patch)
 raise ValueError('Unsupported action '+action)

def duplicate_proposals(accounts,parent_id):
 """Suggest only exact street+ZIP duplicates under the same parent; require review."""
 seen={};out=[]
 for a in accounts:
  if str(val(a,'parent_id') or '')!=str(parent_id):continue
  address=normaddr(val(a,'address'));zipcode=norm(val(a,'zip'))
  if not address or not zipcode:continue
  k=(address,zipcode)
  if k not in seen:seen[k]=a;continue
  survivor=seen[k];loser=a
  if str(val(survivor,'id'))==str(val(loser,'id')):continue
  if number(val(loser,'revenue')) is None or number(val(loser,'ar')) is None or number(val(loser,'revenue'))>0 or number(val(loser,'ar'))>0:
   # Account with financial history should never be marked Inactive automatically.
   continue
  changes={'duplicate_of_account':val(survivor,'id'),'status':'Inactive'}
  out.append(make_proposal('duplicate',{},loser,changes,90,{'same_address_zip':True},f'Exact street and ZIP duplicate of account {val(survivor,"id")}. Confirm which record should survive.'))
 return out

_previous_apply=apply_approved
def apply_approved(session,p):
 if p['action']!='duplicate':return _previous_apply(session,p)
 a=p['account'];aid=val(a,'id');fresh=fetch_single(session,aid)
 if isinstance(fresh,dict) and isinstance(fresh.get('data'),dict):fresh=fresh['data']
 if not isinstance(fresh,dict):raise ValueError('Could not re-read duplicate account.')
 if number(val(fresh,'revenue')) is None or number(val(fresh,'ar')) is None or number(val(fresh,'revenue'))>0 or number(val(fresh,'ar'))>0:
  raise ValueError('Financial balances/history present or invalid; do not inactivate without investigation.')
 if str(val(fresh,'parent_id') or '')!=str(val(a,'parent_id') or ''):raise ValueError('Parent changed. Re-review required.')
 changes=p['changes']
 return write_patch(session,aid,{'duplicate_of_account':changes['duplicate_of_account'],'status':'Inactive','note':p['reason']})
