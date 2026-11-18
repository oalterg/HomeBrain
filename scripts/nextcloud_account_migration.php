<?php
/** Versioned Nextcloud 33 adapter. Executed through CLI, never placed in webroot. */
define('OC_CONSOLE', 1);
require '/var/www/html/lib/base.php';
set_exception_handler(function (Throwable $e) { fwrite(STDERR, $e->getMessage() . "\n"); exit(1); });
$args = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
$config = \OCP\Server::get(\OCP\IConfig::class);
if (explode('.', $config->getSystemValue('version'))[0] !== '33') {
    throw new RuntimeException('Only Nextcloud 33 is supported by this migration adapter');
}
$c = \OC::$server->getDatabaseConnection()->getInner();
$prefix = $config->getSystemValue('dbtableprefix', 'oc_');
$table = fn($name) => $c->quoteIdentifier($prefix . $name);
$src = $args['source']; $dst = $args['destination'];
$sourcePrincipal = 'principals/users/' . $src;
$destPrincipal = 'principals/users/' . $dst;
$action = $args['action'];
if ($action === 'inspect') {
    $out = ['version' => $config->getSystemValue('version'),
            'dbtype' => $config->getSystemValue('dbtype'),
            'data_directory' => $config->getSystemValue('datadirectory'),
            'owned' => [], 'calendars' => [], 'addressbooks' => []];
    $schema = $c->createSchemaManager();
    foreach ($schema->listTableNames() as $t) {
        if (!str_starts_with($t, $prefix)) continue;
        foreach ($schema->listTableColumns($t) as $name => $col) {
            if (!in_array($name, ['uid','user_id','userid','user','owner','uid_owner','uid_initiator','actor_id','principaluri'])) continue;
            $count = $c->executeQuery('SELECT COUNT(*) FROM ' . $c->quoteIdentifier($t) .
                ' WHERE ' . $c->quoteIdentifier($name) . ' IN (?,?)', [$src, $sourcePrincipal])->fetchOne();
            if ($count) $out['owned'][] = ['table' => substr($t, strlen($prefix)), 'column' => $name, 'count' => (int)$count];
        }
    }
    foreach (['calendars','addressbooks'] as $t) {
        $out[$t] = $c->executeQuery('SELECT id,uri FROM ' . $table($t) . ' WHERE principaluri=?', [$sourcePrincipal])->fetchAllAssociative();
    }
    $out['target_passwords'] = in_array($prefix.'passwords_password', $schema->listTableNames())
        ? (int)$c->executeQuery('SELECT COUNT(*) FROM '.$table('passwords_password').' WHERE user_id=?', [$dst])->fetchOne() : 0;
    $out['source_shares'] = $c->executeQuery('SELECT id,share_type,token,permissions,share_with,uid_owner,uid_initiator FROM '.$table('share').' WHERE uid_owner=? OR uid_initiator=?', [$src,$src])->fetchAllAssociative();
    $out['incoming_shares'] = $c->executeQuery('SELECT id,share_type,token,permissions,uid_owner FROM '.$table('share').' WHERE share_type=0 AND share_with=?', [$src])->fetchAllAssociative();
    $out['storage_unsupported'] = (bool)$config->getSystemValue('objectstore', []) ||
        $config->getAppValue('core', 'encryption_enabled', 'no') === 'yes';
    $out['enabled_apps'] = \OCP\Server::get(\OCP\App\IAppManager::class)->getEnabledApps();
    // Talk's generated sample/changelog rooms are disposable only if there are no authored messages.
    $out['talk_authored'] = (int)$c->executeQuery('SELECT COUNT(*) FROM '.$table('comments')." WHERE object_type='chat' AND actor_id=?", [$src])->fetchOne();
    $out['talk_real_rooms'] = in_array($prefix.'talk_attendees', $schema->listTableNames()) ? (int)$c->executeQuery('SELECT COUNT(*) FROM '.$table('talk_attendees').' a JOIN '.$table('talk_rooms')." r ON a.room_id=r.id WHERE a.actor_id=? AND r.type<>4 AND r.object_type NOT IN ('sample','note_to_self')", [$src])->fetchOne() : 0;
    echo json_encode($out, JSON_THROW_ON_ERROR);
} elseif ($action === 'init-destination') {
    \OCP\Server::get(\OCP\Files\IRootFolder::class)->getUserFolder($dst)->getDirectoryListing();
    echo json_encode(['status'=>'initialized']);
} elseif ($action === 'verify-shares') {
    foreach (['source_shares','incoming_shares'] as $kind) {
        foreach ($args[$kind] as $original) {
            $row=$c->executeQuery('SELECT * FROM '.$table('share').' WHERE id=?',[$original['id']])->fetchAssociative();
            if (!$row) throw new RuntimeException('A transferred share is missing');
            foreach ($original as $key=>$value) {
                $expected=($kind==='source_shares' && in_array($key,['share_with','uid_owner','uid_initiator']) && $value===$src) ? $dst : $value;
                if ((string)$row[$key] !== (string)$expected) throw new RuntimeException('Transferred share changed: '.$key);
            }
            if ($kind==='incoming_shares' && $row['share_with']!==$dst) throw new RuntimeException('Share was not transferred to destination');
        }
    }
    echo json_encode(['status'=>'verified']);
} elseif ($action === 'top-level') {
    $path=$config->getSystemValue('datadirectory').'/'.$dst.'/files';
    echo json_encode(array_values(array_diff(scandir($path), ['.','..'])));
} elseif ($action === 'extras') {
    \OC_Util::setupFS($dst);
    \OC\Files\Filesystem::initMountPoints($src);
    \OC\Files\Filesystem::initMountPoints($dst);
    $view = new \OC\Files\View();
    $folder=$args['folder'] ?? '';
    foreach (['files_versions', 'files_trashbin'] as $part) {
        $target=$dst.'/'.$part.($part==='files_versions' && $folder!=='' ? '/'.$folder : '');
        if ($part==='files_versions' && $folder!=='' && !$view->is_dir($dst.'/'.$part)) $view->mkdir($dst.'/'.$part);
        if (!$view->is_dir($src.'/'.$part)) continue; // completed before an interruption
        if ($view->is_dir($target)) {
            // A target may contain empty trash subdirectories only.
            $root = $config->getSystemValue('datadirectory').'/'.$target;
            $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS));
            foreach ($it as $item) if ($item->isFile()) throw new RuntimeException('Destination '.$part.' contains data');
            if (!$view->rmdir($target)) throw new RuntimeException('Cannot clear empty '.$part);
        }
        if (!$view->rename($src.'/'.$part, $target)) throw new RuntimeException('Cannot transfer '.$part);
    }
    $c->beginTransaction();
    try {
        if ($folder!=='') {
            $trash=$c->executeQuery('SELECT auto_id,location FROM '.$table('files_trash').' WHERE user=?',[$src])->fetchAllAssociative();
            foreach($trash as $row) $c->executeStatement('UPDATE '.$table('files_trash').' SET location=? WHERE auto_id=?', [rtrim($folder.'/'.ltrim($row['location'],'/'),'/'),$row['auto_id']]);
        }
        foreach ([['files_trash','user'],['files_trash','deleted_by'],['activity','user'],['activity','affecteduser'],['vcategory','uid'],['recent_contact','uid']] as [$t,$col]) {
            $c->executeStatement('UPDATE '.$table($t).' SET '.$c->quoteIdentifier($col).'=? WHERE '.$c->quoteIdentifier($col).'=?', [$dst,$src]);
        }
        $books = $c->executeQuery('SELECT id,uri FROM '.$table('addressbooks').' WHERE principaluri=?', [$sourcePrincipal])->fetchAllAssociative();
        foreach ($books as $book) {
            $uri = $book['uri'];
            if ($c->executeQuery('SELECT COUNT(*) FROM '.$table('addressbooks').' WHERE principaluri=? AND uri=?', [$destPrincipal,$uri])->fetchOne()) {
                $uri = 'migrated-'.$src.'-'.$book['id'];
            }
            $c->executeStatement('UPDATE '.$table('addressbooks').' SET principaluri=?,uri=? WHERE id=?', [$destPrincipal,$uri,$book['id']]);
        }
        $c->commit();
    } catch (Throwable $e) { $c->rollBack(); throw $e; }
    echo json_encode(['status'=>'moved']);
} elseif ($action === 'dav-digests') {
    $result = [];
    foreach ([['calendarobjects','calendars','calendarid','calendardata'],['cards','addressbooks','addressbookid','carddata']] as [$objects,$parents,$fk,$data]) {
        $rows = $c->executeQuery('SELECT o.'.$c->quoteIdentifier($data).' FROM '.$table($objects).' o JOIN '.$table($parents).' p ON o.'.$fk.'=p.id WHERE p.principaluri=?', [$args['principal']])->fetchAllAssociative();
        $hashes = array_map(fn($row) => hash('sha256', $row[$data]), $rows);
        sort($hashes); $result[$objects] = $hashes;
    }
    echo json_encode($result);
} else {
    throw new RuntimeException('Unknown migration action');
}
