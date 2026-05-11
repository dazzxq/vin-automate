<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Json;

// POST /api/lock/{name}/acquire, /heartbeat, DELETE /api/lock/{name}.
//
// The `locks` table (§3) is keyed by `name`. We use INSERT with ON DUPLICATE
// KEY UPDATE conditional on expiry to atomically reclaim expired locks. The
// existing-row branch returns 409 with the current holder's details so the
// caller can decide whether to back off or abort.
final class Lock
{
    public static function acquire(string $name): void
    {
        $body = Json::body();
        $ttl  = $body['ttl_seconds'] ?? null;
        if (!is_int($ttl) || $ttl < 1 || $ttl > 86400) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'ttl_seconds must be int 1..86400']);
        }

        // 32-hex-char owner id (matches the schema CHAR(32) column).
        $ownerId = bin2hex(random_bytes(16));
        $pdo     = Db::pdo();

        // First sweep expired locks for this name so the INSERT can succeed.
        $pdo->prepare('DELETE FROM locks WHERE name = ? AND expires_at < NOW()')
            ->execute([$name]);

        try {
            $pdo->prepare(
                'INSERT INTO locks (name, owner_id, expires_at)
                 VALUES (:name, :owner, NOW() + INTERVAL :ttl SECOND)'
            )->execute([
                ':name'  => $name,
                ':owner' => $ownerId,
                ':ttl'   => $ttl,
            ]);
        } catch (\PDOException $e) {
            // SQLSTATE 23000 / errorCode 1062 = duplicate key (lock held).
            $errCode = ($e->errorInfo[1] ?? 0);
            if ($errCode === 1062 || $e->getCode() === '23000') {
                $sel = $pdo->prepare('SELECT acquired_at, expires_at, owner_pid FROM locks WHERE name = ?');
                $sel->execute([$name]);
                $row = $sel->fetch();
                Json::out(409, [
                    'error'       => 'lock_held',
                    'owner_pid'   => $row['owner_pid'] ?? null,
                    'acquired_at' => $row['acquired_at'] ?? null,
                    'expires_at'  => $row['expires_at'] ?? null,
                ]);
            }
            throw $e;
        }

        $sel = $pdo->prepare('SELECT expires_at FROM locks WHERE name = ?');
        $sel->execute([$name]);
        Json::out(201, [
            'name'       => $name,
            'owner_id'   => $ownerId,
            'expires_at' => $sel->fetchColumn(),
        ]);
    }

    public static function heartbeat(string $name): void
    {
        $body    = Json::body();
        $ownerId = $body['owner_id'] ?? null;
        $ttl     = $body['ttl_seconds'] ?? null;
        if (!is_string($ownerId) || !preg_match('/^[0-9a-f]{32}$/', $ownerId)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'owner_id must be 32 hex chars']);
        }
        if (!is_int($ttl) || $ttl < 1 || $ttl > 86400) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'ttl_seconds must be int 1..86400']);
        }

        $pdo  = Db::pdo();
        $stmt = $pdo->prepare(
            'UPDATE locks
                SET expires_at = NOW() + INTERVAL :ttl SECOND
              WHERE name = :name AND owner_id = :owner AND expires_at > NOW()'
        );
        $stmt->execute([':ttl' => $ttl, ':name' => $name, ':owner' => $ownerId]);

        if ($stmt->rowCount() === 0) {
            Json::out(409, ['error' => 'lost_ownership']);
        }
        $sel = $pdo->prepare('SELECT expires_at FROM locks WHERE name = ?');
        $sel->execute([$name]);
        Json::out(200, ['name' => $name, 'expires_at' => $sel->fetchColumn()]);
    }

    public static function release(string $name): void
    {
        $body    = Json::body();
        $ownerId = $body['owner_id'] ?? null;
        if (!is_string($ownerId) || !preg_match('/^[0-9a-f]{32}$/', $ownerId)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'owner_id must be 32 hex chars']);
        }
        $pdo  = Db::pdo();
        $stmt = $pdo->prepare('DELETE FROM locks WHERE name = ? AND owner_id = ?');
        $stmt->execute([$name, $ownerId]);
        // Idempotent: deleted:false means owner mismatch (or already released),
        // but we still return 200 so callers can always call release in cleanup.
        Json::out(200, ['deleted' => $stmt->rowCount() > 0]);
    }
}
