<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Json;

// POST /api/articles/{id}/fail — bump retry_count; discard at MAX_RETRIES.
// Per §4 + ISSUE-2: idempotent if row already final_state='discarded'.
final class Fail
{
    private const ALLOWED_STAGES = ['extract', 'score', 'notify'];

    public static function record(string $id): void
    {
        $body  = Json::body();
        $stage = $body['stage'] ?? null;
        if (!is_string($stage) || !in_array($stage, self::ALLOWED_STAGES, true)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'stage must be extract|score|notify']);
        }
        $errorCode = $body['error_code'] ?? null;
        if (!is_string($errorCode) || $errorCode === '') {
            Json::out(400, ['error' => 'bad_request', 'message' => 'error_code required']);
        }
        if (strlen($errorCode) > 100) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'error_code too long']);
        }
        $message = $body['message'] ?? '';
        if (!is_string($message)) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'message must be string']);
        }
        if (strlen($message) > 500) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'message too long (>500 chars)']);
        }

        $pdo        = Db::pdo();
        $maxRetries = (int)$_ENV['MAX_RETRIES'];

        // Atomic update per §4: bumps retry_count, sets final_state='discarded'
        // when the new retry_count >= MAX_RETRIES. Idempotent on already-discarded
        // rows (the WHERE final_state IS NULL clause filters them out).
        $stmt = $pdo->prepare(
            'UPDATE articles
                SET failed_at   = NOW(),
                    last_error  = CONCAT(:stage, \': \', :err),
                    retry_count = retry_count + 1,
                    final_state = CASE WHEN retry_count + 1 >= :max
                                       THEN \'discarded\'
                                       ELSE final_state
                                  END
              WHERE id = :id AND final_state IS NULL'
        );
        $stmt->execute([
            ':stage' => $stage . '/' . $errorCode,
            ':err'   => $message,
            ':max'   => $maxRetries,
            ':id'    => (int)$id,
        ]);

        // Read back current state for response.
        $sel = $pdo->prepare('SELECT retry_count, failed_at, final_state FROM articles WHERE id = ?');
        $sel->execute([(int)$id]);
        $row = $sel->fetch();
        if (!$row) {
            Json::out(404, ['error' => 'not_found']);
        }

        $discarded = ($row['final_state'] === 'discarded');
        $resp = [
            'id'          => (int)$id,
            'retry_count' => (int)$row['retry_count'],
            'max_retries' => $maxRetries,
            'discarded'   => $discarded,
        ];
        if ($discarded) {
            $resp['final_state'] = 'discarded';
        } else {
            $resp['failed_at']   = $row['failed_at'];
        }
        Json::out(200, $resp);
    }
}
