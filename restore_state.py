"""Retrieve the newest valid state artifact from earlier workflow runs."""
import os,io,zipfile,requests
from pathlib import Path
repo=os.environ['GITHUB_REPOSITORY']; token=os.environ['GITHUB_TOKEN']
url=f'https://api.github.com/repos/{repo}/actions/artifacts'
headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
r=requests.get(url,headers=headers,params={'name':'eco-retreat-state','per_page':20},timeout=30);r.raise_for_status()
artifacts=[a for a in r.json().get('artifacts',[]) if not a.get('expired') and a.get('workflow_run',{}).get('id')!=int(os.environ.get('GITHUB_RUN_ID','0'))]
if not artifacts:
    print('No prior state artifact; initializing baseline')
else:
    a=max(artifacts,key=lambda a:a['created_at'])
    z=requests.get(a['archive_download_url'],headers=headers,timeout=30);z.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(z.content)) as arc:
        content=arc.read('state.json')
    Path('state.json').write_bytes(content)
    print('Restored state from artifact',a['id'])
