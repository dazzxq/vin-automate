<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Json;

// PATCH /api/articles/{id}/score — CAS: only updates if scored_at IS NULL
// AND final_state IS NULL (per §4 and ISSUE-2 + ISSUE-23 success-path clearing).
final class Score
{
    public static function update(string $id): void
    {
        $body  = Json::body();
        $score = $body['score'] ?? null;
        if (!is_int($score) || $score < 1 || $score > 5) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'score must be int 1..5']);
        }
        $reason = $body['reason'] ?? '';
        if (!is_string($reason)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'reason must be a string']);
        }
        if (strlen($reason) > 500) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'reason too long (>500 chars)']);
        }

        $pdo  = Db::pdo();
        $stmt = $pdo->prepare(
            'UPDATE articles
                SET scored_at = NOW(),
                    score = :score,
                    score_reason = :reason,
                    failed_at = NULL,
                    last_error = NULL
              WHERE id = :id
                AND scored_at IS NULL
                AND final_state IS NULL'
        );
        $stmt->execute([
            ':score'  => $score,
            ':reason' => $reason,
            ':id'     => (int)$id,
        ]);
        if ($stmt->rowCount() === 0) {
            // Either already scored, final_state set, or row missing.
            $exists = $pdo->prepare('SELECT scored_at, final_state FROM articles WHERE id = ?');
            $exists->execute([(int)$id]);
            $row = $exists->fetch();
            if (!$row) {
                Json::out(404, ['error' => 'not_found']);
            }
            $reasonStr = $row['final_state'] !== null ? 'discarded_or_archived' : 'already_scored';
            Json::out(200, ['id' => (int)$id, 'updated' => false, 'reason' => $reasonStr]);
        }
        $stmt = $pdo->prepare('SELECT scored_at FROM articles WHERE id = ?');
        $stmt->execute([(int)$id]);
        Json::out(200, [
            'id'        => (int)$id,
            'updated'   => true,
            'scored_at' => $stmt->fetchColumn(),
        ]);
    }
}
