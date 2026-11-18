import os, json, traceback
import streamlit as st
import pandas as pd
from core import (BASE, API,client,crm_request,fetch_accounts,scrape,reconcile,duplicate_proposals,
                  get_decisions,store_decision,apply_approved,val,DB,setup_db)

st.set_page_config(page_title='Bellhaven CRM Review',layout='wide',page_icon='🏥')
st.title('Bellhaven CRM Reconciliation')
st.caption('Read-only collection → proposed changes → explicit human approval → CRM verification')

with st.sidebar:
 st.header('Connection')
 token=st.text_input('Personal API token',type='password',value=os.getenv('BELLHAVEN_API_TOKEN',''))
 st.caption('Token remains in this running application; do not screen record this sidebar with the token visible.')
 st.write('API:',BASE+API)
 st.caption('No writes occur until you press Approve & apply.')
 if st.button('Clear temporary results'):
  for x in ('accounts','sites','proposals','sample','scrape_info','parent_id'):st.session_state.pop(x,None)
  st.rerun()

if not token:
 st.info('Enter your assessment API token in the sidebar, then use the tabs below.');st.stop()
session=client(token)

t1,t2,t3,t4=st.tabs(['1 · Inspect CRM','2 · Scrape & match','3 · Review & apply','4 · Audit / notes'])
with t1:
 st.subheader('Inspect real CRM accounts (read only)')
 if st.button('Fetch all CRM accounts',type='primary'):
  try:
   with st.spinner('Reading CRM...'):
    accounts,sample=fetch_accounts(session)
   st.session_state.accounts=accounts;st.session_state.sample=sample
   st.success(f'Loaded {len(accounts)} unique CRM accounts.')
  except Exception as e:st.error(str(e))
 if 'accounts' in st.session_state:
  accounts=st.session_state.accounts
  if accounts:
   st.write('Fields in first record:',list(accounts[0].keys()))
   display=[]
   for a in accounts:
    display.append({'id':val(a,'id'),'name':val(a,'name'),'parent_id':val(a,'parent_id'), 'city':val(a,'city'),'state':val(a,'state'),'zip':val(a,'zip'), 'status':val(a,'status'), 'lifetime_revenue':val(a,'revenue'),'outstanding_ar':val(a,'ar')})
   st.dataframe(pd.DataFrame(display),use_container_width=True,hide_index=True)
   candidates=[a for a in accounts if 'bellhaven' in str(val(a,'name','')).lower()]
   st.write(f'Bellhaven-name candidates: {len(candidates)}')
   st.dataframe(pd.DataFrame([{'id':val(a,'id'),'name':val(a,'name'),'parent_id':val(a,'parent_id')} for a in candidates]),hide_index=True)
   st.info('Choose the Bellhaven CORPORATE parent account ID, not one of its facility IDs.')
   st.text_input('Bellhaven corporate parent ID',key='parent_id')
   st.warning('If this shows exactly 100 accounts and the sandbox holds more, pagination may need adjustment in core.py before review.')
  else:st.warning('No records returned; check response shape and credentials.')
  with st.expander('Raw API response structure (first 2,000 characters)'):
   st.code(json.dumps(st.session_state.get('sample'),indent=2,default=str)[:2000],language='json')
with t2:
 st.subheader('Scrape Bellhaven community directory')
 if st.button('Collect website facilities',type='primary'):
  try:
   with st.spinner('Visiting directory and detail pages...'):
    sites,info=scrape()
   st.session_state.sites=sites;st.session_state.scrape_info=info
   st.success(f'Collected {len(sites)} facilities from {info["directory_pages"]} directory pages.')
  except Exception as e:st.error(str(e))
 if 'sites' in st.session_state:
  sites=st.session_state.sites;info=st.session_state.scrape_info
  st.dataframe(pd.DataFrame(sites),use_container_width=True,hide_index=True)
  if info['errors']:st.warning('Some facility pages failed: '+str(info['errors'][:10]))
  if not sites or len(sites)<25:
   st.error('Coverage appears incomplete. Check the website structure before treating missing locations as legitimate.')
  csv=pd.DataFrame(sites).to_csv(index=False).encode()
  st.download_button('Download scraped locations CSV',csv,'bellhaven_locations.csv','text/csv')
 if st.button('Generate review proposals',type='primary'):
  sites=st.session_state.get('sites',[]);accounts=st.session_state.get('accounts',[])
  parent_id=st.session_state.get('parent_id','').strip()
  if not sites or not accounts or not parent_id:
   st.error('First fetch CRM accounts, scrape the website, and select the Bellhaven parent account ID.')
  elif len(sites)<25:
   st.error('Scrape seems incomplete. Fix this before generating proposals; otherwise false missing-from-site alerts are likely.')
  elif not any(str(val(a,'id'))==parent_id for a in accounts):
   st.error('Parent ID is not among fetched accounts.')
  else:
   ps=reconcile(sites,accounts,parent_id)+duplicate_proposals(accounts,parent_id)
   st.session_state.proposals=ps
   st.success(f'Generated {len(ps)} proposals; nothing has been written.')

with t3:
 st.subheader('Human review queue')
 proposals=st.session_state.get('proposals',[])
 if not proposals:st.info('Generate proposals in tab 2.');
 else:
  decisions=get_decisions();pending=[p for p in proposals if p['id'] not in decisions]
  st.metric('Pending proposals',len(pending))
  st.caption(f'{sum(x=="approved" for x in decisions.values())} previously approved · {sum(x=="rejected" for x in decisions.values())} previously rejected')
  allowed=['update','create','chow','missing_from_site','duplicate','manual_match']
  kinds=st.multiselect('Filter actions',allowed,default=allowed)
  visible=[p for p in pending if p['action'] in kinds]
  for p in visible:
   a=p['account'];site=p['site'];pid=p['id']
   title=f'{p["action"].upper()} · {site.get("name") or val(a,"name")} · {p["score"]}/100'
   with st.expander(title):
    st.write('**Reason:**',p['reason'])
    st.write('**Existing CRM record:**',{'id':val(a,'id'),'name':val(a,'name'),'parent_id':val(a,'parent_id'),'revenue':val(a,'revenue'),'outstanding_ar':val(a,'ar'),'status':val(a,'status')})
    st.write('**Website evidence:**',site)
    st.write('**Signals:**',p['signals'])
    st.write('**Proposed changes:**',p['changes'])
    if site.get('url'):st.link_button('Open website source',site['url'])
    note=st.text_area('Review note / reason',key='note_'+pid,placeholder='Why is this decision justified?')
    left,right=st.columns(2)
    with left:
     if p['action']=='manual_match':st.warning('Ambiguous: manual matching required before a write.')
     else:
      if st.button('Approve & apply to CRM',key='approve_'+pid,type='primary'):
       try:
        result=apply_approved(session,p)
        store_decision(pid,'approved',json.dumps({'note':note,'response':result},default=str))
        st.success('Applied. Verify in CRM browser.');st.rerun()
       except Exception as e:st.error(f'Not applied / requires attention: {e}')
    with right:
     if st.button('Reject (no CRM change)',key='reject_'+pid):
      store_decision(pid,'rejected',note);st.rerun()
  if not visible:st.success('No pending proposals in this filter. Re-run data retrieval to verify CRM state.')
with t4:
 st.subheader('Decision history and submission notes')
 st.write('Local decision database:',DB)
 import sqlite3
 setup_db()
 with sqlite3.connect(DB) as con:
  decisions=con.execute('SELECT id,decision,updated_at,details FROM decisions ORDER BY updated_at DESC').fetchall()
 st.dataframe(pd.DataFrame(decisions,columns=['proposal_id','decision','UTC timestamp','details']),hide_index=True,use_container_width=True)
 st.markdown('''**Quality controls**\n\n- GET calls are read-only. Writes require the visible approval button.\n- Revenue > 0 AND AR > 0 triggers CHOW, never direct reparenting.\n- Fresh financial and parent information is checked immediately before writes.\n- Decision IDs are stable for unchanged proposals; re-runs suppress already decided items.\n- Missing-from-site accounts are flagged Needs Review rather than deleted.\n- A local SQLite file records decisions. Keep it across runs.\n- Incomplete scraping must be fixed before generating proposals.\n''')
 st.link_button('Open read-only CRM browser',BASE+'/crm/'+token+'/accounts')
