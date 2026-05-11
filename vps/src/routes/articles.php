<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Hashing;
use Tlinh\Json;

// POST /api/articles, GET /api/articles, GET /api/articles/{id},
// PATCH /api/articles/{id}/extract — see PLAN-v2.md §4 for the contract.
final class Articles
{
    /** POST /api/articles — idempotent on url_hash. */
    public static function create(): void
    {
        $body = Json::body();
        $url           = self::requireString($body, 'url', 2048);
        $canonicalUrl  = self::requireString($body, 'canonical_url', 2048);
        $title         = self::optionalString($body, 'title', 1024) ?? '';
        $source        = self::optionalString($body, 'source', 255);
        $publishedAt   = self::optionalString($body, 'published_at', 32);

        // Reserved URL scheme — only POST /api/admin/inject-test may use it.
        // Check BOTH `url` and `canonical_url` (per Codex impl-review): clients
        // could otherwise smuggle a bootstrap-test row by setting `url` while
        // leaving canonical_url benign.
        if (self::isReservedScheme($url) || self::isReservedScheme($canonicalUrl)) {
            Json::out(403, [
                'error'   => 'reserved_url_scheme',
                'message' => 'bootstrap-test:// is reserved for /api/admin/inject-test',
            ]);
        }

        $urlHash   = Hashing::urlHash($canonicalUrl);
        $titleHash = Hashing::titleHash($title);

        $pdo = Db::pdo();
        $pdo->prepare(
            'INSERT IGNORE INTO articles
                (url, canonical_url, url_hash, title, title_hash, source, published_at)
             VALUES (:url, :canonical, :url_hash, :title, :title_hash, :source, :published)'
        )->execute([
            ':url'        => $url,
            ':canonical'  => $canonicalUrl,
            ':url_hash'   => $urlHash,
            ':title'      => $title,
            ':title_hash' => $titleHash,
            ':source'     => $source,
            ':published'  => $publishedAt,
        ]);
        $lastId = (int)$pdo->lastInsertId();
        if ($lastId > 0) {
            Json::out(201, [
                'id'         => $lastId,
                'created'    => true,
                'url_hash'   => $urlHash,
                'title_hash' => $titleHash,
            ]);
        }

        // Row already existed — look up its id (UNIQUE key uq_url_hash).
        $stmt = $pdo->prepare('SELECT id FROM articles WHERE url_hash = ?');
        $stmt->execute([$urlHash]);
        $id = (int)($stmt->fetchColumn() ?: 0);
        Json::out(200, [
            'id'         => $id,
            'created'    => false,
            'url_hash'   => $urlHash,
            'title_hash' => $titleHash,
        ]);
    }

    /** GET /api/articles — filtered list. */
    public static function list(): void
    {
        $q       = $_GET;
        $clauses = [];
        $params  = [];
        $maxRetries = (int)$_ENV['MAX_RETRIES'];

        // stage predicates per §4 (queue stages exclude discarded; terminals don't).
        $stage = $q['stage'] ?? null;
        switch ($stage) {
            case 'new':
                $clauses[] = 'extracted_at IS NULL AND final_state IS NULL AND retry_count < :max_retries';
                $params[':max_retries'] = $maxRetries;
                break;
            case 'extracted':
                $clauses[] = 'extracted_at IS NOT NULL AND scored_at IS NULL AND final_state IS NULL AND retry_count < :max_retries';
                $params[':max_retries'] = $maxRetries;
                break;
            case 'scored':
                $clauses[] = 'scored_at IS NOT NULL AND notified_at IS NULL AND final_state IS NULL AND retry_count < :max_retries';
                $params[':max_retries'] = $maxRetries;
                break;
            case 'failed':
                $clauses[] = 'failed_at IS NOT NULL AND final_state IS NULL';
                break;
            case 'notified':
                $clauses[] = 'notified_at IS NOT NULL';
                break;
            case 'brainstormed':
                $clauses[] = 'brainstormed_at IS NOT NULL';
                break;
            case null:
                break;
            default:
                Json::out(400, ['error' => 'bad_query', 'message' => 'invalid stage']);
        }

        // Score filters.
        if (isset($q['score'])) {
            $clauses[] = 'score = :score';
            $params[':score'] = (int)$q['score'];
        }
        if (isset($q['min_score'])) {
            $clauses[] = 'score >= :min_score';
            $params[':min_score'] = (int)$q['min_score'];
        }
        if (isset($q['max_score'])) {
            $clauses[] = 'score <= :max_score';
            $params[':max_score'] = (int)$q['max_score'];
        }
        if (($q['not_scored']     ?? '') === '1') { $clauses[] = 'scored_at IS NULL'; }
        if (($q['not_notified']   ?? '') === '1') { $clauses[] = 'notified_at IS NULL'; }
        if (($q['not_brainstormed'] ?? '') === '1') { $clauses[] = 'brainstormed_at IS NULL'; }

        // final_state: 'active' is server-side alias for IS NULL (per ISSUE-21).
        if (isset($q['final_state'])) {
            switch ($q['final_state']) {
                case 'active':
                    $clauses[] = 'final_state IS NULL';
                    break;
                case 'discarded':
                case 'archived':
                    $clauses[] = 'final_state = :final_state';
                    $params[':final_state'] = $q['final_state'];
                    break;
                default:
                    Json::out(400, ['error' => 'bad_query', 'message' => 'invalid final_state']);
            }
        }

        // Time-window filters.
        if (isset($q['last_hours'])) {
            $clauses[] = 'crawled_at >= NOW() - INTERVAL :last_hours HOUR';
            $params[':last_hours'] = (int)$q['last_hours'];
        } elseif (isset($q['since'])) {
            $clauses[] = 'crawled_at >= :since';
            $params[':since'] = $q['since'];
        } else {
            // Default: last 7 days.
            $clauses[] = 'crawled_at >= NOW() - INTERVAL 7 DAY';
        }

        if (isset($q['search']) && $q['search'] !== '') {
            $clauses[] = '(title LIKE :search OR content LIKE :search)';
            $params[':search'] = '%' . str_replace(['%', '_'], ['\%', '\_'], (string)$q['search']) . '%';
        }

        $limit = (int)($q['limit'] ?? 50);
        if ($limit < 1)   { $limit = 50; }
        if ($limit > 200) { $limit = 200; }

        $where = $clauses ? 'WHERE ' . implode(' AND ', $clauses) : '';
        // Priority ordering: bootstrap-test rows first (verification doesn't starve).
        $sql = "SELECT id, url, canonical_url, url_hash, title, title_hash, source,
                       content, published_at, crawled_at, extracted_at, scored_at,
                       notified_at, brainstormed_at, failed_at, score, score_reason,
                       ideas, telegram_msg_id, last_error, retry_count, final_state,
                       notify_claimed_at, notify_claim_owner
                FROM articles
                $where
                ORDER BY CASE WHEN url LIKE 'bootstrap-test://%' THEN 0 ELSE 1 END,
                         scored_at DESC, crawled_at DESC
                LIMIT $limit";

        $stmt = Db::pdo()->prepare($sql);
        foreach ($params as $k => $v) {
            $stmt->bindValue($k, $v, is_int($v) ? \PDO::PARAM_INT : \PDO::PARAM_STR);
        }
        $stmt->execute();
        $rows = $stmt->fetchAll();
        // Decode ideas JSON column for clients.
        foreach ($rows as &$r) {
            if (isset($r['ideas']) && is_string($r['ideas']) && $r['ideas'] !== '') {
                $decoded = json_decode($r['ideas'], true);
                $r['ideas'] = is_array($decoded) ? $decoded : null;
            }
        }
        unset($r);
        Json::out(200, ['rows' => $rows, 'count' => count($rows)]);
    }

    /** GET /api/articles/{id} */
    public static function show(string $id): void
    {
        $stmt = Db::pdo()->prepare('SELECT * FROM articles WHERE id = ?');
        $stmt->execute([(int)$id]);
        $row = $stmt->fetch();
        if (!$row) {
            Json::out(404, ['error' => 'not_found']);
        }
        if (isset($row['ideas']) && is_string($row['ideas']) && $row['ideas'] !== '') {
            $decoded = json_decode($row['ideas'], true);
            $row['ideas'] = is_array($decoded) ? $decoded : null;
        }
        Json::out(200, $row);
    }

    /** PATCH /api/articles/{id}/extract — CAS via extracted_at IS NULL. */
    public static function extract(string $id): void
    {
        $body = Json::body();
        $content = self::requireString($body, 'content', 4_000_000);
        $title   = self::optionalString($body, 'title', 1024);
        $source  = self::optionalString($body, 'source', 255);

        $pdo = Db::pdo();
        $set = ['extracted_at = NOW()', 'content = :content', 'failed_at = NULL', 'last_error = NULL'];
        $params = [':content' => $content, ':id' => (int)$id];
        if ($title !== null) {
            $set[] = 'title = :title';
            $set[] = 'title_hash = :title_hash';
            $params[':title']      = $title;
            $params[':title_hash'] = Hashing::titleHash($title);
        }
        if ($source !== null) {
            $set[] = 'source = :source';
            $params[':source'] = $source;
        }
        $sql = 'UPDATE articles SET ' . implode(', ', $set)
             . ' WHERE id = :id AND extracted_at IS NULL';
        $stmt = $pdo->prepare($sql);
        $stmt->execute($params);
        if ($stmt->rowCount() === 0) {
            // Either already extracted, or row missing.
            $exists = $pdo->prepare('SELECT extracted_at FROM articles WHERE id = ?');
            $exists->execute([(int)$id]);
            $row = $exists->fetch();
            if (!$row) {
                Json::out(404, ['error' => 'not_found']);
            }
            Json::out(200, ['id' => (int)$id, 'updated' => false, 'reason' => 'already_extracted']);
        }
        $stmt = $pdo->prepare('SELECT extracted_at FROM articles WHERE id = ?');
        $stmt->execute([(int)$id]);
        $extractedAt = $stmt->fetchColumn();
        Json::out(200, ['id' => (int)$id, 'updated' => true, 'extracted_at' => $extractedAt]);
    }

    private static function isReservedScheme(string $u): bool
    {
        // Case-insensitive: URI schemes are case-insensitive per RFC 3986 §3.1,
        // and we don't want `Bootstrap-Test://` to bypass the admin-only
        // restriction. Match anywhere a leading-whitespace caller might try.
        return preg_match('#^\s*bootstrap-test://#i', $u) === 1;
    }

    private static function requireString(array $body, string $key, int $max): string
    {
        $v = $body[$key] ?? null;
        if (!is_string($v) || $v === '') {
            Json::out(400, ['error' => 'bad_request', 'message' => "missing required string: $key"]);
        }
        if (strlen($v) > $max) {
            Json::out(400, ['error' => 'bad_request', 'message' => "$key too long (>$max bytes)"]);
        }
        return $v;
    }

    private static function optionalString(array $body, string $key, int $max): ?string
    {
        if (!isset($body[$key]) || $body[$key] === null || $body[$key] === '') {
            return null;
        }
        $v = $body[$key];
        if (!is_string($v)) {
            Json::out(400, ['error' => 'bad_request', 'message' => "$key must be a string"]);
        }
        if (strlen($v) > $max) {
            Json::out(400, ['error' => 'bad_request', 'message' => "$key too long (>$max bytes)"]);
        }
        return $v;
    }
}
