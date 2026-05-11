<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Hashing;
use Tlinh\Json;

// POST /api/admin/inject-test — seeds a verifiable bootstrap row.
//
// Nginx-level guard: the vhost (see §10 Step 6) restricts this location to
// 127.0.0.1 / ::1; non-loopback callers are 403'd before PHP runs. PHP still
// re-checks defensively in case the location block is misconfigured or this
// handler is invoked from another path.
final class Admin
{
    public static function injectTest(): void
    {
        $remote = $_SERVER['REMOTE_ADDR'] ?? '';
        if ($remote !== '127.0.0.1' && $remote !== '::1') {
            Json::out(403, ['error' => 'forbidden', 'message' => 'admin endpoints are localhost-only']);
        }

        $body  = Json::body();
        $token = $body['token'] ?? null;
        if (!is_string($token) || !preg_match('/^[0-9a-fA-F]{4,64}$/', $token)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'token must match ^[0-9a-fA-F]{4,64}$']);
        }

        $url           = "bootstrap-test://$token";
        $title         = "[BOOTSTRAP TEST $token] sample article";
        $content       = 'Test content for verification — VinFast Q1 sample';
        $urlHash       = Hashing::urlHash($url);
        $titleHash     = Hashing::titleHash($title);

        $pdo = Db::pdo();
        $pdo->prepare(
            'INSERT IGNORE INTO articles
                (url, canonical_url, url_hash, title, title_hash, source, content,
                 crawled_at, extracted_at, scored_at, score, score_reason)
             VALUES (:url, :canonical, :url_hash, :title, :title_hash, :source, :content,
                     NOW(), NOW(), NOW(), 5, :reason)'
        )->execute([
            ':url'        => $url,
            ':canonical'  => $url,
            ':url_hash'   => $urlHash,
            ':title'      => $title,
            ':title_hash' => $titleHash,
            ':source'     => 'bootstrap-inject',
            ':content'    => $content,
            ':reason'     => 'bootstrap verification',
        ]);
        $newId = (int)$pdo->lastInsertId();

        if ($newId > 0) {
            Json::out(201, [
                'id'      => $newId,
                'created' => true,
                'url'     => $url,
                'score'   => 5,
            ]);
        }

        $sel = $pdo->prepare('SELECT id FROM articles WHERE url_hash = ?');
        $sel->execute([$urlHash]);
        $id = (int)($sel->fetchColumn() ?: 0);
        Json::out(200, [
            'id'      => $id,
            'created' => false,
            'url'     => $url,
            'score'   => 5,
        ]);
    }
}
