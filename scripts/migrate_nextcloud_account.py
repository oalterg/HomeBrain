#!/usr/bin/env python3
"""Verified, resumable migration of Nextcloud 33 accounts. See --help and the runbook."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import tempfile
from urllib.parse import quote

import requests
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from vault_admin import locked, read_json, write_json  # noqa: E402


class MigrationError(Exception):
    pass


def file_hash(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def run(argv, **kwargs):
    p = subprocess.run(argv, capture_output=True, **kwargs)
    if p.returncode:
        # argv may carry a password in docker's environment; never include it in exceptions.
        fd, report = tempfile.mkstemp(prefix='homebrain-migration-error-')
        with os.fdopen(fd, 'w') as f:
            f.write(p.stderr if isinstance(p.stderr, str) else p.stderr.decode(errors='replace'))
        raise MigrationError('Command failed: ' + str(argv[0]) + '; protected error report: ' + report)
    return p.stdout


class Migration:
    def __init__(self, args):
        self.args = args
        self.root = Path(os.environ.get('INSTALL_DIR', '/opt/homebrain'))
        self.env = dict(dotenv_values(self.root / '.env'))
        self.compose = ['docker','compose','-f',str(self.root / 'docker-compose.yml')]
        self.container = run(self.compose + ['ps','-q','nextcloud'], text=True).strip()
        if not self.container:
            raise MigrationError('Nextcloud is not running')
        self.source, self.destination = args.source, args.destination
        if not all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._@-]{0,63}', u) for u in (self.source,self.destination)):
            raise MigrationError('Invalid account ID')
        if self.source.lower() == self.destination.lower():
            raise MigrationError('Source and destination must be different')
        self.job = Path(args.job_dir).resolve() if args.job_dir else None
        self.adapter = '/tmp/homebrain-account-migration.php'
        run(['docker','cp',str(Path(__file__).with_suffix('.php').with_name('nextcloud_account_migration.php')),
             self.container+':'+self.adapter])
        self.state = read_json(self.job / 'journal.json') if self.job else {}
        self.maintenance_owned = False

    def occ(self, *args):
        override = ['-e','HOMEBRAIN_MIGRATION_CLI=1'] if self.maintenance_owned else []
        return run(['docker','exec',*override,'-u','www-data',self.container,'php','occ',*args], text=True)

    def php(self, action, **kwargs):
        override = ['-e','HOMEBRAIN_MIGRATION_CLI=1'] if self.maintenance_owned else []
        raw = run(['docker','exec','-i',*override,'-u','www-data',self.container,'php',self.adapter],
                  input=json.dumps({'action':action,'source':self.source,'destination':self.destination,**kwargs}), text=True)
        return json.loads(raw)

    def pause_writes(self):
        # Web requests and cron stay in maintenance; only this CLI process loads apps.
        config = '<?php if (PHP_SAPI === "cli" && getenv("HOMEBRAIN_MIGRATION_CLI") === "1") { $CONFIG = ["maintenance" => false]; }'
        program = '$p=$argv[1];$s=$argv[2];if(file_exists($p)&&file_get_contents($p)!==$s)exit(1);if(file_put_contents($p,$s)===false)exit(1);chmod($p,0640);'
        run(['docker','exec','-u','www-data',self.container,'php','-r',program,
             '/var/www/html/config/homebrain-migration.config.php',config])
        self.occ('maintenance:mode','--on')
        self.maintenance_owned = True

    def resume_writes(self):
        self.maintenance_owned = False
        self.occ('maintenance:mode','--off')
        run(['docker','exec',self.container,'rm','-f','/var/www/html/config/homebrain-migration.config.php'])

    def record(self, **kwargs):
        self.state.update(kwargs)
        write_json(self.job / 'journal.json', self.state)

    def manifest(self, uid, part):
        data = self.state['inventory']['data_directory']
        # Hash in the container so remote Docker installations need no data-dir host mapping.
        program = r'''$root=$argv[1]; $out=[]; if(is_dir($root)) {
        $it=new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root,FilesystemIterator::SKIP_DOTS));
        foreach($it as $f) { if($f->isLink()) throw new Exception("Symlinks are unsupported");
        if($f->isFile()) $out[substr($f->getPathname(),strlen($root)+1)]=[$f->getSize(),hash_file('sha256',$f->getPathname())]; }}
        ksort($out);echo json_encode((object)$out);'''
        return json.loads(run(['docker','exec',self.container,'php','-r',program,f'{data}/{uid}/{part}'],text=True))

    def inspect(self):
        users = json.loads(self.occ('user:list','--info','--output=json'))
        if self.source not in users or self.destination not in users:
            raise MigrationError('Both account IDs must exist with their exact spelling')
        status = json.loads(self.occ('status', '--output=json'))
        if status.get('maintenance'):
            raise MigrationError('Nextcloud is already in maintenance mode')
        info = self.php('inspect')
        supported = {'users','accounts','accounts_data','authtoken','twofactor_providers','twofactor_backupcodes',
                     'twofactor_totp_secrets','notifications','notifications_settings','notifications_pushhash',
                     'preferences','preferences_ex','profile_config','user_status','mounts','activity','files_trash',
                     'recent_contact','vcategory','calendars','addressbooks','cards','share','circles_member','talk_attendees'}
        unsupported = [r for r in info['owned'] if r['table'] not in supported and not r['table'].startswith('passwords_')]
        if info['storage_unsupported'] or 'files_external' in info['enabled_apps'] or 'groupfolders' in info['enabled_apps']:
            unsupported.append({'table':'storage','reason':'External, group or encrypted storage requires a separate migration'})
        if info['talk_authored'] or info['talk_real_rooms']:
            unsupported.append({'table':'talk','reason':'User conversations require a separate migration'})
        info['unsupported'] = unsupported
        info['users'] = {u:{'enabled': users[u]['enabled'], 'groups':users[u]['groups']} for u in (self.source,self.destination)}
        return info

    def password_digest(self, uid, password_file):
        if not password_file:
            raise MigrationError('Password verification requires source and destination password files')
        pw = Path(password_file).read_text().strip()
        base = 'http://127.0.0.1:' + self.env.get('NEXTCLOUD_PORT','8080')
        r = requests.get(base+'/index.php/apps/passwords/api/1.0/password/list', auth=(uid,pw), timeout=60)
        if r.status_code != 200 or not isinstance(r.json(),list):
            raise MigrationError('Could not decrypt the Passwords app through the account API')
        values = [{k:p.get(k) for k in ('id','label','username','password','notes','url','customFields')} for p in r.json()]
        values.sort(key=lambda p:p['id'])
        return hmac.new(bytes.fromhex(self.state['verify_key']),json.dumps(values,sort_keys=True).encode(),hashlib.sha256).hexdigest()

    def verify_webdav(self):
        password = Path(self.args.destination_password_file).read_text().strip()
        base = 'http://127.0.0.1:' + self.env.get('NEXTCLOUD_PORT','8080')
        folder = self.state.get('folder','')
        with requests.Session() as session:
            session.auth = (self.destination,password)
            for path, (size, digest) in read_json(self.job/'files.json').items():
                remote = '/'.join(p for p in (folder,path) if p)
                url = base+'/remote.php/dav/files/'+quote(self.destination,safe='')+'/'+quote(remote,safe='/')
                with session.get(url,stream=True,timeout=60) as response:
                    if response.status_code != 200:
                        raise MigrationError('Destination WebDAV could not read transferred content')
                    actual=hashlib.sha256(); total=0
                    for chunk in response.iter_content(1024*1024):
                        actual.update(chunk); total+=len(chunk)
                    if total!=size or actual.hexdigest()!=digest:
                        raise MigrationError('Destination WebDAV content does not match the recovery point')

    def prepare(self):
        if self.state:
            expected = (self.source,self.destination)
            if (self.state.get('source'),self.state.get('destination')) != expected:
                raise MigrationError('Job belongs to different accounts')
            if self.state.get('destination_root') != self.args.destination_root:
                raise MigrationError('Resume must keep the original destination mode')
            return
        info = self.inspect()
        if not self.args.destination_password_file:
            raise MigrationError('A destination password file is required for WebDAV verification')
        if info['unsupported']:
            raise MigrationError('Unsupported app-owned data; run inspect for details')
        if self.args.replace_destination_files and not self.args.destination_root:
            raise MigrationError('--replace-destination-files requires --destination-root')
        if info['target_passwords']:
            raise MigrationError('Destination Passwords app contains data; no overwrite is allowed')
        if self.source.lower() == self.env.get('NEXTCLOUD_ADMIN_USER','admin').lower():
            raise MigrationError('Migrating the configured owner out is not supported')
        self.job.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.job.chmod(0o700)
        self.php('init-destination')
        self.record(source=self.source,destination=self.destination,inventory=info,
                    destination_root=self.args.destination_root,phase='preflight',verify_key=secrets.token_hex(32),
                    replace_files=self.args.replace_destination_files,retire=self.args.retire_source)
        for part in ('files','files_versions','files_trashbin'):
            source = self.manifest(self.source,part)
            target = self.manifest(self.destination,part)
            if target and (part!='files' or (self.args.destination_root and not self.args.replace_destination_files)):
                raise MigrationError(f'Destination {part} is not empty; resolve the conflict before migration')
            write_json(self.job / (part+'.json'),source)
            if part == 'files':
                write_json(self.job / 'target-files.json', target)
        self.record(dav=self.php('dav-digests',principal='principals/users/'+self.source),
                    target_dav=self.php('dav-digests',principal='principals/users/'+self.destination))
        has_passwords = any(r['table']=='passwords_password' for r in info['owned'])
        self.record(has_passwords=has_passwords)
        if has_passwords:
            self.record(password_digest=self.password_digest(self.source,self.args.source_password_file))
            if not self.args.destination_password_file:
                raise MigrationError('A destination password file is required for final verification')
        needed = sum(v[0] for v in read_json(self.job/'target-files.json').values()) + sum(v[0] for part in ('files','files_versions','files_trashbin')
                     for v in read_json(self.job/(part+'.json')).values())
        if shutil.disk_usage(self.job).free < needed*2 + 1024**3:
            raise MigrationError('Insufficient space for the verified recovery point')
        self.record(phase='prepared', target_top=self.php('top-level'))

    def snapshot(self):
        if self.state['phase'] != 'prepared':
            return
        self.occ('maintenance:mode','--on')
        try:
            info = self.state['inventory']
            data = info['data_directory']
            for uid in (self.source,self.destination):
                (self.job/uid).mkdir(exist_ok=True)
                run(['docker','cp',self.container+':'+data+'/'+uid+'/.',str(self.job / uid)])
            (self.job/'nextcloud-config').mkdir(exist_ok=True)
            run(['docker','cp',self.container+':/var/www/html/config/.',str(self.job/'nextcloud-config')])
            if info['dbtype']=='sqlite3':
                program = '$CONFIG=[];require "/var/www/html/config/config.php";$p=new PDO("sqlite:".$CONFIG["datadirectory"]."/".$CONFIG["dbname"].".db");if(file_exists("/tmp/hb-account-recovery.sqlite"))unlink("/tmp/hb-account-recovery.sqlite");$p->exec("VACUUM INTO \'/tmp/hb-account-recovery.sqlite\'");'
                run(['docker','exec',self.container,'php','-r',program])
                run(['docker','cp',self.container+':/tmp/hb-account-recovery.sqlite',str(self.job/'database.sqlite')])
                run(['docker','exec',self.container,'rm','/tmp/hb-account-recovery.sqlite'])
            elif info['dbtype']=='mysql':
                db = run(self.compose+['ps','-q','db'],text=True).strip()
                if not db: raise MigrationError('The recovery adapter requires the local database service')
                with (self.job/'database.sql').open('wb') as f:
                    p=subprocess.run(['docker','exec',db,'sh','-c',
                        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb-dump -uroot --single-transaction --routines "$MYSQL_DATABASE"'],stdout=f,stderr=subprocess.PIPE)
                if p.returncode: raise MigrationError('Database recovery dump failed')
            else:
                raise MigrationError('Unsupported database backup adapter')
            for part in ('files','files_versions','files_trashbin'):
                expected=read_json(self.job/(part+'.json'))
                root=self.job/self.source/part
                actual={str(p.relative_to(root)):[p.stat().st_size,file_hash(p)]
                        for p in root.rglob('*') if p.is_file()}
                if actual!=expected: raise MigrationError('Recovery-point content verification failed')
            root=self.job/self.destination/'files'
            target={str(p.relative_to(root)):[p.stat().st_size,file_hash(p)]
                    for p in root.rglob('*') if p.is_file()}
            if target!=read_json(self.job/'target-files.json'):
                raise MigrationError('Destination recovery-point content verification failed')
            self.occ('user:disable',self.source)
            self.occ('user:disable',self.destination)
            self.record(phase='backed_up')
        finally:
            self.occ('maintenance:mode','--off')

    def execute(self):
        self.prepare()
        if self.state['phase']=='preflight':
            raise MigrationError('Preflight failed; inspect and create a new job after resolving conflicts')
        self.snapshot()
        if self.state['phase'] in ('backed_up','files_pending','files_done'):
            self.pause_writes()
        data=self.state['inventory']['data_directory']
        if self.state['phase']=='backed_up':
            if self.state['replace_files']:
                # Move into the recovery point; never recursively delete destination data.
                program = '$p=$argv[1];if(!is_dir($p.".before-homebrain-migration")){if(!rename($p,$p.".before-homebrain-migration"))exit(1);}if(!is_dir($p)&&!mkdir($p))exit(1);'
                run(['docker','exec','-u','www-data',self.container,'php','-r',program,f'{data}/{self.destination}/files'])
                self.occ('files:scan','--path='+self.destination+'/files','--quiet')
            self.record(phase='files_pending')
        if self.state['phase']=='files_pending':
            expected=read_json(self.job/'files.json')
            if self.state['destination_root']:
                actual=self.manifest(self.destination,'files')
                if actual!=expected:
                    if actual: raise MigrationError('Unexpected destination content; recover before retrying')
                    self.occ('files:transfer-ownership','--move',self.source,self.destination,'--no-interaction')
                folder=''
            else:
                new=set(self.php('top-level'))-set(self.state['target_top'])
                if not new:
                    self.occ('files:transfer-ownership','--use-user-id',self.source,self.destination,'--no-interaction')
                    new=set(self.php('top-level'))-set(self.state['target_top'])
                if len(new)!=1: raise MigrationError('Cannot identify the transferred folder; recovery point retained')
                folder=new.pop()
            if self.manifest(self.destination,'files'+('/'+folder if folder else ''))!=expected:
                raise MigrationError('Transferred files do not match the recovery point')
            self.record(phase='files_done', folder=folder)
        if self.state['phase']=='files_done':
            self.php('extras',folder=self.state.get('folder',''))
            for calendar in self.php('inspect')['calendars']:
                self.occ('dav:move-calendar',calendar['uri'],self.source,self.destination,'--force')
            if self.state['has_passwords']:
                inventory=self.php('inspect')
                if any(r['table']=='passwords_password' for r in inventory['owned']):
                    if inventory['target_passwords']: raise MigrationError('Password move has conflicting destination data')
                    self.occ('passwords:user:move',self.source,self.destination,'--no-interaction')
            self.record(phase='verify')
        if self.state['phase']=='verify':
            self.resume_writes()
            for part in ('files','files_versions','files_trashbin'):
                folder=self.state.get('folder','')
                path=part+('/'+folder if folder and part!='files_trashbin' else '')
                if self.manifest(self.destination,path)!=read_json(self.job/(part+'.json')):
                    raise MigrationError('Content mismatch in '+part+'; source was not deleted')
            if self.state.get('folder'):
                combined=read_json(self.job/'target-files.json')
                combined.update({self.state['folder']+'/'+k:v for k,v in read_json(self.job/'files.json').items()})
                if self.manifest(self.destination,'files')!=combined:
                    raise MigrationError('Existing destination files changed during migration')
            dav=self.php('dav-digests',principal='principals/users/'+self.destination)
            for part,hashes in dav.items():
                if sorted(hashes)!=sorted(self.state['dav'][part]+self.state['target_dav'][part]):
                    raise MigrationError('Calendar/contact content mismatch')
            remaining=self.php('inspect')
            self.php('verify-shares',source_shares=self.state['inventory']['source_shares'],
                     incoming_shares=self.state['inventory']['incoming_shares'])
            if remaining['source_shares'] or remaining['incoming_shares']:
                raise MigrationError('Shares still belong to the source')
            self.occ('user:enable',self.destination)
            self.verify_webdav()
            if self.state['has_passwords']:
                if self.password_digest(self.destination,self.args.destination_password_file)!=self.state['password_digest']:
                    raise MigrationError('Decrypted passwords differ; source was not deleted')
            self.record(phase='verified')
        if self.state['phase']=='verified':
            # Remove only the matching local source integration, after the destination is verified.
            import integrations
            accounts=integrations._load_nc_accounts()
            base='http://127.0.0.1:'+self.env.get('NEXTCLOUD_PORT','8080')
            old=[a for a in accounts if a.get('user')==self.source and a.get('base_url','').rstrip('/')==base]
            if old:
                dest=next((a for a in accounts if a.get('user')==self.destination and a.get('base_url','').rstrip('/')==base),None)
                if not dest: raise MigrationError('Connect and verify the destination integration before retiring the source')
                # Validate the stored integration token against OCS before deleting configuration.
                r=requests.get(base+'/ocs/v1.php/cloud/user?format=json',auth=(self.destination,integrations._decrypt_secret(dest['token'])),headers={'OCS-APIRequest':'true'},timeout=30)
                if r.status_code!=200 or r.json().get('ocs',{}).get('data',{}).get('id')!=self.destination:
                    raise MigrationError('Destination integration authentication failed')
                shutil.copy2(integrations.NC_ACCOUNTS_FILE,self.job/'nc-accounts-before.json')
                for a in old: integrations.remove_nc_account(a['name'])
            if self.state['retire']:
                users=json.loads(self.occ('user:list','--output=json'))
                if self.source in users: self.occ('user:delete',self.source)
            self.record(phase='complete',completed_at=time.time())
        return {'status':self.state['phase'],'source_retired':self.state['retire'],
                'recovery_point':str(self.job),'client_action':'Sign in as the destination account; source tokens are not migrated'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['inspect','run','resume'])
    p.add_argument('source');p.add_argument('destination')
    p.add_argument('--job-dir');p.add_argument('--destination-root',action='store_true')
    p.add_argument('--replace-destination-files',action='store_true')
    p.add_argument('--retire-source',action='store_true')
    p.add_argument('--source-password-file');p.add_argument('--destination-password-file')
    args=p.parse_args()
    if args.action!='inspect' and not args.job_dir: p.error('--job-dir is required')
    m = None
    try:
        with locked(os.environ.get('HOMEBRAIN_BACKUP_LOCK','/var/run/homebrain-backup.lock'),timeout=1), \
             locked(Path(os.environ.get('HOMEBRAIN_STATE_DIR','/var/lib/homebrain'))/'accounts.lock',timeout=1):
            m=Migration(args)
            if args.action=='inspect':
                info=m.inspect()
                # Public-link tokens are needed for verification, not console output.
                info['source_shares']=len(info['source_shares'])
                info['incoming_shares']=len(info['incoming_shares'])
                print(json.dumps(info,indent=2))
            else:
                print(json.dumps(m.execute(),indent=2))
        return 0
    except (MigrationError,requests.RequestException,ValueError) as e:
        print('Migration incomplete: '+str(e),file=sys.stderr)
        print('Source retirement is blocked. Use the protected journal to resume; affected accounts may remain disabled.',file=sys.stderr)
        return 1
    finally:
        if m and m.maintenance_owned:
            m.resume_writes()


if __name__=='__main__':
    raise SystemExit(main())
