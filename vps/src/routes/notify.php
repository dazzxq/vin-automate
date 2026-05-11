<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Json;
use Tlinh\Telegram;

// POST /api/notify/{id} — 2-phase claim + send + finalize (per §4 + Codex
// ISSUE-1 / ISSUE-8 / ISSUE-14 / ISSUE-22 / ISSUE-23 / ISSUE-24).
//
// Phase 1a: atomic INSERT-style claim (UPDATE notify_claimed_at WHERE ...).
// Phase 1b: if 1a missed, SELECT to disambiguate; map to a no-op reason.
// Phase 1c: if claim was stale (older than TTL), atomically reclaim it once.
// Phase 2 : send to Telegram. The Telegram helper invokes a heartbeat callback
//           every NOTIFY_CLAIM_HEARTBEAT_SECONDS during sleeps; if the claim
//           is stolen mid-flight the send aborts and we report a no-op.
// Phase 2 step 4 finalize UPDATE clears notify_claim_*, sets notified_at +
// telegram_msg_id, and (per ISSUE-23) clears failed_at + last_error.
final class Notify
{
    public static function trigger(string $id): void
    {
        $articleId = (int)$id;
        $pdo       = Db::pdo();
        $minScore  = (int)$_ENV['MIN_SCORE_TO_NOTIFY'];
        $ttl       = (int)$_ENV['NOTIFY_CLAIM_TTL_SECONDS'];

        // The notify request can run up to ~241s in the worst case (see
        // §POST /api/notify "Claim TTL sizing"). PHP-FPM's max_execution_time
        // would kill us otherwise; lift it for this single endpoint.
        set_time_limit(0);
        ignore_user_abort(true);

        $ownerId = bin2hex(random_bytes(16));

        // Phase 1a — fresh claim.
        $stmt = $pdo->prepare(
            'UPDATE articles
                SET notify_claimed_at = NOW(),
                    notify_claim_owner = :owner
              WHERE id = :id
                AND notified_at IS NULL
                AND notify_claimed_at IS NULL
                AND score >= :min_score
                AND final_state IS NULL'
        );
        $stmt->execute([':owner' => $ownerId, ':id' => $articleId, ':min_score' => $minScore]);

        if ($stmt->rowCount() !== 1) {
            // Phase 1b — disambiguate.
            $info = $pdo->prepare('SELECT notified_at, notify_claimed_at, notify_claim_owner, score, final_state FROM articles WHERE id = ?');
            $info->execute([$articleId]);
            $row = $info->fetch();
            if (!$row) {
                Json::out(404, ['error' => 'not_found']);
            }

            if ($row['notified_at'] !== null) {
                Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'already_notified']);
            }
            if ($row['final_state'] === 'discarded') {
                Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'discarded']);
            }
            if ($row['final_state'] === 'archived') {
                Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'archived']);
            }
            if ($row['score'] === null) {
                Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'no_score']);
            }
            if ((int)$row['score'] < $minScore) {
                Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'below_threshold']);
            }

            // Either someone else holds a live claim, or it's stale.
            $claimedAt = $row['notify_claimed_at'];
            if ($claimedAt === null) {
                // Race: phase 1a saw a row that no longer has a claim — retry once.
                $stmt->execute([':owner' => $ownerId, ':id' => $articleId, ':min_score' => $minScore]);
                if ($stmt->rowCount() !== 1) {
                    Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'already_claimed']);
                }
            } else {
                $isStale = false;
                $age = $pdo->prepare(
                    'SELECT TIMESTAMPDIFF(SECOND, notify_claimed_at, NOW()) AS age
                       FROM articles WHERE id = ?'
                );
                $age->execute([$articleId]);
                $ageSeconds = (int)($age->fetchColumn() ?: 0);
                if ($ageSeconds >= $ttl) {
                    $isStale = true;
                }
                if (!$isStale) {
                    Json::out(200, ['id' => $articleId, 'sent' => false, 'reason' => 'already_claimed']);
                }

                // Phase 1c — one reclaim attempt, CAS on age.
                $reclaim = $pdo->prepare(
                    'UPDATE articles
                        SET notify_claimed_at = NOW(),
                            notify_claim_owner = :owner
                      WHERE id = :id
                        AND notified_at IS NULL
                        AND notify_claimed_at < NOW() - INTERVAL :ttl SECOND
                        AND score >= :min_score
                        AND final_state IS NULL'
                );
                $reclaim->execute([
                    ':owner'     => $ownerId,
                    ':id'        => $articleId,
                    ':ttl'       => $ttl,
                    ':min_score' => $minScore,
                ]);
                if ($reclaim->rowCount() !== 1) {
                    Json::out(200, [
                        'id'              => $articleId,
                        'sent'            => false,
                        'reason'          => 'already_claimed',
                        'stale_lost_race' => true,
                    ]);
                }
                error_log("notify.reclaimed_stale: id=$articleId");
            }
        }

        // Phase 2 — fetch row + send to Telegram.
        $sel = $pdo->prepare('SELECT id, url, canonical_url, title, source, score, score_reason FROM articles WHERE id = ?');
        $sel->execute([$articleId]);
        $article = $sel->fetch();
        if (!$article) {
            // Should never happen (we just claimed it), but be defensive.
            self::releaseClaim($pdo, $articleId, $ownerId);
            Json::out(404, ['error' => 'not_found']);
        }

        $html = self::buildMessage($article);

        $heartbeat = function () use ($pdo, $articleId, $ownerId): bool {
            $hb = $pdo->prepare(
                'UPDATE articles
                    SET notify_claimed_at = NOW()
                  WHERE id = :id
                    AND notify_claim_owner = :owner
                    AND notified_at IS NULL'
            );
            $hb->execute([':id' => $articleId, ':owner' => $ownerId]);
            return $hb->rowCount() === 1;
        };

        $result = Telegram::sendMessage($html, $heartbeat);

        if ($result['ok'] ?? false) {
            // Phase 2 step 4 — finalize CAS-bound to our claim. Per ISSUE-23,
            // this also clears failed_at + last_error.
            $finalize = $pdo->prepare(
                'UPDATE articles
                    SET notified_at = NOW(),
                        telegram_msg_id = :msg_id,
                        notify_claimed_at = NULL,
                        notify_claim_owner = NULL,
                        failed_at = NULL,
                        last_error = NULL
                  WHERE id = :id
                    AND notify_claim_owner = :owner
                    AND notified_at IS NULL'
            );
            $finalize->execute([
                ':msg_id' => $result['message_id'],
                ':id'     => $articleId,
                ':owner'  => $ownerId,
            ]);

            if ($finalize->rowCount() === 1) {
                $ts = $pdo->prepare('SELECT notified_at FROM articles WHERE id = ?');
                $ts->execute([$articleId]);
                Json::out(200, [
                    'id'              => $articleId,
                    'sent'            => true,
                    'telegram_msg_id' => $result['message_id'],
                    'notified_at'     => $ts->fetchColumn(),
                ]);
            }

            // Mid-finalize, our claim was stolen. The Telegram message went out
            // but we can't atomically pair it with notified_at. Log loudly so
            // ops can reconcile, but still surface success to the caller.
            error_log("notify.claim_stolen_post_send: id=$articleId msg_id=" . (int)$result['message_id']);
            Json::out(200, [
                'id'              => $articleId,
                'sent'            => true,
                'telegram_msg_id' => $result['message_id'],
                'warning'         => 'claim_stolen_after_send',
            ]);
        }

        // Aborted by heartbeat — claim was stolen, no Telegram send happened
        // (or one happened but is owned by the other caller's flow).
        if (($result['aborted'] ?? false) === true) {
            // Don't release the claim — the new owner has it.
            Json::out(200, [
                'id'              => $articleId,
                'sent'            => false,
                'reason'          => 'already_claimed',
                'stale_lost_race' => true,
            ]);
        }

        // Telegram failure — release the claim and record /fail.
        self::releaseClaim($pdo, $articleId, $ownerId);
        $detail = is_string($result['body'] ?? null) ? mb_substr($result['body'], 0, 400) : '';

        // Mirror POST /api/articles/{id}/fail semantics inline so we don't
        // need a self-HTTP round-trip.
        $maxRetries = (int)$_ENV['MAX_RETRIES'];
        $reasonCode = $result['reason'] ?? 'unknown';
        $pdo->prepare(
            'UPDATE articles
                SET failed_at   = NOW(),
                    last_error  = CONCAT(\'notify/\', :code, \': \', :msg),
                    retry_count = retry_count + 1,
                    final_state = CASE WHEN retry_count + 1 >= :max
                                       THEN \'discarded\'
                                       ELSE final_state
                                  END
              WHERE id = :id AND final_state IS NULL'
        )->execute([
            ':code' => $reasonCode,
            ':msg'  => mb_substr($detail, 0, 400),
            ':max'  => $maxRetries,
            ':id'   => $articleId,
        ]);

        Json::out(502, [
            'error'  => 'telegram_permanent',
            'status' => (int)($result['status'] ?? 0),
            'detail' => $detail,
            'reason' => $reasonCode,
        ]);
    }

    private static function buildMessage(array $row): string
    {
        $id     = (int)$row['id'];
        $score  = (int)($row['score'] ?? 0);
        $url    = (string)($row['canonical_url'] ?? $row['url'] ?? '');
        $title  = htmlspecialchars((string)($row['title'] ?? ''), ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $source = htmlspecialchars((string)($row['source'] ?? ''), ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $reason = htmlspecialchars((string)($row['score_reason'] ?? ''), ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $urlEsc = htmlspecialchars($url, ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $dot    = $score >= 5 ? '🔴' : ($score >= 4 ? '🟠' : '🟡');

        return "<b>[#{$id}]</b> {$dot} score {$score}/5 | <i>{$source}</i>\n\n"
             . "<b>{$title}</b>\n"
             . ($reason !== '' ? "<i>{$reason}</i>\n\n" : "\n")
             . "<a href=\"{$urlEsc}\">Đọc bài</a>\n\n"
             . "Brainstorm: <code>/idea-brainstormer {$id}</code>";
    }

    private static function releaseClaim(\PDO $pdo, int $id, string $ownerId): void
    {
        $pdo->prepare(
            'UPDATE articles
                SET notify_claimed_at = NULL,
                    notify_claim_owner = NULL
              WHERE id = :id AND notify_claim_owner = :owner'
        )->execute([':id' => $id, ':owner' => $ownerId]);
    }
}
