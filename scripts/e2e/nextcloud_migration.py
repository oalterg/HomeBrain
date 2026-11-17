#!/usr/bin/env python3
"""Destructive tests confined to the disposable homebrain-owner-e2e compose project."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from dotenv import dotenv_values
import requests

root = Path(os.environ['INSTALL_DIR'])
assert root.name == 'homebrain-owner-e2e'
env = dict(dotenv_values(root/'.env'))
container = 'homebrain-owner-e2e-nextcloud-1'
source = 'migration'+str(int(time.time()))
destination = 'target'+str(int(time.time()))
password = secrets.token_urlsafe(24)
base = 'http://127.0.0.1:18080'
job = root/source
job.mkdir(mode=0o700)
(job/'source-password').write_text(password)
(job/'dest-password').write_text(password)
for p in job.iterdir():p.chmod(0o600)


def occ(*args):
    p=subprocess.run(['docker','exec','-e','OC_PASS='+password,'-u','www-data',container,'php','occ',*args],capture_output=True,text=True)
    assert p.returncode==0,p.stdout+p.stderr
    return p.stdout


occ('config:system:set','overwriteprotocol','--value=https')
occ('user:add',source,'--password-from-env')
occ('user:add',destination,'--password-from-env')
donor=requests.Session();donor.auth=('admin',env['MASTER_PASSWORD']);donor.headers['OCS-APIRequest']='true'
incoming='incoming-'+source+'.txt'
assert donor.put(base+'/remote.php/dav/files/admin/'+incoming,data=b'incoming fixture',timeout=30).status_code==201
shared=donor.post(base+'/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json',data={'path':incoming,'shareType':0,'shareWith':source,'permissions':1},timeout=30)
assert shared.status_code==200
s = requests.Session();s.auth=(source,password);s.headers['OCS-APIRequest']='true'
dav=base+'/remote.php/dav/files/'+source
for data in [b'first revision',b'second revision']:
    r=s.put(dav+'/keep.txt',data=data,timeout=30);assert r.status_code in (201,204),r.status_code
r=s.put(dav+'/deleted.txt',data=b'trash fixture',timeout=30);assert r.status_code==201
assert s.delete(dav+'/deleted.txt',timeout=30).status_code==204
r=s.post(base+'/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json',data={'path':'keep.txt','shareType':3,'permissions':1},timeout=30)
assert r.status_code==200,r.status_code
share=r.json()['ocs']['data']['token']
r=s.post(base+'/index.php/apps/passwords/api/1.0/password/create',json={'label':'E2E migration','username':'example','password':'fixture-secret-123','url':'https://example.invalid'},timeout=30)
assert r.status_code in (200,201),r.text
occ('dav:create-calendar',source,'migration')
ics='BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:hb-migration-test\r\nDTSTAMP:20261116T120000Z\r\nDTSTART:20261117T120000Z\r\nSUMMARY:Migration fixture\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
r=s.put(base+'/remote.php/dav/calendars/'+source+'/migration/event.ics',data=ics,headers={'Content-Type':'text/calendar'},timeout=30);assert r.status_code==201,r.status_code
occ('dav:create-addressbook',source,'migration')
r=s.put(base+'/remote.php/dav/addressbooks/users/'+source+'/migration/person.vcf',data='BEGIN:VCARD\r\nVERSION:3.0\r\nUID:fixture-person\r\nFN:Fixture Person\r\nEND:VCARD\r\n',headers={'Content-Type':'text/vcard'},timeout=30);assert r.status_code==201,r.status_code
command=[sys.executable,str(root/'scripts/migrate_nextcloud_account.py')]
inspect=subprocess.run(command+['inspect',source,destination],capture_output=True,text=True)
assert inspect.returncode==0,inspect.stderr
inventory=json.loads(inspect.stdout)
assert not inventory['unsupported'],inventory['unsupported']
args=[source,destination,'--job-dir',str(job/'operation'),'--retire-source',
      '--source-password-file',str(job/'source-password'),'--destination-password-file',str(job/'dest-password')]
if '--folder' not in sys.argv:
    args += ['--destination-root','--replace-destination-files']
action='run'
if '--interrupt' in sys.argv:
    from argparse import Namespace
    sys.path.insert(0,str(root/'scripts'))
    from migrate_nextcloud_account import Migration, locked
    with locked(root/'state/accounts.lock'):
        m=Migration(Namespace(source=source,destination=destination,job_dir=str(job/'operation'),
             destination_root='--folder' not in sys.argv,replace_destination_files='--folder' not in sys.argv,
             retire_source=True,source_password_file=str(job/'source-password'),destination_password_file=str(job/'dest-password')))
        m.prepare();m.snapshot()
    assert source in json.loads(occ('user:list','--output=json'))
    action='resume'
r=subprocess.run(command+[action]+args,capture_output=True,text=True)
assert r.returncode==0,r.stderr
result=json.loads(r.stdout);assert result['status']=='complete'
assert source not in json.loads(occ('user:list','--output=json'))
journal=json.loads((job/'operation/journal.json').read_text())
folder=journal.get('folder','')
incoming_path='/'.join(p for p in (folder,incoming) if p)
r=requests.get(base+'/remote.php/dav/files/'+destination+'/'+incoming_path,auth=(destination,password),timeout=30)
assert r.status_code==200 and r.content==b'incoming fixture'
file_path='/'.join(p for p in (folder,'keep.txt') if p)
r=requests.get(base+'/remote.php/dav/files/'+destination+'/'+file_path,auth=(destination,password),timeout=30)
assert r.status_code==200 and r.content==b'second revision'
r=requests.get(base+'/public.php/dav/files/'+share,timeout=30);assert r.status_code==200 and r.content==b'second revision'
r=requests.get(base+'/index.php/apps/passwords/api/1.0/password/list',auth=(destination,password),timeout=30)
assert any(p['password']=='fixture-secret-123' for p in r.json())
r=subprocess.run(command+['resume']+args,capture_output=True,text=True);assert r.returncode==0,r.stderr
# Read old versions and actually restore a trashed file through the destination API.
t=requests.Session();t.auth=(destination,password)
props=t.request('PROPFIND',base+'/remote.php/dav/files/'+destination+'/'+file_path,headers={'Depth':'0','Content-Type':'application/xml'},data='<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:fileid/></d:prop></d:propfind>',timeout=30)
assert props.status_code==207
file_id=ET.fromstring(props.content).find('.//{http://owncloud.org/ns}fileid').text
versions=t.request('PROPFIND',base+'/remote.php/dav/versions/'+destination+'/versions/'+file_id,headers={'Depth':'1'},timeout=30)
assert versions.status_code==207
hrefs=[e.text for e in ET.fromstring(versions.content).findall('.//{DAV:}href')]
assert any(t.get(base+urlparse(h).path,timeout=30).content==b'first revision' for h in hrefs[1:])
trash=t.request('PROPFIND',base+'/remote.php/dav/trashbin/'+destination+'/trash',headers={'Depth':'1'},timeout=30)
assert trash.status_code==207
href=next(e.text for e in ET.fromstring(trash.content).findall('.//{DAV:}href') if 'deleted.txt' in unquote(e.text))
restored=t.request('MOVE',base+urlparse(href).path,headers={'Destination':base+'/remote.php/dav/trashbin/'+destination+'/restore/deleted.txt'},timeout=30)
assert restored.status_code in (201,204),restored.text
trash_path='/'.join(p for p in (folder,'deleted.txt') if p)
assert t.get(base+'/remote.php/dav/files/'+destination+'/'+trash_path,timeout=30).content==b'trash fixture'
print('PASS migration: live files, shared link, passwords, versions, trash, DAV content, retirement, completed-job resume',flush=True)
