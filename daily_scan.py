"""Daily read-only snapshot; never writes to CRM."""
import os,json
from core import client,fetch_accounts,scrape,reconcile,duplicate_proposals,get_decisions,val

def main():
 token=os.getenv('BELLHAVEN_API_TOKEN','')
 parent=os.getenv('BELLHAVEN_PARENT_ID','')
 if not token or not parent:raise SystemExit('Set BELLHAVEN_API_TOKEN and BELLHAVEN_PARENT_ID')
 accounts,_=fetch_accounts(client(token));sites,info=scrape()
 if len(sites)<25:raise SystemExit(f'Incomplete website coverage: {len(sites)} sites; refuse reconciliation')
 proposals=reconcile(sites,accounts,parent)+duplicate_proposals(accounts,parent)
 decisions=get_decisions()
 queue=[p for p in proposals if p['id'] not in decisions]
 with open('proposals.json','w') as f:json.dump({'info':info,'accounts_seen':len(accounts),'pending':queue},f,indent=2,default=str)
 print(f'Accounts {len(accounts)}, websites {len(sites)}, pending {len(queue)}. No CRM writes.')
if __name__=='__main__':main()
